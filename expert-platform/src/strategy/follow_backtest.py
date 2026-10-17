"""迁移自 scripts/experts/follow_backtest.py (2026-10-16)。变更 (仅导入路径/存储后端/文件路径, 业务逻辑逐行保持):
  * load_index: MySQL xcq_index_daily -> strategy.prices.index_closes (本地价格库指数收盘, dict[date, close] 同语义);
  * load_kline_map 主路径: qlib (scripts/selectors/selector_kline_utils.preload_klines) -> 本地价格库
    (strategy.pit_nav._prices 读 price_daily 的 open/close), 回退分支 (xcq_stock_kline) 原样保留;
  * load_signals: xcq_expert_trades -> crawl_trade (全量流水; live 分组查询走 expert_trades 视图);
  * persist: qexec 走 core.db; ON DUPLICATE KEY UPDATE run_id=run_id (MySQL 空更新) -> INSERT OR IGNORE
    (SQLite 等价) —— 目标表 xcq_expert_follow_backtest_runs 本项目暂无读取方且未建表, 失败由本函数 try/except 吞掉;
  * 路径: OUT_DIR -> paths.FOLLOW_BT_DIR。

「跟随社区高手」策略回测 (2026-09-24)。

设计（方案见 docs/社区高手实盘模拟盘分离与指定高手跟踪_方案_20260923.md 后续规划）：
  * 策略：跟随关注列表(xcq_expert_watchlist)高手的买卖信号，用模拟资金镜像其持仓权重。
  * 子账户：每位关注高手等分初始资金，独立核算（高手多为满仓单票轮动，混池会失真）。
  * 信号→目标：同一专家同一股票同一交易日的多条操作，取**日终净目标权重**（weight_after
    的最后一条），次日一次性调仓——天然规避 T+1 当日买卖限制与日内来回刷单。
  * 执行：信号日次一交易日开盘价 ± 滑点0.002；费率与模拟盘一致（佣金0.0008，卖出加印花税0.0005）。
  * 涨停拦截：开盘≥前收×1.098(主板)/×1.198(创业板/科创) 时放弃买入（追不进，如实跳过）。
  * 噪音过滤：目标权重<1% 跳过；卖出后目标权重<1% 视为清仓。
  * 期末：最后交易日收盘价市值结算。
  * 基准：沪深300(000300.SH)，同窗口对照。

独立小引擎，不进 xcq_backtest_runs v1/v2（项目约定两引擎表不混入非引擎产出）。
结果落 data/follow_backtest/<run_id>.json + Markdown 报告。

用法:
  python -m strategy.follow_backtest \
      [--capital 1000000] [--start 2025-01-01] [--end 2026-09-23] [--expert-id 25271...]
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.logger import logger, setup_logging
from paths import FOLLOW_BT_DIR

# 与模拟盘 paper_trading 一致的交易成本口径
SLIPPAGE = 0.002          # 滑点（双边）
COMMISSION = 0.0008       # 佣金（双边）
STAMP_TAX = 0.0005        # 印花税（仅卖出）
MIN_WEIGHT = 0.01         # 目标权重<1% 视为噪音跳过/清仓
MIN_FEE = 5.0             # 佣金最低 5 元

# 涨停幅度: 创业板(300/301)与科创板(688/689) 20%, 其余 10%
def _limit_up_ratio(code: str) -> float:
    return 1.198 if str(code).startswith(("300", "301", "688", "689")) else 1.098


# ── 数据加载 ─────────────────────────────────────────────────────────────────

def load_signals(expert_ids: List[str], start: str, end: str) -> List[Dict[str, Any]]:
    """关注高手在窗口内的全部带标的操作（含每日流水与历史回补，dedup 已保证不重）。"""
    from core.db import q
    ids = ",".join(f"'{e}'" for e in expert_ids)
    rows = q(f"""SELECT expert_id, expert_name, trade_date, trade_time, stock_code, stock_name,
                        action, price, weight_before, weight_after
                 FROM crawl_trade
                 WHERE expert_id IN ({ids}) AND stock_code<>'' AND stock_code IS NOT NULL
                   AND trade_date >= '{start}' AND trade_date <= '{end}'
                 ORDER BY expert_id, trade_date, trade_time""")
    return rows


def load_kline_map(codes: List[str], start: str, end: str) -> Dict[str, List[Dict[str, Any]]]:
    """code -> [{date, open, close, prev_close}]（按日期升序；prev_close 用于涨停判定）。

    价格源优先本地价格库（strategy.prices 的 price_daily，原始开/收盘，经
    strategy.pit_nav._prices 读取，与原 qlib 版 scripts/selectors/selector_kline_utils
    .preload_klines 同角色）——xcq_stock_kline 表实测有洞（如 301520 仅 3 行、000993 缺
    2026-09-17 之后），不能单独依赖；本地价格库不可用时回退 DB 表。
    """
    try:
        from strategy import pit_nav as PN
        p = PN._prices(codes, start, end, fields=("open", "close"))
        if len(p.dates):
            out: Dict[str, List[Dict[str, Any]]] = {}
            for j, code in enumerate(p.codes):
                lst: List[Dict[str, Any]] = []
                for i, d in enumerate(p.dates):
                    o, c = float(p.f["open"][i, j]), float(p.f["close"][i, j])
                    if o != o or c != c:      # NaN（停牌/缺失，与原 preload_klines 的跳过一致）
                        continue
                    lst.append({"date": d, "open": o, "close": c,
                                "prev_close": lst[-1]["close"] if lst else None})
                if lst:
                    out[code] = lst
            if out:
                logger.info(f"[follow-backtest] K线来自本地价格库: {len(out)} 只, "
                            f"共 {sum(len(v) for v in out.values())} 行")
                return out
    except Exception as e:
        logger.warning(f"[follow-backtest] 本地价格库加载失败，回退 xcq_stock_kline 表: {e}")

    from core.db import q
    cs = ",".join(f"'{c}'" for c in codes)
    rows = q(f"""SELECT stock_code, trade_date, open_price, close_price
                 FROM xcq_stock_kline
                 WHERE stock_code IN ({cs}) AND trade_date >= '{start}' AND trade_date <= '{end}'
                 ORDER BY stock_code, trade_date""")
    out = defaultdict(list)
    for r in rows:
        lst = out[r["stock_code"]]
        prev_close = lst[-1]["close"] if lst else None
        lst.append({"date": str(r["trade_date"]), "open": float(r["open_price"]),
                    "close": float(r["close_price"]), "prev_close": prev_close})
    return dict(out)


def load_index(index_code: str, start: str, end: str) -> Dict[str, float]:
    from strategy import prices
    return prices.index_closes(index_code, start, end)


def next_trading_day(kline_by_date: Dict[str, Dict[str, Any]], d: str) -> Optional[str]:
    """在合并后的交易日历（任一持仓/信号股票的 kline 日期并集）里找 d 的下一交易日。"""
    dates = sorted(kline_by_date.keys())
    import bisect
    i = bisect.bisect_right(dates, d)
    return dates[i] if i < len(dates) else None


# ── 回测核心 ─────────────────────────────────────────────────────────────────

def run_backtest(capital: float, start: str, end: str, expert_filter: Optional[str],
                 market_type: Optional[str] = None,
                 expert_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    from core.db import q
    if market_type == "live":
        # [2026-09-25] 实盘回测: 用全部有流水的实盘组合 (DB 77 个), 不限于关注列表
        experts = [(r["expert_id"], r.get("expert_name") or r["expert_id"]) for r in
                   q("SELECT DISTINCT expert_id, MAX(expert_name) AS expert_name FROM expert_trades "
                     "WHERE market_type='live' AND stock_code<>'' GROUP BY expert_id "
                     "ORDER BY expert_id")]
    else:
        wl = q("SELECT expert_id, name, market_type FROM xcq_expert_watchlist")
        experts = [(r["expert_id"], r.get("name") or r["expert_id"])
                   for r in wl if (not expert_filter or r["expert_id"] == expert_filter)]
    if expert_ids:
        want = set(expert_ids)
        experts = [(e, n) for e, n in experts if e in want]
    experts = [(e, n) for e, n in experts if not expert_filter or e == expert_filter]
    if not experts:
        raise RuntimeError("没有可回测的高手——先回补实盘流水或添加关注")

    signals = load_signals([e for e, _ in experts], start, end)
    codes = sorted({r["stock_code"] for r in signals})
    kline = load_kline_map(codes, start, end)

    # 交易日历（并集）
    cal: Dict[str, Dict[str, Any]] = {}
    for code, lst in kline.items():
        for k in lst:
            cal[k["date"]] = k
    # 每股票按日期索引
    kidx: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for code, lst in kline.items():
        kidx[code] = {k["date"]: k for k in lst}

    # 信号 → 每专家每日净目标权重
    # targets[expert][date][code] = (target_weight, stock_name, signal_time)
    targets: Dict[str, Dict[str, Dict[str, Tuple[float, str, str]]]] = defaultdict(lambda: defaultdict(dict))
    sig_meta: Dict[str, str] = {e: n for e, n in experts}
    n_signals = len(signals)
    for r in signals:
        e, d, c = r["expert_id"], str(r["trade_date"]), r["stock_code"]
        wa = float(r["weight_after"]) if r["weight_after"] is not None else None
        if wa is None:
            continue
        targets[e][d][c] = (wa / 100.0, r["stock_name"] or c, str(r["trade_time"] or ""))

    # 执行日倒排索引: exec_day[信号日][expert][code] = target（信号日的次一交易日执行）
    # 需要先有交易日历，故在 cal 建好后构建（见下方 all_dates 计算后）

    # 逐日执行（按交易日历推进）
    all_dates = sorted(cal.keys())
    per_expert_cash: Dict[str, float] = {e: capital / len(experts) for e, _ in experts}
    # positions[expert][code] = {"qty": 股数, "cost": 摊薄成本}
    positions: Dict[str, Dict[str, Dict[str, float]]] = defaultdict(dict)
    trades: List[Dict[str, Any]] = []
    nav_daily: List[Dict[str, Any]] = []
    fees_total = 0.0
    skipped_limitup = 0

    def _holdings_value(e: str, d: str) -> float:
        v = 0.0
        for code, pos in positions[e].items():
            k = kidx.get(code, {}).get(d)
            if k:
                v += pos["qty"] * k["close"]
        return v

    def _expert_nav(e: str, d: str) -> float:
        return per_expert_cash[e] + _holdings_value(e, d)

    # 执行日映射: exec_day[执行日][expert][code] = target（信号日的次一交易日执行）
    import bisect
    exec_day: Dict[str, Dict[str, Dict[str, Tuple[float, str, str]]]] = defaultdict(lambda: defaultdict(dict))
    for e, by_date in targets.items():
        for sd, code_map in by_date.items():
            i = bisect.bisect_right(all_dates, sd)
            if i >= len(all_dates):
                continue  # 信号日晚于窗口末尾，无执行日
            ed = all_dates[i]
            for c, t in code_map.items():
                exec_day[ed][e][c] = t  # 同一执行日多条信号取后到者（日终净目标语义）

    for i_d, d in enumerate(all_dates):
        prev_d = all_dates[i_d - 1] if i_d > 0 else None
        # ① 当日应执行 = 昨日信号对应的净目标（T+1 开盘执行）
        for e in per_expert_cash:
            day_targets = exec_day.get(d, {}).get(e, {})
            for code, (tw, sname, stime) in day_targets.items():
                k = kidx.get(code, {}).get(d)
                if not k:
                    continue  # 停牌/无K线，顺延不了就放弃（记录）
                pos = positions[e].get(code)
                cur_qty = pos["qty"] if pos else 0
                # 前一日 NAV（首日用初始资金）——目标权重以此为基准
                nav_prev = _expert_nav(e, prev_d) if prev_d else capital / len(experts)
                # 噪音过滤: 目标<1% 视为清仓
                eff_tw = tw if tw >= MIN_WEIGHT else 0.0
                tgt_val = eff_tw * nav_prev
                cur_val = cur_qty * k["open"]
                if abs(tgt_val - cur_val) < max(500.0, nav_prev * 0.002):
                    continue  # 变动太小（<0.2% NAV 或 <500元），省手续费不调仓
                if tgt_val > cur_val:
                    # ── 买入/加仓 ──
                    if k["prev_close"] and k["open"] >= k["prev_close"] * _limit_up_ratio(code):
                        skipped_limitup += 1
                        trades.append({"expert": sig_meta[e], "code": code, "name": sname,
                                       "side": "buy_skipped", "date": d,
                                       "reason": f"开盘涨停({k['open']:.2f} ≥ {k['prev_close']:.2f}×{_limit_up_ratio(code)})",
                                       "signal_time": stime})
                        continue
                    buy_val = min(tgt_val - cur_val, per_expert_cash[e])
                    if buy_val < 1000:
                        continue
                    px = k["open"] * (1 + SLIPPAGE)
                    qty = math.floor(buy_val / px / 100) * 100
                    if qty <= 0:
                        continue
                    gross = qty * px
                    fee = max(gross * COMMISSION, MIN_FEE)
                    if gross + fee > per_expert_cash[e]:
                        qty = max(0, math.floor((per_expert_cash[e] - MIN_FEE) / px / 100) * 100)
                        gross = qty * px
                        fee = max(gross * COMMISSION, MIN_FEE)
                        if qty <= 0:
                            continue
                    per_expert_cash[e] -= gross + fee
                    fees_total += fee
                    old = positions[e].get(code)
                    new_qty = (old["qty"] if old else 0) + qty
                    new_cost = ((old["qty"] * old["cost"]) if old else 0) + gross
                    positions[e][code] = {"qty": new_qty, "cost": new_cost / max(new_qty, 1)}
                    trades.append({"expert": sig_meta[e], "code": code, "name": sname, "side": "buy",
                                   "date": d, "price": round(px, 3), "qty": qty,
                                   "amount": round(gross, 2), "fee": round(fee, 2),
                                   "signal_time": stime})
                elif cur_qty > 0:
                    # ── 卖出/减仓 ──
                    px = k["open"] * (1 - SLIPPAGE)
                    tgt_qty = math.floor(tgt_val / px / 100) * 100
                    sell_qty = cur_qty - max(tgt_qty, 0)
                    if sell_qty <= 0:
                        continue
                    gross = sell_qty * px
                    fee = max(gross * COMMISSION, MIN_FEE) + gross * STAMP_TAX
                    per_expert_cash[e] += gross - fee
                    fees_total += fee
                    pos = positions[e][code]
                    entry = pos["cost"]
                    pos["qty"] -= sell_qty
                    realized = (px - entry) * sell_qty - fee
                    trades.append({"expert": sig_meta[e], "code": code, "name": sname, "side": "sell",
                                   "date": d, "price": round(px, 3), "qty": sell_qty,
                                   "amount": round(gross, 2), "fee": round(fee, 2),
                                   "realized_pnl": round(realized, 2),
                                   "entry_cost": round(entry, 3),
                                   "return_pct": round((px / entry - 1) * 100, 2) if entry else None,
                                   "signal_time": stime})
                    if pos["qty"] <= 0:
                        del positions[e][code]

        # ② 日终净值
        total_nav = sum(_expert_nav(e, d) for e in per_expert_cash)
        nav_daily.append({"date": d, "nav": round(total_nav, 2),
                          "cash": round(sum(per_expert_cash.values()), 2),
                          "position_value": round(total_nav - sum(per_expert_cash.values()), 2)})

    last_d = all_dates[-1]
    final_nav = nav_daily[-1]["nav"] if nav_daily else capital

    # ── 指标 ──
    rets: List[float] = []
    ret_dates: List[str] = []
    for i in range(1, len(nav_daily)):
        prev = nav_daily[i - 1]["nav"]
        if prev > 0:
            rets.append(nav_daily[i]["nav"] / prev - 1)
            ret_dates.append(nav_daily[i]["date"])
    cum = 1.0
    peak = 1.0
    mdd = 0.0
    for r in rets:
        cum *= (1 + r)
        peak = max(peak, cum)
        mdd = max(mdd, 1 - cum / peak)
    d0 = date.fromisoformat(nav_daily[0]["date"]) if nav_daily else date.today()
    d1 = date.fromisoformat(nav_daily[-1]["date"]) if nav_daily else date.today()
    days = max((d1 - d0).days, 1)
    total_ret = final_nav / capital - 1
    annualized = (1 + total_ret) ** (365 / days) - 1 if total_ret > -1 else -1.0
    mean_r = sum(rets) / len(rets) if rets else 0.0
    std_r = (sum((x - mean_r) ** 2 for x in rets) / max(len(rets) - 1, 1)) ** 0.5 if len(rets) > 1 else 0.0
    sharpe = (mean_r / std_r) * math.sqrt(252) if std_r > 0 else 0.0

    sells = [t for t in trades if t["side"] == "sell"]
    wins = [t for t in sells if (t.get("return_pct") or 0) > 0]
    losses = [t for t in sells if (t.get("return_pct") or 0) <= 0]
    avg_win = (sum(t["return_pct"] for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t["return_pct"] for t in losses) / len(losses)) if losses else 0.0

    # 基准（沪深300）
    idx = load_index("000300.SH", start, end)
    bench_dates = [d for d in all_dates if d in idx]
    bench_ret = None
    bench_curve: List[Dict[str, Any]] = []
    if bench_dates:
        b0 = idx[bench_dates[0]]
        for d in all_dates:
            if d in idx:
                bench_curve.append({"date": d, "value": round(idx[d] / b0 * capital, 2)})
        b_last = idx.get(last_d) or idx[bench_dates[-1]]
        bench_ret = b_last / idx[bench_dates[0]] - 1

    # β调整后的α (D25口径, 对中证500回归, 与pipeline/wf_config闸门同一套 E1_RULES 比较;
    # 展示用基准仍是沪深300, 两者用途不同——参考 arena.py::_wf_rows 对同一份 alpha_mod 的用法)
    al: Optional[Dict[str, float]] = None
    try:
        from strategy import alpha as alpha_mod
        csi500 = alpha_mod.bench_returns(start, end)
        al = alpha_mod.regress(ret_dates, rets, csi500)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[follow-backtest] alpha 回归失败: {e}")

    # 分专家归因
    by_expert: Dict[str, Dict[str, Any]] = {}
    for e, name in experts:
        sub_cap = capital / len(experts)
        sells_e = [t for t in sells if t["expert"] == name]
        hold_val = _holdings_value(e, last_d)
        by_expert[name] = {
            "capital": round(sub_cap, 2),
            "realized_pnl": round(sum(t.get("realized_pnl") or 0 for t in sells_e), 2),
            "closed_trades": len(sells_e),
            "win_trades": len([t for t in sells_e if (t.get("return_pct") or 0) > 0]),
            "holding_value": round(hold_val, 2),
            "holding_codes": sorted(positions[e].keys()),
        }

    return {
        "run_id": datetime.now().strftime("%Y%m%d_%H%M%S"),
        "params": {"capital": capital, "start": start, "end": end,
                   "market_type": market_type or "watchlist",
                   "experts": [n for _, n in experts], "expert_ids": [e for e, _ in experts],
                   "signals": n_signals,
                   "slippage": SLIPPAGE, "commission": COMMISSION, "stamp_tax": STAMP_TAX,
                   "execution": "T+1开盘价", "sizing": "权重镜像(日终净目标)"},
        "metrics": {
            "initial_capital": capital, "final_nav": round(final_nav, 2),
            "total_return_pct": round(total_ret * 100, 2),
            "annualized_return_pct": round(annualized * 100, 2),
            "max_drawdown_pct": round(mdd * 100, 2),
            "sharpe": round(sharpe, 2),
            "calendar_days": days, "trading_days": len(nav_daily),
            "n_trades": len([t for t in trades if t["side"] in ("buy", "sell")]),
            "n_buy": len([t for t in trades if t["side"] == "buy"]),
            "n_sell": len(sells),
            "win_rate_pct": round(len(wins) / len(sells) * 100, 1) if sells else None,
            "avg_win_pct": round(avg_win, 2), "avg_loss_pct": round(avg_loss, 2),
            "fees_total": round(fees_total, 2),
            "skipped_limitup_buys": skipped_limitup,
            "benchmark_return_pct": round(bench_ret * 100, 2) if bench_ret is not None else None,
            "excess_return_pct": round(total_ret * 100 - bench_ret * 100, 2) if bench_ret is not None else None,
            "alpha_pct": round(al["alpha"], 2) if al else None,
            "beta": round(al["beta"], 3) if al else None,
            "alpha_t": round(al["alpha_t"], 2) if al else None,
        },
        "by_expert": by_expert,
        "trades": trades,
        "nav_daily": nav_daily,
        "benchmark_curve": bench_curve,
    }


def write_report(res: Dict[str, Any]) -> Path:
    FOLLOW_BT_DIR.mkdir(parents=True, exist_ok=True)
    fp = FOLLOW_BT_DIR / f"follow_backtest_{res['run_id']}.json"
    fp.write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    logger.info(f"[follow-backtest] 结果已写入 {fp}")
    _persist_summary(res, fp)
    return fp


def _persist_summary(res: Dict[str, Any], fp: Path) -> None:
    """把汇总指标写进 xcq_expert_follow_backtest_runs, 供 arena.gate_for("expert_follow", ...)
    查询接入模拟盘闸门——逐笔明细/净值曲线仍只在上面的 JSON 里(不重复入库)。"""
    from core.db import qexec

    p, m = res["params"], res["metrics"]
    ids = ",".join(p.get("expert_ids") or [])[:900]   # 实盘池 77+ 组合会超列宽, 截断(全量在 JSON)

    def _n(v: Any) -> str:
        return "NULL" if v is None else str(v)

    try:
        qexec(f"""INSERT OR IGNORE INTO xcq_expert_follow_backtest_runs
                  (run_id, expert_ids, start_date, end_date, total_return, annualized_return,
                   sharpe_ratio, max_drawdown, win_rate, total_trades, benchmark_return,
                   alpha, beta, alpha_t, json_path)
                  VALUES ('{res['run_id']}', '{ids}', '{p['start']}', '{p['end']}',
                          {_n(m.get('total_return_pct'))}, {_n(m.get('annualized_return_pct'))},
                          {_n(m.get('sharpe'))}, {_n(m.get('max_drawdown_pct'))},
                          {_n(m.get('win_rate_pct'))}, {_n(m.get('n_sell'))},
                          {_n(m.get('benchmark_return_pct'))}, {_n(m.get('alpha_pct'))},
                          {_n(m.get('beta'))}, {_n(m.get('alpha_t'))}, '{fp}')""")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[follow-backtest] 汇总指标入库失败(不影响JSON结果): {e}")


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="跟随社区高手策略回测")
    ap.add_argument("--capital", type=float, default=1000000.0)
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2026-09-23")
    ap.add_argument("--expert-id", default="", help="只回测某位关注高手")
    ap.add_argument("--market-type", default="", choices=["", "live", "sim"],
                    help="live=全部实盘组合(不限于关注列表); 空=关注列表")
    args = ap.parse_args()
    setup_logging()
    res = run_backtest(args.capital, args.start, args.end, args.expert_id or None,
                       market_type=args.market_type or None)
    fp = write_report(res)
    m = res["metrics"]
    print(json.dumps(m, ensure_ascii=False, indent=2))
    print(f"\n结果文件: {fp}")


if __name__ == "__main__":
    main()
