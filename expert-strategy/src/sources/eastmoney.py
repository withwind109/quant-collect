"""东方财富：实盘组合高手榜 + 关注组合调仓明细（rtV1 / rtV3 / srtV1）。

⚠️ 2026-10-19 起模拟盘通道整体下线（simqry2.eastmoney.com 的 spo_* 系列接口
   ——全站操作流水 / 买入热度榜 / 组合调仓历史 / 组合基础信息——已全部删除），
   本模块只保留**实盘**数据链路，杜绝任何逻辑再爬到模拟盘数据。

接口（2026-10 实测）：
  1. 实盘组合选手榜（匿名可用）
     GET https://emdcspzhapi.dfcfs.cn/rtV1
         ?type=rt_get_rank&rankType=10005&recIdx=0&recCnt=20&rankid=0&appVer=9001000
     rankType: 10005=日榜(当日收益) 10004=总榜(累计收益) 10000/10001/10003=周/月/年榜
  2. 实盘组合调仓明细（需 APP 会话令牌 + SM4 签名，仅对关注者开放）
     POST https://spzhapi.dfcfs.cn/rtV3  method=CombinationRelocatePositionHandler
  3. 东财账号关注关系（需 APP 令牌）
     查已关注 rtV3 FollowCombinationQueryHandler / 关注·取消 srtV1 GET rt_add_concern·rt_cancel_concern

认证方式：
  * rtV1 榜单匿名即可抓取；配置登录态 Cookie（config.yaml eastmoney.cookie 或
    .env EASTMONEY_COOKIE）后随请求带上，可降低被 WAF 拦截的概率。
  * rtV3/srtV1 需要 APP 会话令牌（ctToken/utToken/userId），由「账户中心」
    扫码登录后写入 broker_accounts，pipeline 自动注入。
"""
from __future__ import annotations

import hashlib
import json
import random
import string
import time
from datetime import date, datetime

from loguru import logger

from core.http import HttpClient
from core.models import (
    ACTION_BUY,
    ACTION_SELL,
    Expert,
    TradeRecord,
)
from core.utils import MARKET_LIVE, normalize_symbol, to_float, to_int
from .base import BaseSource

LIVE_RANK_URL = "https://emdcspzhapi.dfcfs.cn/rtV1"
SPZH_URL = "https://spzhapi.dfcfs.cn/rtV3"
#: 关注/取消关注走 srtV1 GET（xcq-platform 2026-09-30 验证可用；deviceId 可空）
SRTV1_URL = "https://spzhapi.dfcfs.cn/srtV1"
SRTV1_APP_VER = "11002002"
REFERER_PC = "https://group.eastmoney.com/"

#: 实盘榜 rankType
RANK_TYPES = {
    "10005": "日榜",
    "10000": "周榜",
    "10001": "月榜",
    "10003": "年榜",
    "10004": "总榜",
}

#: mktCode -> 市场
_MKT_CODE = {"1": "SH", "0": "SZ", "2": "BJ"}

SM4_KEY = bytes.fromhex("e4dd41fd138867c3665492702fe277eb")


def _normalize_action(mmbz: str) -> str:
    """东财 mmbz（买入/卖出）-> 统一动作。"""
    text = str(mmbz or "")
    if "买" in text or "加" in text:
        return ACTION_BUY
    if "卖" in text or "减" in text:
        return ACTION_SELL
    return text


