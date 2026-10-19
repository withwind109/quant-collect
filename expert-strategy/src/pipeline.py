"""采集编排：抓高手 → 抓当日买入明细 → 按个股聚合 → 落盘 → SQLite 入库。

迁移自 xcq-platform/experts_crawl/src/pipeline.py（2026-10-16）：
  * 去掉平台耦合（scripts.core.db / token_provider / live_scan_sync），
    实盘 rtV3 令牌与关注组合常驻扫描名单只来自 config.yaml / .env；
  * MySQL 入库换成本地 SQLite（src/core/db.py）；
  * 其余逻辑（交易日投票、覆盖度统计、盈利排序、聚合口径）与原版逐行一致。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from loguru import logger

from core.db import ensure_schema as _db_ensure_schema
from core.db import write_day as _db_write_day
from core.http import CHROME_UA, HttpClient
from paths import CRAWL_DATA_DIR
from core.models import BUY_ACTIONS, Expert, StockAggregate, TradeRecord
from core.storage import AGG_COLUMNS, EXPERT_BUY_COLUMNS, EXPERT_COLUMNS, TRADE_COLUMNS, write_csv, write_excel, write_json
from settings import sqlite_path
from sources.alipay import AlipaySource
from sources.base import BaseSource
from sources.tonghuashun import TonghuashunSource
from sources.xueqiu import XueqiuSource

#: 各数据源「能拿到什么」的固定说明，随覆盖度一起落盘，供前端做可用性提示
SOURCE_CAPABILITY = {
    "eastmoney": "组合高手操作明细：组合名 + 累计收益率 + 当日收益率 + 股票 + 成交价 + 仓位变化",
    "xueqiu": "热门组合调仓：组合名 + 累计收益率 + 股票 + 仓位变化（需 XUEQIU_COOKIE）",
    "tonghuashun": "模拟炒股百强榜（收益率）+ 高手买卖动态；逐笔股票代码需登录且「追踪」该高手后才公开",
    "alipay": "蚂蚁财富实盘榜：需自行抓包提供端点",
}


def aggregate_buys(trades: list[TradeRecord]) -> list[StockAggregate]:
    """按个股聚合买入热度（仅统计买入/加仓）。"""
    buckets: dict[tuple[str, str], list[TradeRecord]] = defaultdict(list)
    for t in trades:
        if t.action in BUY_ACTIONS and t.stock_code:
            buckets[(t.stock_code, t.asset_type)].append(t)

    result: list[StockAggregate] = []
    for (code, atype), items in buckets.items():
        prices = [i.price for i in items if i.price]
        amount = sum(i.amount or 0.0 for i in items) or None
        result.append(
            StockAggregate(
                stock_code=code,
                asset_type=atype,
                stock_name=next((i.stock_name for i in items if i.stock_name), ""),
                buy_experts=len({(i.source, i.expert_id) for i in items}),
                buy_records=len(items),
                sources=sorted({f"{i.source}:{i.channel}" for i in items}),
                avg_price=round(sum(prices) / len(prices), 3) if prices else None,
                amount=amount,
                latest_time=max((i.trade_time for i in items), default=""),
            )
        )
    result.sort(key=lambda x: (x.buy_experts, x.buy_records), reverse=True)
    return result


def _collect_cookies(settings: dict) -> list[tuple[str, str]]:
    """汇总各源登录 Cookie（domain, cookie_str），供浏览器上下文预置会话。

    [2026-10-16] 新增东财 Cookie（.eastmoney.com）。
    """
    out: list[tuple[str, str]] = []
    em = settings.get("eastmoney", {}).get("cookie")
    if em:
        out.append((".eastmoney.com", em))
    xq = settings.get("xueqiu", {}).get("cookie")
    if xq:
        out.append((".xueqiu.com", xq))
    ths = settings.get("tonghuashun", {}).get("cookie")
    if ths:
        out.append((".10jqka.com.cn", ths))
    ali = settings.get("alipay", {}).get("cookie")
    if ali:
        out.append((".alipay.com", ali))
    return out


def _build_fetcher(settings: dict, force_http: bool = False):
    """构建抓取后端：优先无头浏览器（拟人化反爬），失败回退 curl_cffi。

    force_http=True 直接跳过浏览器（批量回补任务专用，避免浏览器启动+预热
    白白开销），见 build_sources 注释。
    """
    browser_cfg = settings.get("browser", {})
    if not force_http and browser_cfg.get("enabled"):
        try:
            from core.browser import BrowserClient

            return BrowserClient(
                headless=browser_cfg.get("headless", True),
                stealth=browser_cfg.get("stealth", True),
                min_interval=browser_cfg.get("min_interval", 1.2),
                timeout=browser_cfg.get("timeout", 30000),
                cookies=_collect_cookies(settings),
            )
        except Exception as exc:
            logger.warning(f"无头浏览器启动失败，回退到 curl_cffi：{exc}")
    http_cfg = settings.get("http", {})
    if force_http:
        logger.info("[pipeline] force_http=True，批量任务走 curl_cffi 轻量后端")
    return HttpClient(
        min_interval=http_cfg.get("min_interval", 0.8),
        timeout=http_cfg.get("timeout", 20),
        ua=CHROME_UA,
        proxy=http_cfg.get("proxy") or None,
    )


def _build_eastmoney(fetcher, settings: dict, em_cfg: dict | None = None):
    """构造东财数据源（build_sources 与 history/bulk_history 共用，参数只此一处维护）。

    em_cfg: build_sources 里已合并关注列表（config ∪ 本地关注表）后的东财配置；
    传 None 时从 settings 重新读取（history/bulk_history 场景，不需要关注合并）。
    """
    from sources.eastmoney import EastmoneySource

    em_cfg = dict(em_cfg if em_cfg is not None else settings.get("eastmoney", {}))
    # 实盘 rtV3 令牌优先级（对齐 xcq-platform pipeline 2026-09-25 账户中心方案）:
    # 登录态账户(broker_accounts 表) > config/env。config/env 三要素没配全时才尝试
    # 从账户中心取, 取不到则保持空值(实盘明细自动跳过, 模拟盘不受影响)。
    if not (em_cfg.get("spzh_ct_token") and em_cfg.get("spzh_ut_token") and em_cfg.get("spzh_user_id")):
        try:
            from core.broker_account import active_tokens
            _tp = active_tokens("eastmoney") or {}
            for k_src, k_cfg in (("ctToken", "spzh_ct_token"), ("utToken", "spzh_ut_token"),
                                 ("userId", "spzh_user_id"), ("deviceId", "spzh_device_id")):
                if _tp.get(k_src) and not em_cfg.get(k_cfg):
                    em_cfg[k_cfg] = _tp[k_src]
        except Exception as _tp_e:  # 令牌供给失败不阻塞爬取（模拟盘不受影响）
            logger.warning(f"[pipeline] 账户中心令牌供给失败(非阻塞): {_tp_e}")
    return EastmoneySource(
        fetcher,
        rank_type=str(em_cfg.get("rank_type", "10005")),
        tiger_time_period=int(em_cfg.get("tiger_time_period", 1)),
        max_records=int(em_cfg.get("max_records", 400)),
        cookie=em_cfg.get("cookie", "") or "",
        spzh_ct_token=em_cfg.get("spzh_ct_token", "") or "",
        spzh_ut_token=em_cfg.get("spzh_ut_token", "") or "",
        spzh_user_id=em_cfg.get("spzh_user_id", "") or "",
        spzh_device_id=em_cfg.get("spzh_device_id", "") or "",
        spzh_app_version=em_cfg.get("spzh_app_version", "11.2.2") or "11.2.2",
        spzh_rn_version=em_cfg.get("spzh_rn_version", "2.3.0.260731172423109"),
        spzh_follow=em_cfg.get("spzh_follow") or [],
    )


def build_sources(settings: dict, force_http: bool = False) -> dict[str, BaseSource]:
    """按配置构建数据源客户端（共享同一个抓取后端：浏览器或 curl_cffi）。

    多渠道扩展: 新数据源在 src/sources/ 下继承 BaseSource 实现三个方法，
    在这里注册一行即可（CLI --sources 与页面筛选自动生效）。

    force_http=True 时强制走 curl_cffi 轻量后端（跳过浏览器）—— 用于
    history/bulk_history 这类批量回补任务：组合数 × 翻页数远超日常爬取的
    请求量，浏览器限速（默认 1.2s/请求）下跑不完；且 spo_hldchg_detail 是
    匿名接口，无 WAF 挑战。
    """
    fetcher = _build_fetcher(settings, force_http=force_http)
    em_cfg = settings.get("eastmoney", {})
    xq_cfg = settings.get("xueqiu", {})
    ths_cfg = settings.get("tonghuashun", {})
    ali_cfg = settings.get("alipay", {})
    # 关注实盘组合常驻扫描 [2026-09-25]: config.yaml spzh_follow ∪ DB关注列表
    # (watchlist 中 market_type='live' 的条目, 由 UI 关注操作 / 东财关注同步维护)
    # 合并去重, expert_id 为准。
    _follow = [dict(x) for x in (em_cfg.get("spzh_follow") or [])
               if isinstance(x, dict) and x.get("expert_id")]
    try:
        from .strategy.watchlist import list_watch
        _seen = {str(x.get("expert_id")) for x in _follow}
        for _r in list_watch("live") or []:
            if str(_r.get("expert_id")) not in _seen:
                _follow.append({"expert_id": str(_r.get("expert_id")),
                                "name": str(_r.get("name") or "")})
    except Exception as _wl_e:
        logger.warning(f"[pipeline] 本地关注列表读取失败, 退回 config.yaml 静态列表: {_wl_e}")
    em_cfg["spzh_follow"] = _follow
    return {
        "xueqiu": XueqiuSource(
            fetcher,
            cookie=xq_cfg.get("cookie") or None,
            category=int(xq_cfg.get("category", 14)),
            max_pages=int(xq_cfg.get("max_pages", 5)),
        ),
        "eastmoney": _build_eastmoney(fetcher, settings, em_cfg),
        "tonghuashun": TonghuashunSource(
            fetcher,
            cookie=ths_cfg.get("cookie") or None,
            hexin_v=ths_cfg.get("hexin_v") or None,
        ),
        "alipay": AlipaySource(
            fetcher,
            rank_url=ali_cfg.get("rank_url") or None,
            trade_url=ali_cfg.get("trade_url") or None,
            cookie=ali_cfg.get("cookie") or None,
        ),
    }


def build_expert_buys(buys: list[TradeRecord], expert_map: dict) -> list[dict]:
    """把买入记录按高手分组，整体按高手收益率（盈利）降序排列。

    返回 list[dict]，每项：
        source / asset_type / channel / expert_id / expert_name / owner / rank_label /
        return_rate(累计收益率) / day_return_rate(当日收益率) / followers /
        buy_count / stocks[{stock_code, stock_name, action, price, volume, amount,
                            weight_before, weight_after, trade_time}]
    """
    groups: dict[tuple[str, str], list[TradeRecord]] = defaultdict(list)
    for t in buys:
        if t.expert_id:
            groups[(t.source, t.expert_id)].append(t)

    rows: list[dict] = []
    for (src, eid), items in groups.items():
        exp = expert_map.get((src, eid))
        # 高手榜里没有该 ID 时（如东财操作明细的组合不在 rtV1 榜内），
        # 直接以操作记录自身携带的身份与收益率兜底 —— 保证「谁买的」永不为空。
        first = items[0]
        rows.append(
            {
                "source": src,
                "asset_type": (exp.asset_type if exp else first.asset_type) or "stock",
                "channel": (exp.channel if exp else first.channel) or "",
                "expert_id": eid,
                "expert_name": (exp.name if exp else "") or first.expert_name or (exp.owner if exp else ""),
                "owner": (exp.owner if exp else "") or first.owner,
                "rank_label": (exp.rank_label if exp else "") or "当日活跃组合",
                "market_type": (exp.market_type if exp else "") or first.market_type or "sim",
                "return_rate": (exp.return_rate if exp else None) if (exp and exp.return_rate is not None) else first.return_rate,
                "day_return_rate": first.day_return_rate
                if first.day_return_rate is not None
                else ((exp.extra or {}).get("daily_gain") if exp else None),
                "followers": exp.followers if exp else 0,
                "buy_count": len(items),
                "stocks": [
                    {
                        "stock_code": i.stock_code,
                        "stock_name": i.stock_name,
                        "action": i.action,
                        "price": i.price,
                        "volume": i.volume,
                        "amount": i.amount,
                        "weight_before": i.weight_before,
                        "weight_after": i.weight_after,
                        "trade_time": i.trade_time,
                    }
                    for i in items
                ],
            }
        )
    rows.sort(key=lambda r: (r["return_rate"] or -1e9, r["day_return_rate"] or -1e9), reverse=True)
    return rows


def _collect_active_experts(trades: list[TradeRecord]) -> list[Expert]:
    """从当日操作明细提取『当日活跃高手』并补齐收益率，使其进入盈利排行榜、与买入自洽。

    东财高手操作明细(spo_superior_detail)自带 zsyl（累计收益率）与 syl_dr（当日收益率），
    与 rtV1 实盘榜是不同 ID 体系，故不依赖 hot_experts 的榜单，直接用操作记录本身聚合。
    """
    seen: dict[tuple[str, str], Expert] = {}
    for t in trades:
        if not t.expert_id:
            continue
        key = (t.source, t.expert_id)
        if key in seen:
            continue
        seen[key] = Expert(
            source=t.source,
            asset_type=t.asset_type,
            channel=t.channel,
            expert_id=t.expert_id,
            name=t.expert_name or t.owner,
            owner=t.owner,
            rank_label="当日活跃组合",
            return_rate=t.return_rate,
            market_type=t.market_type,  # 随操作记录的实盘/模拟盘打标 [2026-09-23]
            extra={"daily_gain": t.day_return_rate},
        )
    return list(seen.values())


def resolve_trade_date(clients: dict[str, BaseSource], sources: list[str]) -> tuple[date, dict]:
    """投票选出目标交易日：源数量优先 → 当日活跃量 → 日期新旧。

    理由见 BaseSource.candidate_dates 的说明：雪球组合存在非交易日调仓，
    单独取 max(日期) 会选中周六，把东财/同花顺的周五数据全部过滤为空。
    """
    votes: dict[date, list[tuple[str, int]]] = defaultdict(list)
    for name in sources:
        client = clients.get(name)
        if client is None:
            continue
        try:
            for day, count in (client.candidate_dates() or {}).items():
                votes[day].append((name, count))
        except Exception as exc:
            logger.warning(f"[{name}] 候选交易日探测失败，跳过：{exc}")
    if not votes:
        return date.today(), {}
    best = max(votes, key=lambda d: (len(votes[d]), sum(n for _, n in votes[d]), d))
    detail = {
        "picked": best.strftime("%Y-%m-%d"),
        "votes": [
            {"date": d.strftime("%Y-%m-%d"),
             "sources": sorted(n for n, _ in votes[d]),
             "activity": sum(n for _, n in votes[d])}
            for d in sorted(votes, reverse=True)
        ],
    }
    logger.info(
        "交易日投票结果：" + " | ".join(
            f"{v['date']} <- {','.join(v['sources'])}({v['activity']})" for v in detail["votes"]
        ) + f"  => 采用 {detail['picked']}"
    )
    return best, detail


def _coverage(all_experts: list[Expert], all_trades: list[TradeRecord], buys: list[TradeRecord],
              expert_buys: list[dict], sources: list[str]) -> list[dict]:
    """按数据源统计「拿到了什么」：高手身份 / 股票 / 收益率 三项是否齐全。

    2026-09-23 增补 market_type 维度：live_experts/live_buys 单列拆出，
    供前端实盘/模拟分组与「实盘明细是否可用」提示。
    """
    rows: list[dict] = []
    for name in sources:
        tr = [t for t in all_trades if t.source == name]
        by = [t for t in buys if t.source == name]
        eb = [e for e in expert_buys if e["source"] == name]
        rows.append({
            "source": name,
            "capability": SOURCE_CAPABILITY.get(name, ""),
            "experts": sum(1 for e in all_experts if e.source == name),
            "trades": len(tr),
            "buys": len(by),
            "experts_with_buys": len(eb),
            "stocks": len({t.stock_code for t in by if t.stock_code}),
            "with_expert_name": sum(1 for t in by if t.expert_name or t.expert_id),
            "with_stock": sum(1 for t in by if t.stock_code),
            "with_return_rate": sum(1 for t in by if t.return_rate is not None),
            "with_price": sum(1 for t in by if t.price),
            "live_experts": sum(1 for e in all_experts if e.source == name and e.market_type == "live"),
            "live_buys": sum(1 for t in by if t.market_type == "live"),
            "sim_experts": sum(1 for e in all_experts if e.source == name and e.market_type != "live"),
            "sim_buys": sum(1 for t in by if t.market_type != "live"),
        })
    return rows


def run(
    settings: dict,
    sources: list[str],
    trade_date: date | None = None,
    top: int = 50,
    out_dir: Path | None = None,
    formats: tuple[str, ...] = ("csv", "json"),
    db_path: Path | None = None,
) -> dict:
    """执行一次完整采集：定交易日 → 抓高手榜 → 按收益率(盈利)降序 → 展开当日买入。

    trade_date 传 None 表示自动推断（各源候选交易日投票，见 resolve_trade_date）。
    out_dir 传 None → paths.CRAWL_DATA_DIR（data/crawl/<日期>/）。
    """
    out_dir = out_dir or CRAWL_DATA_DIR
    clients = build_sources(settings)
    all_experts: list[Expert] = []
    all_trades: list[TradeRecord] = []
    stock_rank: list[StockAggregate] = []
    signals: list[dict] = []
    expert_map: dict[tuple[str, str], Expert] = {}

    date_detail: dict = {}
    if trade_date is None:
        trade_date, date_detail = resolve_trade_date(clients, sources)
        logger.info(f"自动推断交易日：{trade_date}")
    summary_date = trade_date

    for name in sources:
        client = clients.get(name)
        if client is None:
            logger.warning(f"未知数据源 {name}，已跳过")
            continue
        client.trade_date = trade_date
        try:
            experts = client.hot_experts(limit=top)
            all_experts.extend(experts)
            all_trades.extend(client.trades(experts, trade_date))
            if client.supports_stock_rank:
                stock_rank.extend(client.hot_buy_stocks(limit=top))
        except Exception as exc:
            logger.error(f"[{name}] 采集失败：{exc}")
        try:
            for sig in client.signals() or []:
                signals.append({"source": name, **sig})
        except Exception as exc:
            logger.warning(f"[{name}] 补充信号采集失败：{exc}")

    # 关闭共享抓取后端（无头浏览器实例 / curl_cffi 会话）
    try:
        shared = next(iter(clients.values())).http
        if hasattr(shared, "close"):
            shared.close()
    except Exception:
        pass

    # 从当日操作明细提取「当日活跃高手」并补齐收益率，使其进入盈利排行榜、与买入自洽
    active = _collect_active_experts(all_trades)
    all_experts.extend(active)

    # 构建 expert_map：hot_experts 优先（含 followers/rank 等完整字段）；
    # active 只补充「rank 里没有、但当日有买入操作」的高手，不覆盖已有条目。
    for e in all_experts:
        key = (e.source, e.expert_id)
        if key not in expert_map:
            expert_map[key] = e
        else:
            existing = expert_map[key]
            if existing.return_rate is None and e.return_rate is not None:
                existing.return_rate = e.return_rate

    # ① 高手榜按收益率（盈利）降序
    all_experts.sort(key=lambda e: e.return_rate or -1e9, reverse=True)

    # ② 关联高手收益率：买入明细按「高手盈利 → 金额」降序
    def _rate(t: TradeRecord) -> float:
        exp = expert_map.get((t.source, t.expert_id))
        return exp.return_rate if exp and exp.return_rate is not None else -1e9

    buys = [t for t in all_trades if t.action in BUY_ACTIONS]
    buys.sort(key=lambda t: (_rate(t), t.amount or 0), reverse=True)
    all_trades.sort(key=lambda t: (_rate(t), t.amount or 0), reverse=True)

    # ③ 按个股聚合（仅统计有股票代码的记录）
    aggregated = aggregate_buys([t for t in all_trades if t.stock_code])

    # ④ 按高手分组的买入（盈利排行视角）—— 只保留有股票代码的，主视图不出现空标的行
    expert_buys = build_expert_buys([t for t in buys if t.stock_code], expert_map)

    coverage = _coverage(all_experts, all_trades, buys, expert_buys, sources)

    out_dir = out_dir / summary_date.strftime("%Y-%m-%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    meta = {
        "trade_date": summary_date.strftime("%Y-%m-%d"),
        "date_mode": "auto" if date_detail else "fixed",
        "date_votes": date_detail.get("votes", []),
        "coverage": coverage,
    }

    if "json" in formats:
        write_json(out_dir / "experts.json", [e.model_dump() for e in all_experts])
        write_json(out_dir / "trades.json", [t.model_dump() for t in all_trades])
        write_json(out_dir / "buys.json", [t.model_dump() for t in buys])
        write_json(out_dir / "buy_aggregated.json", [a.model_dump() for a in aggregated])
        write_json(out_dir / "expert_buys.json", expert_buys)
        write_json(out_dir / "meta.json", meta)
        written += ["experts.json", "trades.json", "buys.json", "buy_aggregated.json",
                    "expert_buys.json", "meta.json"]
        if signals:
            write_json(out_dir / "signals.json", signals)
            written.append("signals.json")
        if stock_rank:
            write_json(out_dir / "stock_rank.json", [a.model_dump() for a in stock_rank])
            written.append("stock_rank.json")
    if "csv" in formats:
        write_csv(out_dir / "trades.csv", all_trades, TRADE_COLUMNS)
        write_csv(out_dir / "buys.csv", buys, TRADE_COLUMNS)
        write_csv(out_dir / "buy_aggregated.csv", aggregated, AGG_COLUMNS)
        write_csv(out_dir / "experts.csv", all_experts, EXPERT_COLUMNS)
        # 高手买入视角：同一高手的每只股票一行（按收益率降序）
        flat = []
        for eb in expert_buys:
            for s in eb["stocks"]:
                flat.append(
                    {
                        "source": eb["source"],
                        "asset_type": eb.get("asset_type") or "stock",
                        "expert_id": eb["expert_id"],
                        "expert_name": eb["expert_name"],
                        "owner": eb["owner"],
                        "rank_label": eb["rank_label"],
                        "market_type": eb.get("market_type") or "sim",
                        "return_rate": eb["return_rate"],
                        "day_return_rate": eb.get("day_return_rate"),
                        "followers": eb["followers"],
                        "stock_code": s["stock_code"],
                        "stock_name": s["stock_name"],
                        "action": s["action"],
                        "price": s["price"],
                        "volume": s["volume"],
                        "amount": s["amount"],
                        "trade_time": s["trade_time"],
                    }
                )
        write_csv(out_dir / "expert_buys.csv", flat, EXPERT_BUY_COLUMNS)
        written += ["trades.csv", "buys.csv", "buy_aggregated.csv", "experts.csv", "expert_buys.csv"]
        if stock_rank:
            write_csv(out_dir / "stock_rank.csv", stock_rank, AGG_COLUMNS)
            written.append("stock_rank.csv")
    if "xlsx" in formats:
        write_excel(out_dir / "report.xlsx", all_trades, all_experts, aggregated)
        written.append("report.xlsx")
    if "png" in formats:
        try:
            from core.render import render_dashboard_pngs

            written += render_dashboard_pngs(
                all_trades,
                experts=all_experts,
                expert_buys=expert_buys,
                trade_date=summary_date.strftime("%Y-%m-%d"),
                out_dir=out_dir,
                top=top,
            )
        except Exception as exc:
            logger.error(f"[图片] 渲染失败：{exc}")

    # ── 入库（本地 SQLite，按交易日落库，幂等可重跑）──────────────────────
    _db_cfg = settings.get("sqlite") or {}
    if _db_cfg.get("enabled"):
        try:
            db_fp = db_path or sqlite_path(settings)
            _db_ensure_schema(db_fp)  # 幂等建表，保证首次入库即可写
            _db_res = _db_write_day(
                summary_date.strftime("%Y-%m-%d"),
                out_dir,  # 此时 out_dir 已是 .../<YYYY-MM-DD> 目录
                db_path=db_fp,
                crawled_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            if _db_res:
                _db_str = ",".join(f"{k}={v}" for k, v in _db_res.items())
                written.append(f"__sqlite__:{_db_str}")
                logger.info(f"[入库] 已写入 SQLite {db_fp}: {_db_res}")
        except Exception as _db_exc:
            logger.error(f"[入库] 写入 SQLite 失败：{_db_exc}")
            if _db_cfg.get("fail_hard"):
                raise

    summary = {
        "trade_date": summary_date.strftime("%Y-%m-%d"),
        "crawled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sources": sources,
        "experts": len(all_experts),
        "trades": len(all_trades),
        "buy_actions": len(buys),
        "aggregated_stocks": len(aggregated),
        "experts_with_buys": len(expert_buys),
        "signals": len(signals),
        "output_dir": str(out_dir),
        "files": written,
    }
    logger.info(
        f"采集完成：高手 {len(all_experts)} 位（其中当日有买入 {len(expert_buys)} 位），"
        f"成交 {len(all_trades)} 条（买入 {len(buys)} 条），涉及个股 {len(aggregated)} 只"
    )
    for row in coverage:
        logger.info(
            f"  [{row['source']}] 高手 {row['experts']} 位 / 买入 {row['buys']} 笔 / "
            f"有名字 {row['with_expert_name']} / 有股票 {row['with_stock']} / 有收益率 {row['with_return_rate']}"
        )
    return summary
