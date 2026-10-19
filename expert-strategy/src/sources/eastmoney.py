"""东方财富：社区实盘/模拟组合高手榜 + 当日买入明细。

接口（2026-09 实测，除 rtV3 外均匿名可用）：
  1. 高手操作明细（含成交时间/买卖方向/成交价/调仓前后仓位/组合名/收益率，精确到分钟）
     GET https://simqry2.eastmoney.com/qry_tzzh_v2
         ?type=spo_superior_detail&rankType=30001&recIdx=1&recCnt=100&plat=2&ver=web20
     ⚠️ recIdx 从 **1** 起（传 0 返回 HTTP 500）；totalCnt 上限 1000，即只保留最近 1000 笔操作。
     返回字段（实测 2026-09-12）：
         cjsj        "09-11 15:00"  成交时间（月-日 时:分，**不含年份**）
         zjzh        组合账号（同一组合的稳定 ID）
         zuheName    组合名称（= 操作者身份标识）
         userid      用户 ID
         stkCode/stkName/mktCode  股票代码 / 名称 / 市场（1=沪 0=深 2=北）
         mmbz        买卖方向（"买入" / "卖出"）
         price       成交价
         holdPosBef/holdPosAft    调仓前/后仓位(%)
         zsyl        该组合**累计收益率(%)**      -> return_rate
         syl_dr      该组合**当日收益率(%)**      -> day_return_rate
         rank/superiorTypeString   榜位 / 榜单类型（"总收益"）
  2. 组合买入热度榜（站点自带聚合，按多少高手买入排序，**无高手身份**）
     GET 同上 ?type=spo_rank_tiger&rankType=30001&timePeriod=1&recIdx=1&recCnt=50
     rankType: 30001=买入榜, 30002=卖出榜；timePeriod: 1=当日
  3. 实盘组合选手榜
     GET https://emdcspzhapi.dfcfs.cn/rtV1
         ?type=rt_get_rank&rankType=10005&recIdx=0&recCnt=20&rankid=0&appVer=9001000
  4. 实盘组合调仓明细（可选，需 APP 会话令牌 + SM4 签名）
     POST https://spzhapi.dfcfs.cn/rtV3  method=CombinationRelocatePositionHandler

数据视角说明：
  * `spo_superior_detail` 是**全站高手操作流水**（按时间倒序），单日可达 800+ 笔买入，
    覆盖数百个组合与数百只股票 —— 这是「谁（组合名）+ 收益率 + 买了哪只股票」的权威来源。
  * `spo_rank_tiger` 只给「哪只股票被多少高手买入」的聚合数字，**拿不到名单**，
    仅作热度交叉验证，不应作为主视图（这是旧版页面「只知道股票不知道谁买」的原因）。

认证方式（2026-10-16 迁移版）：
  * 不再使用扫码登录。东财各接口（spo_* / rtV1）匿名即可抓取；配置登录态 Cookie
    （config.yaml 的 eastmoney.cookie 或 .env 的 EASTMONEY_COOKIE）后随请求带上，
    可降低被 WAF 拦截的概率、提高限额。
  * rtV3 实盘调仓明细仍需 APP 会话令牌（ctToken/utToken/userId + SM4 签名），
    独立版默认不接入（无扫码登录态）；实盘组合走「网站榜单」即可。
  * 多渠道扩展: 新数据源只需继承 BaseSource 并在 pipeline.build_sources 注册。
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import string
import time
from datetime import date, datetime

from loguru import logger

from core.http import HttpClient
from core.models import (
    ACTION_BUY,
    ACTION_SELL,
    Expert,
    StockAggregate,
    TradeRecord,
)
from core.utils import MARKET_LIVE, MARKET_SIM, normalize_symbol, to_float, to_int
from .base import BaseSource

SIM_URL = "https://simqry2.eastmoney.com/qry_tzzh_v2"
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

#: 高手操作明细单页条数（接口实测上限 100）
DETAIL_PAGE_SIZE = 100
#: 接口最多保留的记录数（totalCnt 恒为 1000）
DETAIL_MAX_TOTAL = 1000

#: mktCode -> 市场
_MKT_CODE = {"1": "SH", "0": "SZ", "2": "BJ"}

#: "09-11 15:00" 形态的成交时间
_CJSJ_RE = re.compile(r"^(\d{2})-(\d{2})\s+(\d{2}):(\d{2})")

SM4_KEY = bytes.fromhex("e4dd41fd138867c3665492702fe277eb")


def parse_cjsj(cjsj: str, today: date | None = None) -> tuple[date, str] | None:
    """把东财的 `"09-11 15:00"`（**不含年份**）解析为 (date, "YYYY-MM-DD HH:MM")。

    年份推断：先用 today 的年份，若结果比 today 晚 90 天以上，判定为上一年的记录
    （接口按时间倒序返回，遇到跨年时会同时出现 12-31 与 01-02）。
    """
    m = _CJSJ_RE.match((cjsj or "").strip())
    if not m:
        return None
    today = today or date.today()
    mm, dd, hh, mi = (int(x) for x in m.groups())
    try:
        day = date(today.year, mm, dd)
    except ValueError:
        return None
    if (day - today).days > 90:
        try:
            day = date(today.year - 1, mm, dd)
        except ValueError:
            return None
    return day, f"{day.isoformat()} {hh:02d}:{mi:02d}"


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
    supports_stock_rank = True

    def __init__(
        self,
        http: HttpClient,
        rank_type: str = "10005",
        tiger_time_period: int = 1,
        max_records: int = DETAIL_MAX_TOTAL,
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
        self.tiger_time_period = tiger_time_period
        self.max_records = max_records
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
        #: 操作明细分页缓存：resolve_trade_date 与 trades 共用，避免重复请求首页
        self._detail_cache: dict[int, list] = {}

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
        label = RANK_TYPES.get(self.rank_type, self.rank_type)
        experts: list[Expert] = []
        rec_idx, page_size = 0, min(20, max(limit, 1))
        while len(experts) < limit:
            data = self.http.get_json(
                LIVE_RANK_URL,
                params={
                    "type": "rt_get_rank",
                    "rankType": self.rank_type,
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
                # ⚠️ rtV1 的 rateForApp 语义取决于 rankType：rankType=10005（日榜）时
                # rateTitle="日收益"，是**当日**收益率而非累计收益率，不能混入「累计收益率」列。
                # 累计收益率以 spo_superior_detail 的 zsyl（总收益）为准。
                title = str(it.get("rateTitle") or "")
                rate = to_float(it.get("rateForApp"))
                is_daily = "日" in title
                experts.append(
                    Expert(
                        source=self.name,
                        channel="东财实盘组合榜",
                        expert_id=str(it.get("zjzh", "")),
                        name=str(it.get("zhuheName", "")),
                        owner=str(it.get("uidNick", "")),
                        rank_label=f"东财实盘{label}",
                        followers=to_int(it.get("concernCnt")),
                        return_rate=None if is_daily else rate,
                        market_type=MARKET_LIVE,  # rtV1 即实盘榜 [2026-09-23 打标]
                        extra={
                            "userid": it.get("userid"),
                            "labels": [it.get(f"label{i}") for i in (1, 2, 3) if it.get(f"label{i}")],
                            "rate_title": title,
                            "rate_value": rate,
                            "daily_gain": rate if is_daily else None,
                        },
                    )
                )
                if len(experts) >= limit:
                    break
            if len(items) < page_size:
                break
            rec_idx += len(items)
        logger.info(f"[东财] 实盘榜({label}) 选手 {len(experts)} 位")
        return experts

    # ── 2. 当日买入明细 ───────────────────────────────────
    def trades(self, experts: list[Expert], trade_date: date) -> list[TradeRecord]:
        target = trade_date.strftime("%Y-%m-%d")
        records = self._superior_trades(target)
        if self.has_spzh_token:
            # [2026-09-28 修] rtV3 只对本账户关注的组合开放。以前 = 榜单 50 人 + 关注组合排在后面, 且只查前 50 个 →
            # 关注组合永远被截掉; 榜单组合 (没关注) 第一个返回失败就 break → 盘中实盘记录恒为 0。
            # 现在只查关注组合 (榜单组合查了也拿不到), 关注组合按 expert_id 用榜单上的完整信息
            by_id = {e.expert_id: e for e in experts}
            follow = [by_id.get(e.expert_id, e) for e in self.spzh_follow]
            records.extend(self._spzh_trades(follow, target))
        return records

    def candidate_dates(self) -> dict[date, int]:
        """扫描操作明细前几页，统计各成交日的记录数（记录按时间倒序）。"""
        today = date.today()
        hist: dict[date, int] = {}
        for rec_idx in (1, 2, 3):
            items = self._detail_page(rec_idx)
            if not items:
                break
            for it in items:
                parsed = parse_cjsj(str(it.get("cjsj") or ""), today)
                if parsed:
                    hist[parsed[0]] = hist.get(parsed[0], 0) + 1
            if len(items) < DETAIL_PAGE_SIZE:
                break
        return hist

    def _detail_page(self, rec_idx: int) -> list:
        """取操作明细第 rec_idx 页（1-based），带实例级缓存。"""
        if rec_idx in self._detail_cache:
            return self._detail_cache[rec_idx]
        data = self.http.get_json(
            SIM_URL,
            params={
                "type": "spo_superior_detail",
                "rankType": "30001",
                "recIdx": rec_idx,
                "recCnt": DETAIL_PAGE_SIZE,
                "plat": 2,
                "ver": "web20",
            },
            headers=self._headers(),
        )
        items = (data or {}).get("data") or []
        if items:
            self._detail_cache[rec_idx] = items
        return items

    def _superior_trades(self, target_date: str) -> list[TradeRecord]:
        """高手操作明细：逐页拉取全站操作流水，筛出 target_date 的成交。

        记录按时间倒序，一旦出现早于目标日的记录即可收尾（同一页内可能混有两日数据，
        故收尾标记在整页处理完后才生效）。
        """
        out: list[TradeRecord] = []
        rec_idx = 1
        today = date.today()
        pages = 0
        while len(out) < self.max_records and pages < DETAIL_MAX_TOTAL // DETAIL_PAGE_SIZE:
            items = self._detail_page(rec_idx)
            if not items:
                break
            pages += 1
            passed_target = False
            for it in items:
                parsed = parse_cjsj(str(it.get("cjsj") or ""), today)
                if parsed is None:
                    continue
                day, stamp = parsed
                ds = day.isoformat()
                if ds < target_date:
                    passed_target = True
                    continue
                if ds != target_date:
                    continue
                code = str(it.get("stkCode", ""))
                mkt = str(it.get("mktCode", ""))
                out.append(
                    TradeRecord(
                        source=self.name,
                        channel="东财组合高手操作",
                        expert_id=str(it.get("zjzh", "")),
                        expert_name=str(it.get("zuheName", "")),
                        owner=str(it.get("zuheName", "")),
                        stock_code=code,
                        stock_name=str(it.get("stkName", "")),
                        market=_MKT_CODE.get(mkt) or (normalize_symbol(code)[1] or ""),
                        action=_normalize_action(it.get("mmbz")),
                        price=to_float(it.get("price")),
                        weight_before=to_float(it.get("holdPosBef")),
                        weight_after=to_float(it.get("holdPosAft")),
                        return_rate=to_float(it.get("zsyl")),
                        day_return_rate=to_float(it.get("syl_dr")),
                        trade_time=stamp,
                        trade_date=ds,
                        market_type=MARKET_SIM,  # spo_* 全站流水即模拟盘 [2026-09-23 打标]
                        raw=it,
                    )
                )
            if passed_target or len(items) < DETAIL_PAGE_SIZE:
                break
            rec_idx += 1
        logger.info(
            f"[东财] 当日({target_date})高手操作明细 {len(out)} 条"
            f"（扫描 {pages} 页 / {pages * DETAIL_PAGE_SIZE} 条流水）"
        )
        return out

    # ── 3. 站点自带聚合买入榜（无高手身份）────────────────────
    def hot_buy_stocks(self, limit: int = 50) -> list[StockAggregate]:
        """站点自带的「股票买入热度榜」：只有股票 + 买入高手数，**不含名单**。

        仅作热度交叉验证；主视图请使用 `trades()` 产出的「组合名 + 收益率 + 股票」明细。
        """
        data = self.http.get_json(
            SIM_URL,
            params={
                "type": "spo_rank_tiger",
                "rankType": 30001,
                "timePeriod": self.tiger_time_period,
                "recIdx": 1,
                "recCnt": limit,
                "plat": 2,
                "ver": "web20",
            },
            headers=self._headers(),
        )
        items = (data or {}).get("data") or []
        today = datetime.now().strftime("%Y-%m-%d")
        return [
            StockAggregate(
                stock_code=str(it.get("stkCode", "")),
                stock_name=str(it.get("stkName", "")),
                buy_experts=to_int(it.get("zuheBuyIn")),
                buy_records=to_int(it.get("zuheBuyIn")),
                sources=["eastmoney:股票买入热度榜"],
                avg_price=to_float(it.get("avgBuyPrice")),
                latest_time=today,
                note="站点全量聚合口径，不含买入高手名单",
            )
            for it in items
        ]

    # ── 4.5 单组合调仓全历史（2026-09-23 实测匿名可用，方案 P1）──
    def portfolio_history(
        self, zjzh: str, since: str = "", max_pages: int = 100
    ) -> list[TradeRecord]:
        """翻页拉取单个**模拟盘**组合的全部调仓记录。

        接口 ``spo_hldchg_detail``（web 版组合详情页同款）：
          GET simqry2.../qry_tzzh_v2?type=spo_hldchg_detail&plat=2&ver=web20&zjzh=...&recIdx=N&recCnt=100
        返回字段：cjsj(完整 "2026-09-23 15:00") / cjjg(成交价) / mmbz("买"/"卖" 单字) /
        stkCode / stkName / fullcode / holdPosBef / holdPosAft。响应带 totalCnt 总数。

        ⚠️ zjzh 必须是模拟盘组合号（1000… 长号）；实盘组合号（9 开头短号）会报
        "组合号错误"——实盘明细只能走 rtV3（需 APP 令牌）。
        since: "YYYY-MM-DD"，遇到早于该日的记录即停止翻页；空 = 拉到接口尽头。
        max_pages: 翻页上限兜底（100 页 × 100 条 = 1 万条，覆盖绝大多数组合全历史）。
        """
        out: list[TradeRecord] = []
        page_size = 100
        page = 1  # ⚠️ recIdx 是「页码」而非记录偏移（实测 recCnt=3 时 recIdx=201 返回第 603 条附近的记录）
        total_cnt: int | None = None
        pages_fetched = 0
        while page <= max_pages:
            data = self.http.get_json(
                SIM_URL,
                params={
                    "type": "spo_hldchg_detail",
                    "plat": 2,
                    "ver": "web20",
                    "zjzh": zjzh,
                    "recIdx": page,
                    "recCnt": page_size,
                },
                headers=self._headers(),
            )
            items = (data or {}).get("data") or []
            if total_cnt is None:
                try:
                    total_cnt = int((data or {}).get("totalCnt") or 0)
                except (TypeError, ValueError):
                    total_cnt = None
            if not items:
                break
            pages_fetched += 1
            stop = False
            for it in items:
                cjsj = str(it.get("cjsj") or "")  # "2026-09-23 15:00" 完整含年份
                day = cjsj[:10]
                if since and day and day < since:
                    stop = True
                    continue
                code = str(it.get("stkCode", ""))
                out.append(
                    TradeRecord(
                        source=self.name,
                        channel="东财组合调仓历史",
                        expert_id=zjzh,
                        expert_name="",  # 本接口不带组合名，由调用方用组合基础信息补
                        owner="",
                        stock_code=code,
                        stock_name=str(it.get("stkName", "")),
                        market=_MKT_CODE.get(str(it.get("mktCode", ""))) or (normalize_symbol(code)[1] or ""),
                        action=_normalize_action(it.get("mmbz")),
                        price=to_float(it.get("cjjg") or it.get("cjjg2")),
                        weight_before=to_float(it.get("holdPosBef")),
                        weight_after=to_float(it.get("holdPosAft")),
                        trade_time=cjsj,
                        trade_date=day,
                        market_type=MARKET_SIM,  # spo_* 网关仅覆盖模拟盘组合
                        raw=it,
                    )
                )
            if stop or len(items) < page_size:
                break
            page += 1
        logger.info(
            f"[东财] 组合 {zjzh} 调仓历史 {len(out)} 条"
            f"（接口 totalCnt={total_cnt}，抓取 {pages_fetched} 页）"
        )
        return out

    def portfolio_basic(self, zjzh: str) -> dict:
        """组合基础信息（名称/管理人/累计收益等），spo_zuhe_detail_basic。

        匿名可用（实测 2026-09-23，仅限模拟盘组合号）；失败返回 {} 不阻塞主流程。
        """
        try:
            data = self.http.get_json(
                SIM_URL,
                params={
                    "type": "spo_zuhe_detail_basic",
                    "plat": 2,
                    "ver": "web20",
                    "zjzh": zjzh,
                },
                headers=self._headers(),
            )
            items = (data or {}).get("data") or []
            return items[0] if isinstance(items, list) and items else (items or {})
        except Exception as exc:
            logger.warning(f"[东财] 组合基础信息获取失败 {zjzh}：{exc}")
            return {}

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
    """解析 rtV3 的中文仓位描述: '3成'→30, '9成以上'→90, '1成以下'→10, '半成'→5。"""
    text = str(value or "").strip()
    if not text:
        return None
    if "半" in text:
        return 5.0
    num = to_float(text.replace("成", "").replace("以上", "").replace("以下", ""))
    return num * 10.0 if num is not None else None
