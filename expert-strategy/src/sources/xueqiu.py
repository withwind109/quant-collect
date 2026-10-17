"""雪球：热门组合及其调仓买入明细。

接口（2026-09 实测）：
  * 组合排行榜
    GET https://xueqiu.com/cubes/discover/rank/cube/list.json?category=14&count=20&page=1
    返回字段：symbol(ZHxxxxxx) / name(组合名) / owner_id / follower_count /
              daily_gain(日收益率) / total_gain(累计收益率) /
              updated_at(最后调仓时间戳, ms) / last_rb_id(最后调仓批次 ID) / net_value
    分页上限：totalCount=100、maxPage=5（每页 20），即热门组合池恒定 100 个。
    ⚠️ 实测 category 仅 14 有效（其余取值返回 HTTP 400 或空列表），故组合池无法再扩大。
  * 组合调仓历史（需登录态 Cookie，匿名返回 error_code=10022）
    GET https://xueqiu.com/cubes/rebalancing/history.json?cube_symbol=ZHxxxxxx&count=20&page=1
    返回 rebalancing_histories[]：stock_symbol / stock_name / prev_weight / target_weight /
              prev_price / price / volume，批次时间取 grp.created_at。
    ⚠️ 明细里的 `price` 常为 null（当日盘中未回填），但 `prev_price` 有值，故取两者之一。

覆盖优化：`updated_at` 标记了组合最后一次调仓时间，因此可先用榜单把「当日有调仓」的组合筛出来，
只对这批组合请求调仓历史 —— 100 个组合通常只有 20~30 个当日活跃，请求量下降 70%+，
同时避免旧版「只扫前 50 个组合、整天只有 1 条命中」的问题。

登录态由 .env 的 XUEQIU_COOKIE 提供；未配置时仍产出榜单，但拿不到买入明细。
"""
from __future__ import annotations

from datetime import date

from loguru import logger

from core.http import HttpClient
from core.models import (
    ACTION_ADD,
    ACTION_BUY,
    ACTION_REDUCE,
    ACTION_SELL,
    Expert,
    TradeRecord,
)
from core.utils import normalize_symbol, to_float, ts_to_str
from .base import BaseSource

BASE = "https://xueqiu.com"
RANK_URL = f"{BASE}/cubes/discover/rank/cube/list.json"
REBALANCE_URL = f"{BASE}/cubes/rebalancing/history.json"

#: 组合排行榜分类：14 = 热门组合（实测唯一可用取值）
CATEGORY_HOT = 14
#: 榜单每页条数
RANK_PAGE_SIZE = 20
#: 榜单最大页数（接口 maxPage=5）
RANK_MAX_PAGES = 5


