"""高手能力评估：逐高手镜像回测 → 盈利能力指标（2026-10-19）。

回答的问题：「这位高手自己赚不赚钱、值不值得跟他的单」——
用他自己的历史调仓记录做一次镜像回测（等同无脑跟单），输出：
总收益率 / 年化 / 夏普 / 索提诺 / 最大回撤 / Calmar / 胜率 / 盈亏比 /
相对沪深300 超额 / alpha·beta（对中证500 回归）/ 覆盖天数 / 买卖笔数。

交易口径与 strategy/follow_backtest.py 单一事实源（常量从该模块导入，
改那里这里自动跟随，不复制第二套数字）：
  * 信号→目标：同一高手同一股票同一交易日取日终净目标权重（weight_after 最后一条），T+1 开盘执行
  * 执行：开盘价 ± 滑点 0.002；佣金 0.0008（最低 5 元）；卖出加印花税 0.0005
  * 涨停拦截（开盘 ≥ 前收×涨跌幅上限）、噪音过滤（目标权重 <1% 视为清仓/不动）
  * 窗口从该高手首笔信号日起（跟单体验：之前没单可跟，净值持平无意义）
  * 期末按最后交易日收盘结算；展示基准沪深300（000300.SH），alpha 对中证500 回归

数据来源（2026-10-19 起仅实盘——模拟盘爬取通道已整体删除）：
  * live ：expert_trades 视图（crawl_trade 的 market_type='live' 投影，rtV3 实盘流水）

样本门槛：卖出笔数 < --min-sells 或覆盖交易日 < --min-days 的专家仍算指标，
但 sample_ok=False、综合评分置空、排序沉底——避免用 3 笔交易谈夏普。

产出：data/eval/expert_eval_<run_id>.json + data/eval/latest.json（Web API 直接读 latest）。
用法：
  python -m strategy.expert_eval [--start 2025-12-01] [--end 2026-10-16]
                                 [--min-sells 3] [--min-days 20] [--top-detail 50]
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
from collections import defaultdict
from datetime import date as _date
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from core.logger import logger, setup_logging
from paths import DB_PATH, DATA_DIR

# ── 交易口径：与 follow_backtest 单一事实源 ─────────────────────────────────
from strategy import follow_backtest as FB
from strategy import prices as P

EVAL_DIR = DATA_DIR / "eval"
EVAL_LATEST = EVAL_DIR / "latest.json"
BENCH_CODE = "000300.SH"
DEFAULT_CAPITAL = 1_000_000.0
DEFAULT_MIN_SELLS = 3
DEFAULT_MIN_DAYS = 20
DEFAULT_TOP_DETAIL = 50


# ── 数据加载 ─────────────────────────────────────────────────────────────────

def _rec(r: dict) -> Dict[str, Any]:
    """统一信号记录形状（只留评估用字段）。"""
    return {
        "trade_date": str(r.get("trade_date") or ""),
        "trade_time": str(r.get("trade_time") or ""),
        "stock_code": r.get("stock_code") or "",
        "stock_name": r.get("stock_name") or "",
        "action": r.get("action") or "",
        "price": r.get("price"),
        "weight_after": r.get("weight_after"),
    }


def _shift_days(day: str, days: int) -> str:
    """YYYY-MM-DD ± N 自然日（K 线窗口缓冲用）；解析失败原样返回。"""
    try:
        return (_date.fromisoformat(day) + timedelta(days=days)).isoformat()
    except (TypeError, ValueError):
        return day


def load_expert_signals(env: str = "live", start: str = "", end: str = "") -> Dict[str, Dict[str, Any]]:
    """{expert_id: {name, source, market_type, records:[...], health:{...}}}（按日期/时间有序）。

    ⚠️ 2026-10-19 口径修复：rtV3 的 positionRatio 是「成交金额 / 组合总资产」的分档，
    **不是调仓后仓位**（'1成' 的卖出有 57% 是整只清仓）。因此必须先用
    pit_nav.normalize_live 做「现金+持仓」记账重放，还原每条记录真实的调仓前/后仓位，
    再进入镜像回测；直接把 positionRatio 当目标仓位用会留下卖出后的幽灵仓位，指标全错
    （源项目实测期末持仓 84% 是幽灵仓位，实盘回测全部作废）。

    2026-10-19 起仅支持 live（rtV3 实盘流水）；传 sim 会抛 ValueError——
    模拟盘爬取通道已整体删除，从入口杜绝任何逻辑再碰模拟盘数据。
    """
    if env != "live":
        raise ValueError(f"expert_eval 仅支持 env='live'（模拟盘通道已删除），收到：{env!r}")
    from core import db as DB
    DB.ensure_schema(DB_PATH)
    DB.ensure_strategy_schema(DB_PATH)
    out, _health = _normalized_histories(start, end)
    return out


def _normalized_histories(start: str = "", end: str = "", expert_id: str = ""):
    """加载实盘流水 → normalize_live 记账重放 → (可用组合, 体检)。

    health: {expert_id: {usable, reason, n_records, n_labeled, phantom_end, ...}}
    不可用组合（无原文/起点持仓不明）整体剔除，原因透出到评估 meta。
    expert_id 非空时只重建该组合（详情抽屉用，避免全量算一遍）。
    """
    from strategy import pit_nav as PN

    raw = PN.load_histories("live")
    if expert_id:
        raw = {k: v for k, v in raw.items() if k == str(expert_id)}
    if not raw:
        return {}, {}
    codes = sorted({r["stock_code"] for h in raw.values() for r in h["records"] if r.get("stock_code")})
    dmin = min(r["trade_date"] for h in raw.values() for r in h["records"] if r.get("trade_date"))
    dmax = max(r["trade_date"] for h in raw.values() for r in h["records"] if r.get("trade_date"))
    px = PN.PriceCtx(codes, end or dmax, start=start or dmin)
    norm, health = PN.normalize_live(raw, px)

    out: Dict[str, Dict[str, Any]] = {}
    for eid, h in norm.items():
        recs = [_rec(r) for r in h["records"]
                if r.get("stock_code") and r.get("weight_after") is not None]
        recs = [r for r in recs
                if (not start or r["trade_date"] >= start) and (not end or r["trade_date"] <= end)]
        if not recs:
            continue
        info = dict(health.get(eid) or {})
        info["n_used"] = len(recs)
        out[eid] = {"name": h.get("name") or eid, "source": "eastmoney",
                    "market_type": "live", "records": recs, "health": info}
    return out, health


# ── 单高手镜像回测（口径同 follow_backtest.run_backtest 的单专家子账户） ──────

def simulate_expert(recs: List[Dict[str, Any]], kidx: Dict[str, Dict[str, Dict[str, Any]]],
                    all_dates: List[str], capital: float = DEFAULT_CAPITAL,
                    seeds: Dict[str, float] | None = None
                    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], float, int]:
    """返回 (nav_daily, trades, fees_total, skipped_limitup)。

    nav_daily: [{date, nav}]，从该高手首笔信号日起、到最后一条信号的 T+1 执行日止；
    trades 含 buy/sell/buy_skipped。
    seeds: 起点回填持仓 {code: 起始权重}（normalize_live 账本里"记录起点之前就持有"的股票，
    按起点前收盘价建仓），与账本口径一致——否则这些仓位的卖出信号在镜像里无仓可卖。
    """
    targets: Dict[str, Dict[str, Tuple[float, str, str]]] = defaultdict(dict)
    for r in recs:
        wa = r.get("weight_after")
        d, c = r.get("trade_date") or "", r.get("stock_code") or ""
        if wa is None or not d or not c:
            continue
        try:
            w = float(wa) / 100.0
        except (TypeError, ValueError):
            continue
        targets[d][c] = (w, r.get("stock_name") or c, r.get("trade_time") or "")
    if not targets:
        return [], [], 0.0, 0
    first_sig, last_sig = min(targets), max(targets)

    # [2026-10-19 修] 评估窗口截止 = 最后一条信号的 T+1 执行日。
    # 原实现 dates 只到 K 线末尾（= 最后信号日），最后一条信号的执行日必然被截掉——
    # 最后一笔调仓永远不执行（股友Qz1LRU 的 10-12 买国力电子就是这样丢的）。
    dates = [d for d in all_dates if first_sig <= d]
    i_last = bisect.bisect_right(all_dates, last_sig)
    if i_last < len(all_dates):
        dates = [d for d in dates if d <= all_dates[i_last]]
    if not dates:
        return [], [], 0.0, 0

    # T+1 执行映射（同一执行日多条信号取后到者 = 日终净目标语义）
    exec_day: Dict[str, Dict[str, Tuple[float, str, str]]] = defaultdict(dict)
    for sd, code_map in targets.items():
        i = bisect.bisect_right(dates, sd)
        if i >= len(dates):
            continue
        for c, t in code_map.items():
            exec_day[dates[i]][c] = t

    cash = float(capital)
    positions: Dict[str, Dict[str, float]] = {}
    # [2026-10-19 修] 起点回填仓初始化（与 normalize_live 账本同口径）：
    # 按首笔信号前一交易日的收盘价建仓，否则"卖出未见买入"的起点仓位卖不出去
    if seeds:
        pre = [d for d in all_dates if d < first_sig]
        seed_day = pre[-1] if pre else None
        for code, wt in seeds.items():
            k = kidx.get(code, {}).get(seed_day) if seed_day else None
            close = (k or {}).get("close")
            if not close or wt <= 0:
                continue
            qty = round(wt * capital / close / 100) * 100
            if qty <= 0:
                continue
            positions[code] = {"qty": float(qty), "cost": float(close)}
            cash -= qty * close
    trades: List[Dict[str, Any]] = []
    nav_daily: List[Dict[str, Any]] = []
    fees_total = 0.0
    skipped = 0
    last_close: Dict[str, float] = {}     # code -> 最近有效收盘价（停牌日估值结转）

    def _nav(d: str) -> float:
        v = cash
        for code, pos in positions.items():
            k = kidx.get(code, {}).get(d)
            close = k.get("close") if k else None
            if close:
                last_close[code] = close          # 滚动记录最近有效收盘价
            close = close or last_close.get(code)
            if close:
                v += pos["qty"] * close
            # [2026-10-19 修] 原实现在当日无 K 线（停牌/缺数据）时直接不计该持仓，
            # 会让净值凭空跳水再跳回，虚增回撤与波动；改为前值结转到最近有效收盘价
        return v

    for d in dates:
        prev_nav = nav_daily[-1]["nav"] if nav_daily else cash
        for code, (tw, sname, stime) in (exec_day.get(d) or {}).items():
            k = kidx.get(code, {}).get(d)
            if not k or not k.get("open"):
                continue  # 停牌/无K线：顺延不了就放弃（同原引擎）
            pos = positions.get(code)
            cur_qty = pos["qty"] if pos else 0
            eff_tw = tw if tw >= FB.MIN_WEIGHT else 0.0
            tgt_val = eff_tw * prev_nav
            cur_val = cur_qty * k["open"]
            if abs(tgt_val - cur_val) < max(500.0, prev_nav * 0.002):
                continue  # 变动太小，省手续费不调仓
            if tgt_val > cur_val:
                # ── 买入/加仓 ──
                if k.get("prev_close") and k["open"] >= k["prev_close"] * FB._limit_up_ratio(code):
                    skipped += 1
                    trades.append({"date": d, "code": code, "name": sname, "side": "buy_skipped",
                                   "reason": "开盘涨停", "signal_time": stime})
                    continue
                buy_val = min(tgt_val - cur_val, cash)
                if buy_val < 1000:
                    continue
                px = k["open"] * (1 + FB.SLIPPAGE)
                qty = math.floor(buy_val / px / 100) * 100
                if qty <= 0:
                    # [2026-10-19 修] 高价股整手约束：目标金额不足一手时，现金够就至少买一手
                    # （真实跟单一手起步，仓位略微超出目标；此前直接丢弃——罗博特科 618 元/手
                    #   6.18 万 > 6% 目标 6 万，整条买入信号就这样蒸发了）
                    if px * 100 > cash or buy_val < 1000:
                        continue
                    qty = 100
                gross = qty * px
                fee = max(gross * FB.COMMISSION, FB.MIN_FEE)
                if gross + fee > cash:
                    qty = max(0, math.floor((cash - FB.MIN_FEE) / px / 100) * 100)
                    gross = qty * px
                    fee = max(gross * FB.COMMISSION, FB.MIN_FEE) if qty > 0 else 0.0
                    if qty <= 0:
                        continue
                cash -= gross + fee
                fees_total += fee
                old = positions.get(code)
                new_qty = (old["qty"] if old else 0) + qty
                new_cost = ((old["qty"] * old["cost"]) if old else 0.0) + gross
                positions[code] = {"qty": new_qty, "cost": new_cost / max(new_qty, 1)}
                trades.append({"date": d, "code": code, "name": sname, "side": "buy",
                               "price": round(px, 3), "qty": qty, "amount": round(gross, 2),
                               "fee": round(fee, 2), "signal_time": stime})
            elif cur_qty > 0:
                # ── 卖出/减仓 ──
                px = k["open"] * (1 - FB.SLIPPAGE)
                tgt_qty = math.floor(tgt_val / px / 100) * 100
                sell_qty = cur_qty - max(tgt_qty, 0)
                if sell_qty <= 0:
                    continue
                gross = sell_qty * px
                fee = max(gross * FB.COMMISSION, FB.MIN_FEE) + gross * FB.STAMP_TAX
                cash += gross - fee
                fees_total += fee
                pos = positions[code]
                entry = pos["cost"]
                pos["qty"] -= sell_qty
                realized = (px - entry) * sell_qty - fee
                trades.append({"date": d, "code": code, "name": sname, "side": "sell",
                               "price": round(px, 3), "qty": sell_qty, "amount": round(gross, 2),
                               "fee": round(fee, 2), "realized_pnl": round(realized, 2),
                               "entry_cost": round(entry, 3),
                               "return_pct": round((px / entry - 1) * 100, 2) if entry else None,
                               "signal_time": stime})
                if pos["qty"] <= 0:
                    del positions[code]
        nav_daily.append({"date": d, "nav": round(_nav(d), 2)})
    return nav_daily, trades, fees_total, skipped


# ── 指标 ─────────────────────────────────────────────────────────────────────

def compute_metrics(nav_daily: List[Dict[str, Any]], trades: List[Dict[str, Any]],
                    bench: Dict[str, float]) -> Optional[Dict[str, Any]]:
    """由净值序列 + 成交明细算全套指标；数据不足返回 None。"""
    if len(nav_daily) < 2:
        return None
    navs = [x["nav"] for x in nav_daily]
    dts = [x["date"] for x in nav_daily]
    rets: List[float] = []
    ret_dates: List[str] = []
    for i in range(1, len(nav_daily)):
        prev = navs[i - 1]
        if prev > 0:
            rets.append(navs[i] / prev - 1)
            ret_dates.append(dts[i])
    if not rets:
        return None

    cum, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        cum *= (1 + r)
        peak = max(peak, cum)
        mdd = max(mdd, 1 - cum / peak)
    d0, d1 = _date.fromisoformat(dts[0]), _date.fromisoformat(dts[-1])
    days = max((d1 - d0).days, 1)
    total_ret = navs[-1] / navs[0] - 1
    annualized = (1 + total_ret) ** (365 / days) - 1 if total_ret > -1 else -1.0
    mean_r = sum(rets) / len(rets)
    std_r = (sum((x - mean_r) ** 2 for x in rets) / max(len(rets) - 1, 1)) ** 0.5
    sharpe = (mean_r / std_r) * math.sqrt(252) if std_r > 0 else None
    dd_std = (sum(min(x, 0.0) ** 2 for x in rets) / len(rets)) ** 0.5
    sortino = (mean_r / dd_std) * math.sqrt(252) if dd_std > 0 else None
    calmar = (annualized / mdd) if mdd > 0 else None

    sells = [t for t in trades if t["side"] == "sell"]
    wins = [t for t in sells if (t.get("return_pct") or 0) > 0]
    losses = [t for t in sells if (t.get("return_pct") or 0) <= 0]
    win_rate = len(wins) / len(sells) * 100 if sells else None
    avg_win = sum(t["return_pct"] for t in wins) / len(wins) if wins else None
    avg_loss = sum(t["return_pct"] for t in losses) / len(losses) if losses else None
    gross_win = sum(t.get("realized_pnl") or 0 for t in wins)
    gross_loss = -sum(t.get("realized_pnl") or 0 for t in losses)
    pf = gross_win / gross_loss if gross_loss > 0 else None

    # 基准（沪深300 同时段）与 alpha（对中证500 回归）
    excess = alpha_pct = beta = alpha_t = None
    if bench:
        bdates = [d for d in dts if d in bench]
        if len(bdates) >= 2 and bench[bdates[0]]:
            bench_ret = bench[bdates[-1]] / bench[bdates[0]] - 1
            excess = round((total_ret - bench_ret) * 100, 2)
    try:
        from strategy import alpha as A
        al = A.regress(ret_dates, rets, A.bench_returns(dts[0], dts[-1]))
        if al:
            alpha_pct = round(al["alpha"], 2)
            beta = round(al["beta"], 3)
            alpha_t = round(al["alpha_t"], 2)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[expert-eval] alpha 回归失败: {exc}")

    return {
        "total_return_pct": round(total_ret * 100, 2),
        "annualized_pct": round(annualized * 100, 2),
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "sortino": round(sortino, 2) if sortino is not None else None,
        "max_drawdown_pct": round(mdd * 100, 2),
        "calmar": round(calmar, 2) if calmar is not None else None,
        "win_rate_pct": round(win_rate, 1) if win_rate is not None else None,
        "avg_win_pct": round(avg_win, 2) if avg_win is not None else None,
        "avg_loss_pct": round(avg_loss, 2) if avg_loss is not None else None,
        "profit_factor": round(pf, 2) if pf is not None else None,
        "excess_pct": excess, "alpha_pct": alpha_pct, "beta": beta, "alpha_t": alpha_t,
        "n_buy": len([t for t in trades if t["side"] == "buy"]),
        "n_sell": len(sells),
        "trading_days": len(nav_daily),
        "calendar_days": days,
        "final_nav": navs[-1],
        "first_date": dts[0], "last_date": dts[-1],
    }


# ── 综合评分（分位秩平均，仅样本充足者参与） ─────────────────────────────────

_SCORE_KEYS = ("sharpe", "sortino", "calmar", "win_rate_pct")


def _assign_scores(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    for key in _SCORE_KEYS:
        vals = sorted(r[key] for r in rows if r.get(key) is not None)
        n = len(vals)
        for r in rows:
            v = r.get(key)
            if v is None or n == 0:
                r["_p"] = 0.0
            else:
                lo = bisect.bisect_left(vals, v)
                hi = bisect.bisect_right(vals, v)
                r["_p"] = (lo + hi) / 2 / n
            r.setdefault("_ps", []).append(r.pop("_p"))
    for r in rows:
        r["score"] = round(sum(r["_ps"]) / len(_SCORE_KEYS) * 100, 1)
        del r["_ps"]


# ── 批量评估 ─────────────────────────────────────────────────────────────────

def _kline_for(codes: List[str], start: str, end: str) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """code -> {date: {date, open, close, prev_close}}（本地价格库优先，口径同 follow_backtest）。

    带「近期确认缺失」跳过缓存：东财/腾讯都取不到的代码（老三板/已退市/接口不served）每次重试要
    5-7 秒，全量评估时会白等几分钟。失败的代码记进 data/eval/price_miss.json（7 天过期，
    期间跳过）；单次缺失超过一半视为行情源故障，不写缓存（避免整体误伤）。
    """
    if not codes:
        return {}
    miss = _load_price_miss()
    todo = [c for c in codes if c not in miss]
    if len(miss) < len(codes):
        logger.info(f"[expert-eval] 跳过近期确认无行情的代码 {len(codes) - len(todo)} 只")
    if todo:
        try:
            P.ensure_calendar()
            done = P.ensure_prices(todo, start, end, max_download=800)
            if done:
                logger.info(f"[expert-eval] 补齐行情 {len(done)} 只")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[expert-eval] 行情补齐失败（按本地已有数据继续）: {exc}")
    kline = FB.load_kline_map(codes, start, end)
    missing_now = [c for c in todo if c not in kline]
    if missing_now and len(missing_now) <= max(1, len(todo) // 2):
        _save_price_miss(missing_now)
    elif missing_now:
        logger.warning(f"[expert-eval] 缺失代码 {len(missing_now)}/{len(todo)} 过多，疑似行情源故障，不写跳过缓存")
    return {code: {k["date"]: k for k in lst} for code, lst in kline.items()}


_PRICE_MISS_FP = EVAL_DIR / "price_miss.json"
_MISS_TTL_DAYS = 7


def _load_price_miss() -> set:
    """近期确认无行情的代码集合（过期自动重试）。"""
    try:
        d = json.loads(_PRICE_MISS_FP.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    cutoff = (datetime.now() - timedelta(days=_MISS_TTL_DAYS)).strftime("%Y-%m-%d")
    return {k for k, v in d.items() if str(v) >= cutoff}


def _save_price_miss(codes: List[str]) -> None:
    try:
        try:
            old = json.loads(_PRICE_MISS_FP.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = {}
        today = datetime.now().strftime("%Y-%m-%d")
        for c in codes:
            old[c] = today
        EVAL_DIR.mkdir(parents=True, exist_ok=True)
        _PRICE_MISS_FP.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
        logger.info(f"[expert-eval] 记录无行情代码 {len(codes)} 只（7 天内跳过）")
    except OSError as exc:
        logger.warning(f"[expert-eval] 写跳过缓存失败: {exc}")


def evaluate(env: str = "live", start: str = "", end: str = "",
             min_sells: int = DEFAULT_MIN_SELLS, min_days: int = DEFAULT_MIN_DAYS,
             capital: float = DEFAULT_CAPITAL) -> Dict[str, Any]:
    """全量评估并写 data/eval/latest.json（+ 带 run_id 的历次文件）。仅实盘。"""
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    if env != "live":
        raise ValueError(f"expert_eval 仅支持 env='live'（模拟盘通道已删除），收到：{env!r}")
    histories, health = _normalized_histories(start, end)
    params = {"env": env, "start": start, "end": end, "min_sells": min_sells,
              "min_days": min_days, "capital": capital}
    skip_list = [{"expert_id": eid, "name": (info or {}).get("name") or eid,
                  "reason": (info or {}).get("reason") or "数据不可用"}
                 for eid, info in (health or {}).items() if not (info or {}).get("usable")]
    if not histories:
        out = {"run_id": run_id, "generated_at": datetime.now().isoformat(timespec="seconds"),
               "params": params, "meta": {"n_experts": 0, "msg": "没有可评估的信号——先爬取/回补数据",
                                          "skipped": skip_list},
               "rows": [], "bench": {}}
        _write(out)
        return out

    codes = sorted({r["stock_code"] for h in histories.values() for r in h["records"]})
    dmin = min(r["trade_date"] for h in histories.values() for r in h["records"])
    dmax = max(r["trade_date"] for h in histories.values() for r in h["records"])
    # K 线窗口前后各留 14 天缓冲：前缓冲供起点回填仓取"首信号前一交易日"收盘价，
    # 后缓冲保证最后一条信号的 T+1 执行日落在窗口内（否则最后一笔调仓永远不执行）
    p_start = _shift_days(start or dmin, -14)
    p_end = _shift_days(end or dmax, 14)
    kidx = _kline_for(codes, p_start, p_end)
    all_dates = sorted({d for m in kidx.values() for d in m})
    try:
        bench = P.index_closes(BENCH_CODE, dmin, dmax)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[expert-eval] 基准行情缺失: {exc}")
        bench = {}
    logger.info(f"[expert-eval] 信号专家 {len(histories)} · 标的 {len(codes)}(有行情 {len(kidx)}) · "
                f"数据范围 {dmin}~{dmax} · 交易日 {len(all_dates)}")

    rows: List[Dict[str, Any]] = []
    for eid, h in histories.items():
        nav_daily, trades, fees, skipped = simulate_expert(
            h["records"], kidx, all_dates, capital,
            seeds=(h.get("health") or {}).get("seed_map"))
        m = compute_metrics(nav_daily, trades, bench)
        if m is None:
            skip_list.append({"expert_id": eid, "name": h["name"], "reason": "信号太少，算不出指标"})
            continue
        if not m.get("n_buy") and not m.get("n_sell"):
            # [2026-10-19 修] 零成交的"僵尸行"（有流水但行情缺失/全部无法执行）不再输出
            # 收益 0、夏普 None 的假结果，改为进 skipped 给出原因
            no_px = sorted({r["stock_code"] for r in h["records"]} - set(kidx))
            reason = ("标的行情缺失，无法回测（本地无历史K线）: " + ", ".join(no_px[:6])) if no_px \
                else "调仓信号全部无法执行（停牌/涨停/无行情）"
            skip_list.append({"expert_id": eid, "name": h["name"], "reason": reason})
            continue
        rows.append({
            "expert_id": eid, "name": h["name"], "source": h["source"],
            "market_type": h["market_type"],
            "sample_ok": (m["n_sell"] >= min_sells and m["trading_days"] >= min_days),
            "score": None,
            **{k: m[k] for k in ("total_return_pct", "annualized_pct", "sharpe", "sortino",
                                 "max_drawdown_pct", "calmar", "win_rate_pct", "profit_factor",
                                 "avg_win_pct", "avg_loss_pct", "excess_pct", "alpha_pct", "beta",
                                 "alpha_t", "n_buy", "n_sell", "trading_days", "calendar_days",
                                 "final_nav", "first_date", "last_date")},
            "fees": round(fees, 2), "skipped_limitup": skipped,
            "data_health": {k: (h.get("health") or {}).get(k) for k in
                            ("usable", "reason", "n_records", "n_labeled", "n_used",
                             "phantom_end", "seeded", "gap_days")},
        })

    _assign_scores([r for r in rows if r["sample_ok"]])
    # [2026-10-19 修] 原排序 key 用 r["sample_ok"] 升序 → False 在前，样本不足的反倒排第一；
    # 现改为 0/1 分值升序（样本充足=0 在前），再按评分/夏普降序
    rows.sort(key=lambda r: (0 if r["sample_ok"] else 1,
                             -(r["score"] if r["score"] is not None else -1e9),
                             -(r["sharpe"] if r["sharpe"] is not None else -1e9)))
    for i, r in enumerate(rows):
        r["rank"] = i + 1

    out = {
        "run_id": run_id,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "params": params,
        "meta": {
            "n_experts": len(rows),
            "n_sample_ok": sum(1 for r in rows if r["sample_ok"]),
            "n_codes": len(codes), "n_codes_with_price": len(kidx),
            "data_range": [dmin, dmax],
            "trading_days": len([d for d in all_dates if dmin <= d <= dmax]),
            "bench_code": BENCH_CODE, "bench_available": bool(bench),
            "n_skipped": len(skip_list), "skipped": skip_list,
            "msg": "" if rows else "有信号但算不出指标：可能是标的行情缺失、数据不足或全部是港股/无行情代码",
        },
        "bench": {d: round(bench[d], 2) for d in all_dates if d in bench},
        "rows": rows,
    }
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    (EVAL_DIR / f"expert_eval_{run_id}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    _write(out)
    logger.info(f"[expert-eval] 完成：{len(rows)} 位高手入榜，样本充足 {out['meta']['n_sample_ok']} 位")
    return out


def _write(out: Dict[str, Any]) -> None:
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    EVAL_LATEST.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                           encoding="utf-8")


# ── 单高手明细（详情视图按需现算，与批量同口径） ─────────────────────────────

def expert_detail(expert_id: str, env: str = "live", start: str = "", end: str = "",
                  capital: float = DEFAULT_CAPITAL) -> Dict[str, Any]:
    """单个高手的净值曲线 + 成交明细 + 指标（供详情抽屉/曲线图）。仅实盘。

    与批量评估同一套口径：先 normalize_live 记账重放（rtV3 的 positionRatio 是成交占比，
    不是调仓后仓位），再镜像回测。
    """
    if env != "live":
        raise ValueError(f"expert_eval 仅支持 env='live'（模拟盘通道已删除），收到：{env!r}")
    from core import db as DB
    DB.ensure_schema(DB_PATH)
    DB.ensure_strategy_schema(DB_PATH)
    from strategy import pit_nav as PN

    all_hist, health = _normalized_histories(start, end, expert_id=str(expert_id))
    h = all_hist.get(str(expert_id))
    if not h:
        info = (health or {}).get(str(expert_id)) or {}
        reason = info.get("reason") or "该高手没有可评估的调仓记录"
        return {"ok": False, "msg": reason}
    recs = h["records"]
    if not recs:
        return {"ok": False, "msg": "该高手没有可评估的调仓记录"}
    codes = sorted({r["stock_code"] for r in recs})
    dmin, dmax = recs[0]["trade_date"], recs[-1]["trade_date"]
    # 与 evaluate 同口径：前后各留 14 天缓冲（起点回填仓取价 + 最后信号的 T+1 执行日）
    kidx = _kline_for(codes, _shift_days(start or dmin, -14), _shift_days(end or dmax, 14))
    all_dates = sorted({d for m in kidx.values() for d in m})
    if not all_dates:
        return {"ok": False, "msg": "本地没有相关标的行情（先跑 prices 补齐）"}
    try:
        bench = P.index_closes(BENCH_CODE, dmin, dmax)
    except Exception:  # noqa: BLE001
        bench = {}
    nav_daily, trades, fees, skipped = simulate_expert(
        recs, kidx, all_dates, capital,
        seeds=(h.get("health") or {}).get("seed_map"))
    m = compute_metrics(nav_daily, trades, bench)
    if m is None:
        return {"ok": False, "msg": "信号太少，算不出指标"}
    return {
        "ok": True, "expert_id": str(expert_id), "name": h["name"], "source": h["source"],
        "env": env, "metrics": m, "fees": round(fees, 2), "skipped_limitup": skipped,
        "data_health": h.get("health") or {},
        "nav_daily": nav_daily,
        "bench": {x["date"]: round(bench[x["date"]], 2) for x in nav_daily if x["date"] in bench},
        "trades": trades[-200:],
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="高手能力评估（逐高手镜像回测，仅实盘）")
    ap.add_argument("--start", default="", help="评估起点 YYYY-MM-DD，空=数据最早日")
    ap.add_argument("--end", default="", help="评估截止 YYYY-MM-DD，空=数据最晚日")
    ap.add_argument("--min-sells", type=int, default=DEFAULT_MIN_SELLS,
                    help="样本充足的卖出笔数门槛")
    ap.add_argument("--min-days", type=int, default=DEFAULT_MIN_DAYS,
                    help="样本充足的覆盖交易日门槛")
    ap.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    args = ap.parse_args()
    setup_logging()
    out = evaluate(env="live", start=args.start, end=args.end,
                   min_sells=args.min_sells, min_days=args.min_days, capital=args.capital)
    meta = out["meta"]
    print(json.dumps({"run_id": out["run_id"], "meta": meta,
                      "top5": [{k: r[k] for k in ("rank", "name", "expert_id", "sharpe",
                                                  "total_return_pct", "max_drawdown_pct", "score")}
                               for r in out["rows"][:5]]}, ensure_ascii=False, indent=2))
    print(f"\n结果文件: {EVAL_LATEST}")


if __name__ == "__main__":
    main()
