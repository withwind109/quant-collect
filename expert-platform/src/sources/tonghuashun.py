"""同花顺·模拟炒股：A股高手模拟盘的收益率排行 + 当日买卖明细。

数据性质（与用户需求一致）：同花顺模拟炒股的高手主页**公开展示**其收益率、持仓与逐笔
买卖（如搜索结果中 `kingdee_YY 09:30 买入`），属于「页面上能看到用户真实盈利/买入」的社区数据。
注意这是模拟盘（虚拟资金），性质与东财社区高手操作一致。

端点与可达性（2026-09 调研）：
  * 模拟炒股排行榜（服务端渲染 HTML）：https://moni.10jqka.com.cn/xgcg.shtml
  * 百强排行另见 https://moni.10jqka.com.cn/ 与 /hezuo/index/paihang/bsid_xxx
  * 高手主页（含持仓 / 交易记录）：榜单每行「主页」链接进入；交易记录需登录后「追踪」才可见
  * 反爬：同花顺 WAF 需 `hexin-v` 令牌，可从登录 Cookie 的 `v=` 字段提取（与论股堂同体系）

⚠️ 数据可用性边界（2026-09-12 实测，均已用当前 .env 凭据复现）：
  * `hot_experts`：排行榜 `xgcg.shtml` 为**服务端渲染**，榜单行直接嵌在 HTML 表格里
    （并非 JS 异步加载），匿名即可解析，实测稳定产出 50 位高手（用户名/总盈利率/总资产/主页）。
    注意收益率字段形如 `1.77%`，需先剥离百分号再转 float（见 `core.utils.to_float`）。
  * `signals`：首页 `index.shtml` 的 `master_news_list` 是**高手买卖动态**，匿名可取，
    实测 96 行（25 买入 / 71 卖出），字段为 昵称 + 时间(YYYY-MM-DD HH:MM) + 买卖方向。
    ⚠️ **不含股票代码** —— 该表只有 3 列，站点未披露标的。
  * `trades`：高手主页（含持仓/逐笔成交）需要**有效登录态 + 已「追踪」该高手**。
    实测当前 THS_COOKIE 已失效（页面 `sns_login_not` 显示未登录），且即便匿名访问，
    交易表 `jyjl_tbl` / 持仓表 `ccqk_tbl` 的「股票代码 / 股票名称」两列恒为
    `<a class='zhuikaen tips_kt' href='###'></a>` 占位符（`href` 模板未回填），
    即**拿得到 时间/方向/数量/价格/金额，但拿不到买的是哪只股票**。
    故本方法只输出「带股票代码」的记录进入主视图；无代码的记录进 `signals`，
    并在日志中明确提示需要重新登录 + 追踪该高手。

✅ F2「追踪」接口 (2026-09-14 人工抓包 + 实测确认可用，见 follow_expert()):
  真实端点: POST https://moni.10jqka.com.cn/mncg/index/addzzgs/{uid}
  （"addzzgs" = 添加追踪高手 拼音首字母）。无请求体，靠 Cookie+hexin-v+Referer
  鉴权。⚠️ 站点自身有个坑：**首次成功追踪一个新高手时，服务端返回 HTTP 500 +
  空响应体**（不是失败！同花顺自己前端把这个也当失败处理，弹"操作失败"提示，
  这正是用户在浏览器里看到该提示、但实际已追踪成功的原因）。已用
  「追踪后立即复查持仓表 zhuikaen 占位符归零」验证过是真成功，非巧合。
  重复追踪同一人返回 HTTP 200 + `{"errorcode":1,"errormsg":"已追踪..."}`。
  follow_expert() 按此已验证的双模式判定成功，见下方实现。
"""
from __future__ import annotations

import json
import re
from datetime import date

from loguru import logger

from core.http import HttpClient
from core.models import (
    ACTION_BUY,
    ACTION_SELL,
    Expert,
    TradeRecord,
)
from core.utils import to_float
from .base import BaseSource

