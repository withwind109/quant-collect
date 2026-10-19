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

数据来源：
  * sim  ：crawl_trade（每日爬取流水累积，market_type<>'live'）∪ data/history_bulk/*.json
           （批量回补全史；按 (日期,股票,方向,价,时间) 去重，bulk 优先保留全量权重上下文）
  * live ：expert_trades 视图（crawl_trade 的 market_type='live' 投影，rtV3 实盘流水）

样本门槛：卖出笔数 < --min-sells 或覆盖交易日 < --min-days 的专家仍算指标，
但 sample_ok=False、综合评分置空、排序沉底——避免用 3 笔交易谈夏普。

产出：data/eval/expert_eval_<run_id>.json + data/eval/latest.json（Web API 直接读 latest）。
用法：
  python -m strategy.expert_eval [--env sim|live] [--start 2025-12-01] [--end 2026-10-16]
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
from paths import DB_PATH, DATA_DIR, HISTORY_BULK_DIR

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


def load_expert_signals(env: str = "sim", start: str = "", end: str = "") -> Dict[str, Dict[str, Any]]:
    """{expert_id: {name, source, market_type, records:[...]}}（按 expert_id, trade_date, trade_time 有序）。"""
    from core import db as DB

    DB.ensure_schema(DB_PATH)
    DB.ensure_strategy_schema(DB_PATH)
    out: Dict[str, Dict[str, Any]] = {}

    if env == "live":
        rows = DB.q("""SELECT expert_id, expert_name, trade_date, trade_time, stock_code, stock_name,
                              action, price, weight_after
                       FROM expert_trades
                       WHERE stock_code<>'' AND stock_code IS NOT NULL AND weight_after IS NOT NULL
                       ORDER BY expert_id, trade_date, trade_time""")
        for r in rows or []:
            eid = str(r.get("expert_id") or "")
            if not eid:
                continue
            rec = out.setdefault(eid, {"name": r.get("expert_name") or eid, "source": "eastmoney",
                                       "market_type": "live", "records": []})
            rec["records"].append(_rec(r))
    else:
        rows = DB.q("""SELECT expert_id, expert_name, source, trade_date, trade_time, stock_code, stock_name,
                              action, price, weight_after
                       FROM crawl_trade
                       WHERE COALESCE(market_type,'sim') <> 'live' AND stock_code<>''
                         AND stock_code IS NOT NULL AND weight_after IS NOT NULL
                       ORDER BY expert_id, trade_date, trade_time""")
        for r in rows or []:
            eid = str(r.get("expert_id") or "")
            if not eid:
                continue
            rec = out.setdefault(eid, {"name": r.get("expert_name") or eid,
                                       "source": r.get("source") or "eastmoney",
                                       "market_type": "sim", "records": []})
            rec["records"].append(_rec(r))
        # 批量回补全史并入（去重；bulk 记录带完整权重上下文，同键不重复加）
        for fp in sorted(HISTORY_BULK_DIR.glob("*.json")):
            try:
                d = json.loads(fp.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            eid = str(d.get("zjzh") or fp.stem)
            rec = out.setdefault(eid, {"name": d.get("name") or eid, "source": "eastmoney",
                                       "market_type": "sim", "records": []})
            if d.get("name"):
                rec["name"] = d["name"]
            seen = {(r["trade_date"], r["stock_code"], r["action"], r["price"], r["trade_time"])
                    for r in rec["records"]}
            for r in d.get("records") or []:
                if not r.get("trade_date") or not r.get("stock_code"):
                    continue
                one = _rec(r)
                key = (one["trade_date"], one["stock_code"], one["action"], one["price"], one["trade_time"])
                if key in seen:
                    continue
                seen.add(key)
                rec["records"].append(one)
        for rec in out.values():
            rec["records"].sort(key=lambda r: (r["trade_date"], r["trade_time"]))

    if start or end:
        for eid in list(out):
            rs = [r for r in out[eid]["records"]
                  if (not start or r["trade_date"] >= start) and (not end or r["trade_date"] <= end)]
            if rs:
                out[eid]["records"] = rs
            else:
                del out[eid]
    return out


# ── 单高手镜像回测（口径同 follow_backtest.run_backtest 的单专家子账户） ──────

def simulate_expert(recs: List[Dict[str, Any]], kidx: Dict[str, Dict[str, Dict[str, Any]]],
                    all_dates: List[str], capital: float = DEFAULT_CAPITAL
                    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], float, int]:
    """返回 (nav_daily, trades, fees_total, skipped_limitup)。

    nav_daily: [{date, nav}]，从该高手首笔信号日（含）起；trades 含 buy/sell/buy_skipped。
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
    dates = [d for d in all_dates if d >= min(targets)]
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
    trades: List[Dict[str, Any]] = []
    nav_daily: List[Dict[str, Any]] = []
    fees_total = 0.0
    skipped = 0

    def _nav(d: str) -> float:
        v = cash
        for code, pos in positions.items():
            k = kidx.get(code, {}).get(d)
            if k and k.get("close"):
                v += pos["qty"] * k["close"]
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
                    continue
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


def evaluate(env: str = "sim", start: str = "", end: str = "",
             min_sells: int = DEFAULT_MIN_SELLS, min_days: int = DEFAULT_MIN_DAYS,
             capital: float = DEFAULT_CAPITAL) -> Dict[str, Any]:
    """全量评估并写 data/eval/latest.json（+ 带 run_id 的历次文件）。"""
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    histories = load_expert_signals(env, start, end)
    params = {"env": env, "start": start, "end": end, "min_sells": min_sells,
              "min_days": min_days, "capital": capital}
    if not histories:
        out = {"run_id": run_id, "generated_at": datetime.now().isoformat(timespec="seconds"),
               "params": params, "meta": {"n_experts": 0, "msg": "没有可评估的信号——先爬取/回补数据"},
               "rows": [], "bench": {}}
        _write(out)
        return out

    codes = sorted({r["stock_code"] for h in histories.values() for r in h["records"]})
    dmin = min(r["trade_date"] for h in histories.values() for r in h["records"])
    dmax = max(r["trade_date"] for h in histories.values() for r in h["records"])
    p_start, p_end = start or dmin, end or dmax
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
        nav_daily, trades, fees, skipped = simulate_expert(h["records"], kidx, all_dates, capital)
        m = compute_metrics(nav_daily, trades, bench)
        if m is None:
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
        })

    _assign_scores([r for r in rows if r["sample_ok"]])
    rows.sort(key=lambda r: (r["sample_ok"], -(r["score"] if r["score"] is not None else -1e9),
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
            "data_range": [dmin, dmax], "trading_days": len(all_dates),
            "bench_code": BENCH_CODE, "bench_available": bool(bench),
            "msg": "",
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

def expert_detail(expert_id: str, env: str = "sim", start: str = "", end: str = "",
                  capital: float = DEFAULT_CAPITAL) -> Dict[str, Any]:
    """单个高手的净值曲线 + 成交明细 + 指标（供详情抽屉/曲线图）。"""
    from core import db as DB

    DB.ensure_schema(DB_PATH)
    DB.ensure_strategy_schema(DB_PATH)
    if env == "live":
        rows = DB.q("""SELECT expert_id, expert_name, trade_date, trade_time, stock_code, stock_name,
                               action, price, weight_after
                        FROM expert_trades
                        WHERE expert_id=%s AND stock_code<>'' AND weight_after IS NOT NULL
                        ORDER BY trade_date, trade_time""", (str(expert_id),))
    else:
        rows = DB.q("""SELECT expert_id, expert_name, source, trade_date, trade_time, stock_code, stock_name,
                               action, price, weight_after
                        FROM crawl_trade
                        WHERE expert_id=%s AND COALESCE(market_type,'sim')<>'live'
                          AND stock_code<>'' AND weight_after IS NOT NULL
                        ORDER BY trade_date, trade_time""", (str(expert_id),))
    recs = [_rec(r) for r in rows or []]
    # 并入批量回补全史（与批量评估同口径去重）
    fp = HISTORY_BULK_DIR / f"{expert_id}.json"
    name = (rows[0].get("expert_name") if rows else "") or expert_id
    source = (rows[0].get("source") if rows else "") or "eastmoney"
    if env != "live" and fp.exists():
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
            name = d.get("name") or name
            seen = {(r["trade_date"], r["stock_code"], r["action"], r["price"], r["trade_time"])
                    for r in recs}
            for r in d.get("records") or []:
                if not r.get("trade_date") or not r.get("stock_code"):
                    continue
                one = _rec(r)
                key = (one["trade_date"], one["stock_code"], one["action"], one["price"], one["trade_time"])
                if key not in seen:
                    seen.add(key)
                    recs.append(one)
            recs.sort(key=lambda r: (r["trade_date"], r["trade_time"]))
        except (OSError, ValueError):
            pass
    if start or end:
        recs = [r for r in recs
                if (not start or r["trade_date"] >= start) and (not end or r["trade_date"] <= end)]
    if not recs:
        return {"ok": False, "msg": "该高手没有可评估的调仓记录"}
    codes = sorted({r["stock_code"] for r in recs})
    dmin, dmax = recs[0]["trade_date"], recs[-1]["trade_date"]
    kidx = _kline_for(codes, start or dmin, end or dmax)
    all_dates = sorted({d for m in kidx.values() for d in m})
    if not all_dates:
        return {"ok": False, "msg": "本地没有相关标的行情（先跑 prices 补齐）"}
    try:
        bench = P.index_closes(BENCH_CODE, dmin, dmax)
    except Exception:  # noqa: BLE001
        bench = {}
    nav_daily, trades, fees, skipped = simulate_expert(recs, kidx, all_dates, capital)
    m = compute_metrics(nav_daily, trades, bench)
    if m is None:
        return {"ok": False, "msg": "信号太少，算不出指标"}
    return {
        "ok": True, "expert_id": str(expert_id), "name": name, "source": source,
        "env": env, "metrics": m, "fees": round(fees, 2), "skipped_limitup": skipped,
        "nav_daily": nav_daily,
        "bench": {x["date"]: round(bench[x["date"]], 2) for x in nav_daily if x["date"] in bench},
        "trades": trades[-200:],
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="高手能力评估（逐高手镜像回测）")
    ap.add_argument("--env", default="sim", choices=["sim", "live"],
                    help="sim=模拟盘流水+批量回补；live=实盘 rtV3 流水")
    ap.add_argument("--start", default="", help="评估起点 YYYY-MM-DD，空=数据最早日")
    ap.add_argument("--end", default="", help="评估截止 YYYY-MM-DD，空=数据最晚日")
    ap.add_argument("--min-sells", type=int, default=DEFAULT_MIN_SELLS,
                    help="样本充足的卖出笔数门槛")
    ap.add_argument("--min-days", type=int, default=DEFAULT_MIN_DAYS,
                    help="样本充足的覆盖交易日门槛")
    ap.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    args = ap.parse_args()
    setup_logging()
    out = evaluate(env=args.env, start=args.start, end=args.end,
                   min_sells=args.min_sells, min_days=args.min_days, capital=args.capital)
    meta = out["meta"]
    print(json.dumps({"run_id": out["run_id"], "meta": meta,
                      "top5": [{k: r[k] for k in ("rank", "name", "expert_id", "sharpe",
                                                  "total_return_pct", "max_drawdown_pct", "score")}
                               for r in out["rows"][:5]]}, ensure_ascii=False, indent=2))
    print(f"\n结果文件: {EVAL_LATEST}")


if __name__ == "__main__":
    main()