class EastmoneySource(BaseSource):
    name = "eastmoney"

    def __init__(
        self,
        http: HttpClient,
        rank_type: str = "10005",
        rank_types: list[str] | None = None,
        cookie: str = "",
        spzh_ct_token: str = "",
        spzh_ut_token: str = "",
        spzh_user_id: str = "",
        spzh_device_id: str = "",
        spzh_app_version: str = "11.2.2",
        spzh_rn_version: str = "2.3.0.260731172423109",
        spzh_follow: list[dict] | None = None,
    ) -> None:
        super().__init__(http)
        self.rank_type = rank_type
        # 多榜合并抓取 [2026-10-19]: 默认 日榜(10005, 当日收益) + 总榜(10004, 累计收益)。
        # rtV1 的 rateForApp 语义跟随 rankType —— 日榜只有日收益, 累计收益必须靠总榜补。
        self.rank_types = [str(x) for x in (rank_types or [rank_type, "10004"])]
        # [2026-10-16] 登录态 Cookie（.env EASTMONEY_COOKIE / config.yaml eastmoney.cookie）。
        # 东财接口匿名可用，带 Cookie 只是降低 WAF 拦截概率；失效不影响匿名接口。
        self.cookie = cookie or ""
        self.ct_token = spzh_ct_token
        self.ut_token = spzh_ut_token
        self.user_id = spzh_user_id
        self.device_id = spzh_device_id
        self.app_version = spzh_app_version
        self.rn_version = spzh_rn_version
        # 用户关注的实盘组合常驻列表: [{"expert_id": "900457430", "name": "狐狸大王呀"}, ...]
        # rtV3 明细仅对关注者开放, 这些组合可能不在任何榜单上, 必须显式带上 [2026-09-25]
        self.spzh_follow = [
            Expert(source=self.name, expert_id=str(x.get("expert_id") or ""),
                   name=str(x.get("name") or ""))
            for x in (spzh_follow or []) if x.get("expert_id")
        ]

    @property
    def has_spzh_token(self) -> bool:
        return bool(self.ct_token and self.ut_token and self.user_id)

    @property
    def has_cookie(self) -> bool:
        return bool(self.cookie)

    def _headers(self) -> dict[str, str]:
        """东财接口通用请求头（Referer + 可选登录态 Cookie）。"""
        headers = {"Referer": REFERER_PC}
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    # ── 1. 实盘选手榜 ─────────────────────────────────────
    def hot_experts(self, limit: int = 50) -> list[Expert]:
        """多榜合并抓取（self.rank_types，默认 日榜+总榜）。

        ⚠️ rtV1 的 rateForApp 语义取决于 rankType，同一字段在不同榜含义不同：
          * 总榜(10004) rateTitle="总收益"   → 累计收益率，进 return_rate
          * 日榜(10005) rateTitle="日收益"   → 当日收益率，进 extra.daily_gain
          * 其他榜（周/月/年/250日）        → 原样存 extra.board_rates
        合并规则：同一组合以先出现的榜（日榜）为基本信息源，收益字段按榜语义覆盖。
        """
        by_id: dict[str, Expert] = {}
        for rt in self.rank_types:
            label = RANK_TYPES.get(rt, rt)
            experts: list[Expert] = []
            rec_idx, page_size = 0, min(20, max(limit, 1))
            while len(experts) < limit:
                data = self.http.get_json(
                    LIVE_RANK_URL,
                    params={
                        "type": "rt_get_rank",
                        "rankType": rt,
                        "recIdx": rec_idx,
                        "recCnt": page_size,
                        "rankid": 0,
                        "appVer": "9001000",
                    },
                    headers=self._headers(),
                )
                items = (data or {}).get("data") or []
                if not items:
                    break
                for it in items:
                    title = str(it.get("rateTitle") or "")
                    rate = to_float(it.get("rateForApp"))
                    experts.append(
                        Expert(
                            source=self.name,
                            channel="东财实盘组合榜",
                            expert_id=str(it.get("zjzh", "")),
                            name=str(it.get("zhuheName", "")),
                            owner=str(it.get("uidNick", "")),
                            rank_label=f"东财实盘{label}",
                            followers=to_int(it.get("concernCnt")),
                            return_rate=rate if "总" in title else None,
                            market_type=MARKET_LIVE,  # rtV1 即实盘榜 [2026-09-23 打标]
                            extra={
                                "userid": it.get("userid"),
                                "labels": [it.get(f"label{i}") for i in (1, 2, 3) if it.get(f"label{i}")],
                                "rate_title": title,
                                "rate_value": rate,
                                "daily_gain": rate if title == "日收益" else None,
                                "board_rates": {title: rate} if title else {},
                            },
                        )
                    )
                    if len(experts) >= limit:
                        break
                if len(items) < page_size:
                    break
                rec_idx += len(items)
            logger.info(f"[东财] 实盘榜({label}) 选手 {len(experts)} 位")
            # 合并进 by_id：同组合以先出现榜为基，收益字段按语义补
            for e in experts:
                old = by_id.get(e.expert_id)
                if old is None:
                    by_id[e.expert_id] = e
                    continue
                if e.return_rate is not None:
                    old.return_rate = e.return_rate
                if e.extra.get("daily_gain") is not None:
                    old.extra["daily_gain"] = e.extra["daily_gain"]
                old.extra.setdefault("board_rates", {}).update(e.extra.get("board_rates") or {})
                for k in ("labels",):
                    if e.extra.get(k) and not old.extra.get(k):
                        old.extra[k] = e.extra[k]
        return list(by_id.values())

    # ── 2. 当日买入明细（仅实盘 rtV3，2026-10-19 模拟盘通道已删除）──
    def trades(self, experts: list[Expert], trade_date: date) -> list[TradeRecord]:
        target = trade_date.strftime("%Y-%m-%d")
        records: list[TradeRecord] = []
        if self.has_spzh_token:
            # [2026-09-28 修] rtV3 只对本账户关注的组合开放——只查关注组合，
            # 榜单组合（没关注）查了也拿不到；关注组合按 expert_id 用榜单上的完整信息。
            by_id = {e.expert_id: e for e in experts}
            follow = [by_id.get(e.expert_id, e) for e in self.spzh_follow]
            records.extend(self._spzh_trades(follow, target))
        else:
            logger.warning("[东财] 无 APP 令牌，跳过 rtV3 实盘调仓明细——请到账户中心扫码登录")
        return records

    # ── 4.6 东财账号关注关系（关注列表 / 关注 / 取消关注，需 APP 令牌）──
    #
    # rtV3 调仓明细只对本账户「已关注」的组合开放（30001=未关注），因此页面上的
    # 「关注」不只是本地收藏，而是真的在东财账号上代为关注（与 xcq-platform
    # watchlist.em_ensure_follow 同一套接口，2026-09-30 实测可用）：
    #   * 查已关注：rtV3 POST FollowCombinationQueryHandler → data.followCidList
    #   * 关注    ：srtV1 GET  type=rt_add_concern    &zh={组合号}
    #   * 取消    ：srtV1 GET  type=rt_cancel_concern &zh={组合号}
    # srtV1 返回 {"result":"0"} 即成功；令牌只需要 ctToken/utToken/userId 三要素。
    def followed_ids(self) -> list[str]:
        """东财账号当前已关注的实盘组合号列表。"""
        resp = self._spzh_call("FollowCombinationQueryHandler", {}) or {}
        return [str(x) for x in ((resp.get("data") or {}).get("followCidList") or [])]

    def follow(self, cid: str) -> bool:
        """在东财账号关注一个实盘组合。"""
        return self._srtv1("rt_add_concern", cid)

    def unfollow(self, cid: str) -> bool:
        """在东财账号取消关注一个实盘组合。"""
        return self._srtv1("rt_cancel_concern", cid)

    def combination_info(self, cid: str) -> dict:
        """组合基础信息（zuheName/uidNick/concernCnt/summary…）。

        rtV3 CombinationInfoHandler，2026-10-19 实测：关注组合多数可查；
        设置了隐私的组合返回 code=9062「该组合已隐藏」（失败返回 {} 不抛）。
        """
        try:
            resp = self._spzh_call("CombinationInfoHandler", {"combinationId": str(cid)}) or {}
            if resp.get("code") == 0:
                return resp.get("data") or {}
            logger.warning(f"[东财] 组合信息 {cid}: {resp.get('message')}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[东财] 组合信息请求失败 {cid}: {exc}")
        return {}

    def _srtv1(self, action: str, cid: str) -> bool:
        if not self.has_spzh_token:
            raise RuntimeError("东财未登录（缺 APP 令牌），无法操作关注——请先到账户中心扫码")
        # srtV1 是 GET 接口，不能走 rtV3 的 POST 信封；与 rtV3 同样用独立 curl 后端
        if getattr(self, "_srtv1_http", None) is None:
            from core.http import HttpClient
            self._srtv1_http = HttpClient(min_interval=0.8)
        data = self._srtv1_http.get_json(
            SRTV1_URL,
            params={"type": action, "ctToken": self.ct_token, "utToken": self.ut_token,
                    "userId": self.user_id, "appVer": SRTV1_APP_VER, "zh": str(cid)},
        ) or {}
        ok = str(data.get("result")) == "0"
        if not ok:
            logger.warning(f"[东财] srtV1 {action} {cid} 失败: {data}")
        return ok

    # ── 5. 实盘组合调仓（可选，需 APP 令牌）──────────────────
    def _spzh_trades(self, experts: list[Expert], target_date: str) -> list[TradeRecord]:
        out: list[TradeRecord] = []
        n_fail = 0
        for exp in experts:
            try:
                # pageSize=40 是服务端硬上限 [2026-09-25 实测]: 翻页参数(pageIndex/page/start)
                # 全被忽略, 永远返回最近 40 条; 40 已是单次可得的完整窗口。
                resp = self._spzh_call(
                    "CombinationRelocatePositionHandler",
                    {"combinationId": exp.expert_id, "pageIndex": 1, "pageSize": 40},
                )
            except Exception as exc:
                logger.warning(f"[东财] rtV3 调仓请求失败 {exp.expert_id}：{exc}")
                n_fail += 1
                continue
            if (resp or {}).get("code") != 0:
                # 单个组合失败 (未关注/临时错误) 跳过继续, 不再中断整轮扫描 [2026-09-28]
                logger.warning(f"[东财] rtV3 {exp.expert_id} 返回 {(resp or {}).get('code')}: {(resp or {}).get('message')}")
                n_fail += 1
                continue
            for it in _as_list(resp.get("data")):
                # rtV3 实际字段 [2026-09-25 实测]: bizDate/relocateTime/stkCode/stkName/
                # relocateQty/bsMark(B|S)/relocatePrice/positionRatio("3成"/"9成以上")
                norm = _normalize_date(it.get("bizDate") or it.get("date") or it.get("tradeDate"))
                # [2026-09-30 修] 开盘初"自动推断交易日"常推成昨天 (模拟盘流水多是昨天的), 只按它筛会把今天的实盘成交全丢掉
                # (09-30 开盘到 10:03 重点组合 12 笔全漏)。当天日期的也保留; 入库按每条记录自己的 trade_date (monitor.ingest_rows)。
                if target_date and norm not in (target_date, _today_str()):
                    continue
                bs = str(it.get("bsMark") or it.get("direction") or it.get("bsFlag") or "").upper()
                if bs in {"B", "1"} or "买" in bs:
                    action = ACTION_BUY
                elif bs in {"S", "2"} or "卖" in bs:
                    action = ACTION_SELL
                else:
                    action = bs
                out.append(
                    TradeRecord(
                        source=self.name,
                        channel="东财实盘组合调仓",
                        expert_id=exp.expert_id,
                        expert_name=exp.name,
                        owner=exp.owner,
                        stock_code=str(it.get("stkCode") or it.get("stockCode") or ""),
                        stock_name=str(it.get("stkName") or it.get("stockName") or ""),
                        market=str(it.get("market") or ""),
                        action=action,
                        price=to_float(it.get("relocatePrice") or it.get("price") or it.get("avgPrice")),
                        volume=to_float(it.get("relocateQty")),
                        weight_after=_parse_ratio_cn(it.get("positionRatio")),
                        weight_label=str(it.get("positionRatio") or "") or None,  # 原文保留 [2026-09-25]
                        return_rate=exp.return_rate,
                        day_return_rate=to_float((exp.extra or {}).get("daily_gain")),
                        trade_time=str(it.get("relocateTime") or norm),
                        trade_date=norm,
                        market_type=MARKET_LIVE,  # rtV3 即实盘组合调仓 [2026-09-23 打标]
                        raw=it,
                    )
                )
        logger.info(f"[东财] rtV3 扫描关注实盘组合 {len(experts)} 个, 失败 {n_fail}, 当日调仓记录 {len(out)} 条")
        return out

    def _spzh_call(self, method: str, args: dict) -> dict:
        envelope = {
            "args": {"ctToken": self.ct_token, "utToken": self.ut_token, "userId": self.user_id, **args},
            "method": method,
            "client": "android",
            "randomCode": datetime.now().strftime("%H%M%S")
            + str(random.randint(0, 999)).zfill(3)
            + "".join(random.choices(string.ascii_letters + string.digits, k=20)),
            "timestamp": int(time.time() * 1000),
            "deviceId": self.device_id,
            "clientType": "cfw",
            "clientVersion": self.app_version,
        }
        headers = {
            "requestid": datetime.now().strftime("%Y%m%d%H%M%S")
            + "".join(random.choices(string.ascii_letters + string.digits, k=23)),
            "sign": _sm4_sign(envelope),
            "appversion": self.app_version,
            "rnversion": self.rn_version,
            "content-type": "application/json",
            "user-agent": "okhttp/3.12.13",
        }
        # [2026-09-28 修] rtV3 固定走 curl HTTP 客户端: 爬虫默认的无头浏览器后端发这个 POST 会被服务端回 400 Bad Request
        # (盘中实盘扫描因此 0 条); 模拟盘/榜单等 GET 接口仍走共享后端。
        if getattr(self, "_spzh_http", None) is None:
            from core.http import HttpClient
            self._spzh_http = HttpClient(min_interval=0.8)
        return self._spzh_http.post_json(SPZH_URL, envelope, headers=headers)