BASE = "https://moni.10jqka.com.cn"
RANK_URL = f"{BASE}/xgcg.shtml"
INDEX_URL = f"{BASE}/index.shtml"
FOLLOW_URL_TMPL = f"{BASE}/mncg/index/addzzgs/{{uid}}"  # F2: 追踪高手，见上方 docstring

_TAG_RE = re.compile(r"<[^>]+>", re.S)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
#: 整段 <td ...>...</td>（保留标签，用于从属性里取 title=时间戳）
_TD_FULL_RE = re.compile(r"<td[^>]*>.*?</td>", re.S | re.I)
#: 真实股票代码链接（高手主页被掩盖时不会有匹配）
_STOCKPAGE_RE = re.compile(r"stockpage\.10jqka\.com\.cn/(\d{6})")
#: 高手主页链接中的用户 ID
_HOME_ID_RE = re.compile(r"moni\.10jqka\.com\.cn/(\d+)")
#: 高手动态里的完整时间戳
_STAMP_RE = re.compile(r'title="(\d{4}-\d{2}-\d{2}[ \d:]*)"')


def _strip(text: str) -> str:
    return _TAG_RE.sub("", text or "").replace("\u00a0", " ").strip()


def _cells(row_html: str) -> list[str]:
    return [_strip(c) for c in _TD_RE.findall(row_html)]


def _first_href(row_html: str) -> str:
    m = _HREF_RE.search(row_html)
    return m.group(1) if m else ""


def _extract_hexin_v(cookie: str) -> str:
    """从 Cookie 的 v= 字段取 hexin-v（同花顺 WAF 令牌）。"""
    for part in (cookie or "").split(";"):
        part = part.strip()
        if part.startswith("v="):
            return part[2:].strip()
    return ""


def _classify(direction: str) -> str:
    """同花顺方向文本 -> 统一动作。

    注意站点渲染的是 `卖入`（而非「卖出」）与 `买入`；`卖入` 同时含「买」「卖」二字，
    故必须先判「卖」再判「买」。
    """
    text = str(direction or "")
    if "卖" in text or "减" in text:
        return ACTION_SELL
    if "买" in text or "加" in text:
        return ACTION_BUY
    return text


