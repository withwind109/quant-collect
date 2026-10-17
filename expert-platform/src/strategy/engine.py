"""跟随高手 —— 执行引擎 (2026-09-25), 按策略文件 execution.engine 分派:

  mirror    镜像持仓: 跟随集合的实际持仓按资金权重叠加成目标仓位, 次日开盘调仓 (backtest_v2.run_variant, v1镜像/v2/v2.1/v3.1)
  signal    信号跟随: 名单高手近 N 日买入过的股票 → 等权占槽, 止损/时间平仓 (与模拟账户 expert_follow_v1 的执行口径一致;
            模拟盘另有"移动止盈", 这里不模拟 —— 回测略偏保守)
  v3_tiers  长窗口分批 + 模型: 交给 expert_v3 训练+走步回测 (strategy/v3_run.py), 这里只做参数翻译与结果整形

上下文 (高手净值/持仓/行情) 按数据环境缓存, 同一次回测里所有方案共用。

迁移自 scripts/expert_follow/engine.py (2026-10-16): core.* 与 backtest_v2 改绝对导入;
v3_tiers 分支的 scripts.expert_v3.run 改惰性导入 strategy.v3_run 并容错 (模型训练流水线未迁移);
其余逐行原样。
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from core.logger import logger
from core.perf import timed as _perf_timed

_CTX: Dict[str, Any] = {}
_LOCK = threading.Lock()


def watch_for(env: str):
    from core.db import q
    if env == "live":
        rows = q("SELECT expert_id, name FROM xcq_expert_watchlist WHERE source='eastmoney' AND market_type='live'")
    else:
        rows = q("SELECT expert_id, name FROM xcq_expert_watchlist WHERE COALESCE(market_type,'sim') <> 'live'")
    return [(r["expert_id"], r.get("name") or r["expert_id"]) for r in rows]


@_perf_timed("expert_follow.get_ctx")   # [2026-09-26] 性能测量 (core/perf.py)
def get_ctx(env: str, end: str):
    """(env, end) → backtest_v2.Ctx; 行情没到 end 时重建。

    [2026-10-16] 缓存键只按 env（原来是 env:end）：Ctx 构造后只读，同一数据环境下
    先到的查询会把面板建到各自 end，逐日调用（paper_sim 前向模拟）会每天重建一次，
    代价约 1~3s/天。改为按 env 缓存后，请求 end 超过已缓存面板末端才重建；
    更早的日期直接命中（面板是超集，语义不变）。
    """
    from strategy import backtest_v2 as FB
    with _LOCK:
        c = _CTX.get(env)
        if c is None or (end not in c.px.didx and c.px.dates[-1] < end):
            c = FB.Ctx(end, watch_for(env), env=env)
            _CTX[env] = c
        return c


def run_mirror(ctx, follow_fn: Callable[[int], list], start: str, end: str, capital: float,
               stock_cap: float = 0.20, exits=None, record_holdings: bool = False) -> Dict[str, Any]:
    """exits = (止损比例, 止盈参数|None) —— 与模拟盘同一份规则 (core/exit_rules.py); None = 不模拟止损止盈。"""
    from strategy import backtest_v2 as FB
    old = FB.STOCK_CAP
    FB.STOCK_CAP = stock_cap
    try:
        return FB.run_variant(ctx, {"exec": "t1_open", "follow_fn": follow_fn, "exits": exits,
                                    "record_holdings": record_holdings}, start, end, capital)
    finally:
        FB.STOCK_CAP = old


BUY_ACTIONS = ("买入", "加仓", "申购")


def run_signal(ctx, follow_fn: Callable[[int], list], start: str, end: str, capital: float,
               sig: Dict[str, Any], exits=None, record_holdings: bool = False) -> Dict[str, Any]:
    """v1 信号跟随。follow_fn(t_sig) 给出当天被跟的高手 (关注名单 / 随机对照)。
    exits = (止损比例, 止盈参数|None), 与模拟盘同一份规则; 缺省按 sig 的 stop_loss / max_hold。"""
    from core import exit_rules as _ER
    from strategy import backtest_v2 as FB
    px = ctx.px
    adj = px.adj
    run_mirror(ctx, lambda t: [], px.dates[1], px.dates[1], capital)       # 触发开盘/最高/最低价缓存
    opn, hi_a, lo_a = ctx._opn_cache, ctx._hi_cache, ctx._lo_cache
    days = [t for t, d in enumerate(px.dates) if start <= d <= end and t >= 1]
    L, N, P = int(sig["lookback_days"]), int(sig["top_n"]), int(sig["max_positions"])
    stop, tp = exits or (_ER.effective_stop(sig.get("stop_loss")),
                         {**_ER.TAKE_PROFIT_DEFAULTS, "max_hold_days": int(sig.get("max_hold", 20))})
    cash = capital
    pos: Dict[int, Dict[str, float]] = {}                 # j → {qty, cost(不含滑点), t_in, top}
    trades: List[Dict[str, Any]] = []
    nav_daily: List[Dict[str, Any]] = []
    fees = turnover = 0.0
    skipped = {"limit_up": 0, "limit_down": 0, "t1_lock": 0}

    def sell(t: int, j: int, why: str, p: Optional[float] = None) -> None:
        nonlocal cash, fees, turnover
        if p is None:
            p = opn[t, j] if np.isfinite(opn[t, j]) else adj[t - 1, j]
        lim = FB._limit(px.codes[j])
        if p <= adj[t - 1, j] * (1 - lim) * 1.002:
            skipped["limit_down"] += 1
            return
        fill = p * (1 - FB.SLIP_OPEN)
        amt = pos[j]["qty"] * fill
        fee = max(amt * FB.COMMISSION, FB.MIN_FEE) + amt * FB.STAMP_TAX
        cash += amt - fee
        fees += fee
        turnover += amt
        trades.append({"date": px.dates[t], "code": px.codes[j], "side": "sell", "amount": round(amt, 2),
                       "return_pct": round((fill / pos[j]["cost"] - 1) * 100, 2), "why": why})
        pos.pop(j)

    for t in days:
        # ① 开盘第一次复核 (模拟盘同口径): 开盘即跌破止损 / 时间平仓 / 止盈; 当天买入的不查
        for j in list(pos):
            o = opn[t, j]
            if not np.isfinite(o):
                continue
            c0, days_held = pos[j]["cost"], t - pos[j]["t_in"] + 1
            if o <= c0 * (1 - stop):
                sell(t, j, "stop_loss", o)
            elif _ER.take_profit_reason(o, c0, max(pos[j]["top"], o), days_held, tp):
                sell(t, j, "take_profit_open", o)
        # ② 信号: 被跟高手近 L 个交易日 (截至 t-1) 的买入, 按最近买入排序取前 N
        last_buy: Dict[int, int] = {}
        for e, _, _ in follow_fn(t - 1):
            ev = ctx.events(e)
            for tt in range(max(0, t - L), t):
                for r in ev.get(tt, []):
                    if r.get("action") in BUY_ACTIONS and float(r.get("weight_after") or 0) >= 1 \
                            and float(r.get("weight_after") or 0) > float(r.get("weight_before") or 0):
                        j = px.cidx[r["stock_code"]]
                        last_buy[j] = max(last_buy.get(j, -1), tt)
        picks = sorted(last_buy, key=lambda j: -last_buy[j])[:N]
        free = P - len(pos)
        for j in [j for j in picks if j not in pos][:max(0, free)]:
            p = opn[t, j]
            prev = adj[t - 1, j]
            if not (np.isfinite(p) and p > 0 and np.isfinite(prev) and prev > 0):
                continue
            if p >= prev * (1 + FB._limit(px.codes[j])) * 0.998:
                skipped["limit_up"] += 1
                continue
            slots = P - len(pos)
            amt = (cash - FB.MIN_FEE) / max(slots, 1)
            if amt < 1000:
                continue
            fill = p * (1 + FB.SLIP_OPEN)
            fee = max(amt * FB.COMMISSION, FB.MIN_FEE)
            pos[j] = {"qty": amt / fill, "cost": p, "t_in": t, "top": p}
            cash -= amt + fee
            fees += fee
            turnover += amt
            trades.append({"date": px.dates[t], "code": px.codes[j], "side": "buy", "amount": round(amt, 2), "why": "signal"})
        # ③ 盘中: 跌破止损 → 止损价卖; 否则收盘按移动止盈/止盈复核 (当天买入的不查)
        for j in list(pos):
            if pos[j]["t_in"] >= t:
                continue
            c0, days_held = pos[j]["cost"], t - pos[j]["t_in"] + 1
            lvl = c0 * (1 - stop)
            if np.isfinite(lo_a[t, j]) and lo_a[t, j] <= lvl:
                sell(t, j, "stop_loss", lvl)
                continue
            if np.isfinite(hi_a[t, j]):
                pos[j]["top"] = max(pos[j]["top"], hi_a[t, j])
            if np.isfinite(adj[t, j]) and _ER.take_profit_reason(adj[t, j], c0, pos[j]["top"], days_held, tp):
                sell(t, j, "take_profit", adj[t, j])
        nv = cash + sum(v["qty"] * (adj[t, j] if np.isfinite(adj[t, j]) else adj[t - 1, j]) for j, v in pos.items())
        row = {"date": px.dates[t], "nav": round(float(nv), 2), "cash": round(float(cash), 2), "n_pos": len(pos)}
        if record_holdings and nv > 0:
            row["w"] = {px.codes[j]: round(float(v["qty"] * adj[t, j] / nv), 5) for j, v in pos.items() if np.isfinite(adj[t, j])}
        nav_daily.append(row)
    return {"metrics": FB._metrics(nav_daily, trades, fees, turnover, capital, skipped, start, end),
            "nav_daily": nav_daily, "trades": trades[-FB.TRADES_KEEP:] if FB.TRADES_KEEP else trades}


def run_strategy(ctx, s: Dict[str, Any], follow_fn: Callable[[int], list], start: str, end: str,
                 capital: float, record_holdings: bool = False) -> Dict[str, Any]:
    from core import exit_rules as _ER
    eng = s["execution"]["engine"]
    exits = _ER.resolve(s.get("risk_controls"))          # [2026-09-26 D1] 与模拟盘同一套止损/止盈
    if eng == "mirror":
        return run_mirror(ctx, follow_fn, start, end, capital, s["execution"]["stock_cap"], exits, record_holdings)
    if eng == "signal":
        return run_signal(ctx, follow_fn, start, end, capital, s["execution"]["signal"], exits, record_holdings)
    raise ValueError(f"run_strategy 不处理 {eng} (v3_tiers 走 run_v3_tiers)")


def v3_spec(s: Dict[str, Any]) -> Dict[str, Any]:
    """策略文件 → expert_v3 的 spec (mode=tiers)。"""
    sc, md = s["screen"], s["models"] or {}
    return {"mode": "tiers", "data_env": s["universe"]["data_env"], "window": sc["window"],
            "min_ret": 0.0 if sc.get("min_ret") is None else sc["min_ret"], "max_mdd": sc["max_mdd"],
            "min_trades": sc["min_trades"], "recent_days": sc["recent_days"], "min_closed": sc["min_closed"],
            "min_win": sc["min_win"], "win_cost": sc["win_cost"], "min_pool": sc["min_pool"],
            "stock_cap": s["execution"]["stock_cap"],
            **{k: md[k] for k in ("pos_tiers", "neg_tiers", "label_h", "accept_q", "veto_q", "ex_q", "min_train",
                                  "stop_loss", "max_hold") if k in md}}


def run_v3_tiers(s: Dict[str, Any], end: str, status_file: Optional[str] = None, run_id: Optional[str] = None):
    # [2026-10-16] 模型训练流水线 (expert_imitation) 尚未迁移到本项目, 导入失败即明确报错
    try:
        from strategy import v3_run as R3
    except ImportError as _e:
        raise NotImplementedError("v3_tiers 引擎依赖模型训练流水线(expert_imitation), 尚未迁移到本项目") from _e
    logger.info(f"[expert-follow] v3_tiers 交给 v3_run (window={s['screen']['window']})")
    return R3.main(v3_spec(s), end, status_file, run_id)