def _sm4_sign(envelope: dict) -> str:
    """sign = SHA256( hex( SM4-CBC( lowercase(deepSorted JSON) ) ) )。"""
    try:
        from gmssl.sm4 import SM4_ENCRYPT, CryptSM4
    except ImportError as exc:  # pragma: no cover - 仅在配置了 rtV3 令牌时才会走到
        raise RuntimeError("rtV3 签名需要 gmssl，请执行 pip install gmssl") from exc

    plain = json.dumps(_deep_sort(envelope), separators=(",", ":"), ensure_ascii=False).lower()
    sm4 = CryptSM4()
    sm4.set_key(SM4_KEY, SM4_ENCRYPT)
    cipher = sm4.crypt_cbc(SM4_KEY, plain.encode())
    return hashlib.sha256(cipher.hex().encode()).hexdigest()


def _deep_sort(obj):
    if isinstance(obj, dict):
        return {k: _deep_sort(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        return [_deep_sort(x) for x in obj]
    return obj


def _today_str() -> str:
    return date.today().strftime("%Y-%m-%d")


def _as_list(data) -> list:
    """rtV3 返回的明细可能直接是数组，也可能包在 list/items 里。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("list", "items", "records", "pages"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _normalize_date(value) -> str:
    """把 2026-09-10 / 2026/09/10 / 20260910 统一成 YYYY-MM-DD。"""
    text = str(value or "").replace("/", "-")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def _parse_ratio_cn(value) -> float | None:
    """rtV3 positionRatio → 数值档位（百分比形式的**成交金额/组合总资产**，不是调仓后仓位）。

    '3成'→30, '9成以上'→90, '1成以下'→10, '半成'→5。
    ⚠️ 语义（源项目用 19 个带成交量组合核对）：买卖都是「成交金额 / 组合总资产」按成向下取整；
    '1成' 的卖出有 57% 是整只清仓。因此这里存进 crawl_trade.weight_after 的数值**不能**直接
    当目标仓位用于评估，必须先用 strategy/pit_nav.normalize_live 做现金+持仓记账重放
    （买入金额=档位中点×总资产、每笔卖出按清仓），还原真实调仓前后仓位。
    """
    text = str(value or "").strip()
    if not text:
        return None
    if "半" in text:
        return 5.0
    num = to_float(text.replace("成", "").replace("以上", "").replace("以下", ""))
    return num * 10.0 if num is not None else None