class XueqiuSource(BaseSource):
    name = "xueqiu"

    def __init__(
        self,
        http: HttpClient,
        cookie: str | None = None,
        category: int = CATEGORY_HOT,
        max_pages: int = RANK_MAX_PAGES,
    ) -> None:
        super().__init__(http)
        self.cookie = cookie or ""
        self.category = category
        self.max_pages = max_pages
        self._warmed = False
        #: 全量组合池（含未进入 top-N 展示的组合），供 trades() 按 updated_at 过滤
        self._cubes: list[dict] = []
        #: symbol -> Expert 索引，避免每次取调仓时重复构造
        self._expert_index: dict[str, Expert] = {}
        #: 调仓历史缓存：symbol -> 当日 TradeRecord 列表
        self._trade_cache: dict[str, list[TradeRecord]] = {}

    # ── 内部 ──────────────────────────────────────────────
    @property
    def has_login(self) -> bool:
        return "xq_a_token" in self.cookie

    def _ensure_token(self) -> None:
        """访问 /hq 换取匿名 xq_a_token（榜单接口依赖，首页不给 token）。"""
        if self._warmed:
            return
        self.http.warm(f"{BASE}/hq")
        self._warmed = True

    def _headers(self, referer: str = f"{BASE}/hq") -> dict[str, str]:
        headers = {"Referer": referer, "X-Requested-With": "XMLHttpRequest"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    # ── 榜单 ──────────────────────────────────────────────
    def _fetch_cubes(self) -> list[dict]:
        """拉取全部热门组合（去重），缓存到 self._cubes。

        组合池固定 100 个，与 CLI 的 --top 解耦：`--top 20` 只影响榜单展示条数，
        不影响调仓扫描范围（否则又会退化成「只扫前 N 个组合」）。
        """
        if self._cubes:
            return self._cubes
        self._ensure_token()
        cubes: list[dict] = []
        seen: set[str] = set()
        for page in range(1, max(1, self.max_pages) + 1):
            data = self.http.get_json(
                RANK_URL,
                params={"category": self.category, "count": RANK_PAGE_SIZE, "page": page},
                headers=self._headers(),
            )
            items = (data or {}).get("list") or []
            if not items:
                break
            for it in items:
                symbol = str(it.get("symbol") or "")
                if not symbol or symbol in seen:
                    continue
                seen.add(symbol)
                cubes.append(it)
            if len(items) < RANK_PAGE_SIZE:
                break
        self._cubes = cubes
        self._expert_index = {e.expert_id: e for e in (self._cube_to_expert(it) for it in cubes)}
        logger.info(f"[雪球] 热门组合池 {len(cubes)} 个（category={self.category}, {self.max_pages} 页）")
        return cubes

    @staticmethod
    def _cube_to_expert(it: dict) -> Expert:
        owner = it.get("owner") or {}
        return Expert(
            source="xueqiu",
            channel="雪球热门组合",
            expert_id=str(it.get("symbol", "")),
            name=str(it.get("name", "")),
            owner=str(owner.get("screen_name") or it.get("owner_name") or ""),
            rank_label="热门组合榜",
            followers=int(it.get("follower_count") or 0),
            # 累计收益率优先（回答「他赚了多少」）；日收益另存 day_return_rate
            return_rate=to_float(it.get("total_gain")),
            total_return=to_float(it.get("total_gain")),
            extra={
                "net_value": it.get("net_value"),
                "daily_gain": to_float(it.get("daily_gain")),
                "monthly_gain": to_float(it.get("monthly_gain")),
                "annualized_gain_rate": it.get("annualized_gain_rate"),
                "owner_id": it.get("owner_id"),
                "updated_at": it.get("updated_at"),
                "last_rb_id": it.get("last_rb_id"),
                "updated_date": ts_to_str(it.get("updated_at"), "%Y-%m-%d"),
            },
        )

    def hot_experts(self, limit: int = 50) -> list[Expert]:
        cubes = self._fetch_cubes()
        experts = [self._cube_to_expert(it) for it in cubes]
        experts.sort(key=lambda e: e.return_rate or -1e9, reverse=True)
        logger.info(f"[雪球] 返回收益榜 {min(len(experts), limit)} / {len(experts)} 个组合")
        return experts[:limit]

    def candidate_dates(self) -> dict[date, int]:
        """组合池 updated_at 的日期分布（最近 3 天），值为当日有调仓的组合数。"""
        hist: dict[date, int] = {}
        for it in self._fetch_cubes():
            text = ts_to_str(it.get("updated_at"), "%Y-%m-%d")
            if not text:
                continue
            try:
                day = date.fromisoformat(text)
            except ValueError:
                continue
            hist[day] = hist.get(day, 0) + 1
        recent = sorted(hist, reverse=True)[:3]
        return {d: hist[d] for d in recent}

    def _active_symbols(self, target_date: str) -> list[str]:
        """筛出 updated_at 落在 target_date 的组合（榜单页已携带该字段，无需额外请求）。"""
        out: list[str] = []
        for it in self._fetch_cubes():
            if ts_to_str(it.get("updated_at"), "%Y-%m-%d") == target_date:
                out.append(str(it.get("symbol")))
        return out

    # ── 调仓明细 ──────────────────────────────────────────
    def trades(self, experts: list[Expert], trade_date: date) -> list[TradeRecord]:
        if not self.has_login:
            logger.warning(
                "[雪球] 未配置 XUEQIU_COOKIE（需登录态），跳过调仓明细；榜单数据仍会输出。"
            )
            return []

        target = trade_date.strftime("%Y-%m-%d")
        symbols = self._active_symbols(target)
        logger.info(f"[雪球] {target} 有调仓的组合 {len(symbols)} 个（组合池 {len(self._cubes)} 个）")
        records: list[TradeRecord] = []
        for symbol in symbols:
            try:
                records.extend(self._cube_trades(symbol, target))
            except Exception as exc:
                logger.warning(f"[雪球] 组合 {symbol} 调仓抓取失败：{exc}")
        logger.info(f"[雪球] 当日({target})调仓记录 {len(records)} 条")
        return records

    def _cube_trades(self, symbol: str, target_date: str) -> list[TradeRecord]:
        cached = self._trade_cache.get(symbol)
        if cached is not None:
            return cached
        exp = self._expert_index.get(symbol)
        out: list[TradeRecord] = []
        for page in (1, 2):
            data = self.http.get_json(
                REBALANCE_URL,
                params={"cube_symbol": symbol, "count": 20, "page": page},
                headers=self._headers(f"{BASE}/P/{symbol}"),
            )
            groups = (data or {}).get("list") or []
            if not groups:
                break
            stop = False
            for grp in groups:
                created = ts_to_str(grp.get("created_at"))
                if not created.startswith(target_date):
                    # 记录按时间倒序，早于目标日即可停止翻页
                    if created and created[:10] < target_date:
                        stop = True
                        break
                    continue
                for item in grp.get("rebalancing_histories") or []:
                    code, market = normalize_symbol(item.get("stock_symbol", ""))
                    prev = to_float(item.get("prev_weight")) or 0.0
                    tgt = to_float(item.get("target_weight")) or 0.0
                    if prev == 0.0 and tgt > 0:
                        action = ACTION_BUY
                    elif tgt > prev:
                        action = ACTION_ADD
                    elif tgt == 0 and prev > 0:
                        action = ACTION_SELL
                    else:
                        action = ACTION_REDUCE
                    out.append(
                        TradeRecord(
                            source=self.name,
                            channel="雪球组合调仓",
                            expert_id=symbol,
                            expert_name=(exp.name if exp else symbol),
                            owner=(exp.owner if exp else ""),
                            stock_code=code,
                            stock_name=item.get("stock_name", ""),
                            market=market,
                            action=action,
                            # 明细的 price 常为 null，回退用 prev_price（上一交易日收盘价）
                            price=to_float(item.get("price")) or to_float(item.get("prev_price")),
                            weight_before=prev,
                            weight_after=tgt,
                            return_rate=(exp.return_rate if exp else None),
                            day_return_rate=to_float((exp.extra or {}).get("daily_gain")) if exp else None,
                            trade_time=created,
                            trade_date=created[:10],
                            raw=item,
                        )
                    )
            if stop:
                break
        self._trade_cache[symbol] = out
        return out