class TonghuashunSource(BaseSource):
    name = "tonghuashun"

    def __init__(self, http: HttpClient, cookie: str | None = None, hexin_v: str | None = None) -> None:
        super().__init__(http)
        self.cookie = cookie or ""
        self.hexin_v = hexin_v or _extract_hexin_v(self.cookie)
        #: 首页 HTML 缓存（candidate_dates 与 signals 共用，避免重复请求）
        self._index_html: str | None = None
        #: 高手主页里「有成交但未披露标的」的记录，归入 signals
        self._code_less: list[dict] = []

    @property
    def has_login(self) -> bool:
        return bool(self.cookie)

    def _headers(self, referer: str = f"{BASE}/") -> dict[str, str]:
        headers = {"Referer": referer, "Accept": "text/html,application/xhtml+xml"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        if self.hexin_v:
            headers["hexin-v"] = self.hexin_v
        return headers

    # ── F2: 追踪高手 (解锁逐笔交易的股票代码字段) ──────────────────
    def follow_expert(self, uid: str) -> bool:
        """对指定高手发起「追踪」请求，解锁其逐笔交易的股票代码字段
        (未追踪时该字段恒为 `<a class='zhuikaen' href='###'></a>` 占位符)。

        端点: POST https://moni.10jqka.com.cn/mncg/index/addzzgs/{uid}，无请求体。
        2026-09-14 人工抓包 + 实测确认（用户提供新鲜 Cookie 后现场验证）：

          * 首次成功追踪一个新高手: HTTP 500 + 空响应体（同花顺自己的已知坑——
            前端把这个当失败处理，弹"操作失败，请稍后再试"，但服务端其实已经
            写入成功）。已用"追踪后立即复查该高手主页 zhuikaen 占位符归零"
            实测验证过是真成功，不是巧合或侥幸。
          * 已追踪过的高手再次调用: HTTP 200 + JSON
            `{"errorcode":1,"errormsg":"您已追踪该用户，无需再次执行此操作！","result":{}}`
            —— 这也是成功状态（幂等），不是失败。

        绕开 HttpClient.post_json()（它对 >=400 状态码会直接抛异常，且会把
        payload 序列化成 JSON body，而真实请求是空 body），直接用底层
        session 发送，自行判定这两种已验证的成功形态。
        """
        url = FOLLOW_URL_TMPL.format(uid=uid)
        headers = self._headers(referer=f"{BASE}/{uid}")
        headers.update({
            "Origin": BASE,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*",
        })
        try:
            # fetch_status(): tonghuashun 源用 BrowserClient(Playwright) 抓取后端
            # (反爬需要)，没有 .session 属性；且 post_json/get_json 遇到非2xx
            # 直接抛异常，拿不到本接口"状态码本身编码成功/失败语义"的信息，
            # 故用两个后端都实现的 fetch_status() 原始访问 (见 core/http.py)。
            status, text = self.http.fetch_status(url, method="POST", headers=headers)
        except Exception as e:
            logger.warning(f"[同花顺] 追踪高手 {uid} 请求异常: {e}")
            return False

        if status == 500 and not (text or "").strip():
            # 已知怪癖: 首次成功追踪返回 500 空体, 见上方 docstring
            logger.info(f"[同花顺] 追踪高手 {uid}: 成功 (首次追踪, 500空体已知模式)")
            return True
        if status == 200:
            try:
                data = json.loads(text)
            except (ValueError, TypeError):
                logger.warning(f"[同花顺] 追踪高手 {uid} 响应非JSON: {text[:200]!r}")
                return False
            errorcode = data.get("errorcode")
            if errorcode in (0, 1):  # 0=推测的"新增成功", 1=已验证的"已追踪"(幂等)
                logger.info(f"[同花顺] 追踪高手 {uid}: 成功 errorcode={errorcode} msg={data.get('errormsg')}")
                return True
            logger.warning(f"[同花顺] 追踪高手 {uid} 失败: {data}")
            return False
        logger.warning(f"[同花顺] 追踪高手 {uid} 意外状态码 {status}: {text[:200]!r}")
        return False

    def batch_follow(self, experts: list[Expert], limit: int = 50, interval_sec: float = 2.0) -> dict:
        """批量追踪前 limit 位高手（用户决策 2026-09-14: 先 Top 50 小范围测试，
        观察是否触发同花顺账号风控，再决定是否扩大）。

        interval_sec 控制请求间隔，默认 2 秒/次，Top 50 约需 100 秒——刻意放慢，
        批量操作账号行为本身就比爬只读页面更容易触发风控，先保守再视测试结果调整。
        """
        import time as _time

        targets = [e for e in experts if e.expert_id][:limit]
        result = {"total": len(targets), "success": 0, "failed": 0, "errors": []}
        for i, e in enumerate(targets):
            try:
                ok = self.follow_expert(e.expert_id)
                if ok:
                    result["success"] += 1
                else:
                    result["failed"] += 1
                    result["errors"].append(e.expert_id)
            except RuntimeError:
                raise  # 配置缺失: 直接向上抛，不静默吞掉
            except Exception as ex:
                result["failed"] += 1
                result["errors"].append(f"{e.expert_id}: {ex}")
            if i < len(targets) - 1:
                _time.sleep(interval_sec)
        logger.info(f"[同花顺] 批量追踪完成: {result['success']}/{result['total']} 成功")
        return result

    # ── 榜单 ──────────────────────────────────────────────
    def hot_experts(self, limit: int = 50) -> list[Expert]:
        html = self.http.get_text(RANK_URL, headers=self._headers())
        experts = self._parse_rank(html, limit)
        if not experts:
            logger.warning(
                "[同花顺] 榜单解析为空，可能页面结构变化或未登录；页面片段(前1500字)：\n"
                + html[:1500]
            )
        logger.info(f"[同花顺] 模拟炒股排行榜 解析得 {len(experts)} 位高手")
        return experts[:limit]

    def _parse_rank(self, html: str, limit: int) -> list[Expert]:
        """解析排行榜表格行。单元格顺序（观察）：排名/用户名/段位/总盈利率/月/周/日/选股成功率/总资产/被追踪数/动态/主页。"""
        experts: list[Expert] = []
        for row in _ROW_RE.findall(html):
            cells = _cells(row)
            # 至少需要 用户名 + 总盈利率 两列才有意义
            if len(cells) < 4:
                continue
            name = cells[1]
            if not name or name in ("用户名",):
                continue
            total_rate = to_float(cells[3])
            if total_rate is None:
                continue
            href = _first_href(row)
            expert_id = href.rstrip("/").split("/")[-1] or name
            # 主页链接统一升级为 https
            home = href if href.startswith("http") else BASE + href
            home = home.replace("http://", "https://", 1)
            experts.append(
                Expert(
                    source=self.name,
                    asset_type="stock",
                    channel="同花顺模拟炒股",
                    expert_id=expert_id,
                    name=name,
                    owner=name,
                    rank_label="模拟炒股百强",
                    followers=int(to_float(cells[9]) or 0),
                    return_rate=total_rate,
                    extra={
                        "home_url": home,
                        "duanwei": cells[2] if len(cells) > 2 else "",
                        "month_rate": to_float(cells[4]) if len(cells) > 4 else None,
                        "week_rate": to_float(cells[5]) if len(cells) > 5 else None,
                        "day_rate": to_float(cells[6]) if len(cells) > 6 else None,
                        "success_rate": to_float(cells[7]) if len(cells) > 7 else None,
                        "total_asset": to_float(cells[8]) if len(cells) > 8 else None,
                    },
                )
            )
            if len(experts) >= limit:
                break
        return experts

    # ── 交易明细 ──────────────────────────────────────────
    def trades(self, experts: list[Expert], trade_date: date) -> list[TradeRecord]:
        if not self.has_login:
            logger.warning(
                "[同花顺] 未配置 THS_COOKIE（需登录态），跳过高手主页交易明细；榜单数据仍会输出。"
            )
            return []

        target = trade_date.strftime("%Y-%m-%d")
        records: list[TradeRecord] = []
        for exp in experts:
            home = (exp.extra or {}).get("home_url")
            if not home:
                continue
            try:
                html = self.http.get_text(home, headers=self._headers(home))
                records.extend(self._parse_trades(exp, html, target))
            except Exception as exc:
                logger.warning(f"[同花顺] 高手 {exp.expert_id} 主页抓取失败：{exc}")
        if self._code_less:
            logger.warning(
                f"[同花顺] 高手主页共 {len(self._code_less)} 笔当日成交**未披露股票代码**"
                f"（页面为 zhuikaen 占位符，需登录并「追踪」该高手后才可见）。"
                f"这些记录已归入 signals.json，不进入高手买入主视图。"
            )
        logger.info(f"[同花顺] 当日({target})带股票代码的交易记录 {len(records)} 条")
        return records

    def _parse_trades(self, exp: Expert, html: str, target_date: str) -> list[TradeRecord]:
        """解析高手主页的交易记录表 `jyjl_tbl`。

        表头顺序（实测）：成交时间 | 股票代码 | 股票名称 | 操作 | 成交数量 | 成交价 |
        成交金额 | 交易状态。日期为完整格式 `YYYY-MM-DD`。

        只输出**能确认真实股票代码**的行（`stockpage.10jqka.com.cn/<6位代码>`）；
        代码被 `zhuikaen` 占位符掩盖的行改写入 `self._code_less`（进 signals）。
        这样「高手买入主视图」里不会出现没有标的的空行。
        """
        out: list[TradeRecord] = []
        m = re.search(r'id="jyjl_tbl".*?(<!-- 交易记录@结束 -->|</table>)', html, re.S)
        if not m:
            logger.debug(f"[同花顺] 高手 {exp.expert_id} 主页无交易记录表")
            return out
        seg = m.group(0)
        for row in _ROW_RE.findall(seg)[1:]:  # 跳过表头
            cells = _cells(row)
            if len(cells) < 8:
                continue
            trade_dt, code_text, name_text, action_raw, qty, price, amount, status = cells[:8]
            if trade_dt != target_date:
                continue
            if status and status != "已成交":
                continue
            action = _classify(action_raw)
            hit = _STOCKPAGE_RE.search(row)
            code = hit.group(1) if hit else ""
            if not code:
                # 未披露标的：不进主视图，作为信号留存（含方向/数量/价格/金额）
                self._code_less.append({
                    "kind": "主页成交(未披露标的)",
                    "expert_id": exp.expert_id,
                    "expert_name": exp.name,
                    "action": action,
                    "stock_code": "",
                    "stock_name": "",
                    "price": to_float(price),
                    "volume": int(to_float(qty) or 0),
                    "amount": to_float(amount),
                    "trade_time": f"{trade_dt} 00:00",
                    "note": "同花顺高手主页股票代码为占位符（需登录且已追踪该高手）",
                })
                continue
            out.append(
                TradeRecord(
                    source=self.name,
                    asset_type="stock",
                    channel="同花顺模拟炒股",
                    expert_id=exp.expert_id,
                    expert_name=exp.name,
                    owner=exp.owner,
                    stock_code=code,
                    stock_name=(name_text or "").strip() or code,
                    market="",
                    action=action,
                    return_rate=exp.return_rate,
                    day_return_rate=to_float((exp.extra or {}).get("day_rate")),
                    trade_time=f"{trade_dt} 00:00",
                    trade_date=target_date,
                    price=to_float(price) or 0.0,
                    volume=int(to_float(qty) or 0),
                    amount=to_float(amount) or 0.0,
                    raw={"action_raw": action_raw, "status": status},
                )
            )
        # fix: 用 _code_less 长度的前后差值判断本高手是否新增了无标的记录，
        # 避免全局列表非空时错误地压制 debug 日志。
        # 调用方 trades() 在调用前记录长度并传入，此处保持签名兼容，改用局部变量。
        if not out:
            logger.debug(f"[同花顺] 高手 {exp.expert_id} 当日无成交（或主页交易表无 {target_date} 数据）")
        return out

    # ── 首页「高手买卖动态」（匿名可取，公开 昵称+时间+方向）──────
    def _fetch_index(self) -> str:
        if self._index_html is None:
            try:
                self._index_html = self.http.get_text(INDEX_URL, headers=self._headers())
            except Exception as exc:
                logger.warning(f"[同花顺] 首页高手动态抓取失败：{exc}")
                self._index_html = ""
        return self._index_html

    def _parse_master_news(self, html: str) -> list[dict]:
        """解析 `master_news_list`：高手动态（3 列 = 昵称 / 时间 / 买卖方向）。"""
        m = re.search(r'id="master_news_list".*?</table>', html, re.S)
        if not m:
            return []
        out: list[dict] = []
        for row in _ROW_RE.findall(m.group(0)):
            tds = _TD_FULL_RE.findall(row)
            if len(tds) < 3:
                continue
            uid = _HOME_ID_RE.search(tds[0])
            stamp = _STAMP_RE.search(tds[1])
            if not (uid and stamp):
                continue
            name = re.search(r'title="([^"]+)"', tds[0])
            out.append({
                "kind": "买卖动态",
                "expert_id": uid.group(1),
                "expert_name": (name.group(1) if name else "").strip(),
                "action": _classify(_strip(tds[2])),
                "stock_code": "",
                "stock_name": "",
                "trade_time": stamp.group(1).strip(),
                "note": "同花顺首页「高手动态」公开 昵称+时间+买卖方向，站点未披露标的",
            })
        return out

    def candidate_dates(self) -> dict[date, int]:
        """从首页高手动态统计各日活跃人数，作为交易日候选。"""
        hist: dict[date, int] = {}
        for sig in self._parse_master_news(self._fetch_index()):
            text = (sig.get("trade_time") or "")[:10]
            try:
                day = date.fromisoformat(text)
            except ValueError:
                continue
            hist[day] = hist.get(day, 0) + 1
        recent = sorted(hist, reverse=True)[:3]
        return {d: hist[d] for d in recent}

    def signals(self) -> list[dict]:
        return self._parse_master_news(self._fetch_index()) + list(self._code_less)
