"""迁移自 scripts/experts/follow_backtest_v2.py (2026-10-16)。变更 (仅导入路径/存储后端/文件路径, 业务逻辑逐行保持):
  * 导入: scripts.* -> core.* / strategy.* (pit_nav / trade_costs / exit_rules / exit_policy /
    follow_backtest.load_index / recs.alpha -> strategy.alpha);
  * 路径: OUT_DIR -> paths.FOLLOW_BT_DIR; LEGACY_HIST_DIR -> paths.HISTORY_DIR (回补逻辑不变);
  * persist: qexec 走 core.db; ON DUPLICATE KEY UPDATE run_id=run_id (MySQL 空更新) -> INSERT OR IGNORE
    (SQLite 等价); ensure_expert_follow_strategy_col 删除 (SQLite 无需) —— 目标表
    xcq_expert_follow_backtest_runs 本项目暂无读取方且未建表, 写入失败由调用方 try/except 吞掉;
  * 关注列表 SQL 原样 (xcq_expert_watchlist 表与 strategy/engine.py 的约定一致, 由 watchlist 侧负责)。

「跟随社区高手 v2」回测 (2026-09-24)。

v2 相对 v1 的升级:
  1. 跟随集合 = 关注列表高手 ∪ 每日动态收益排名前 N(默认10)的社区高手;
     排名只用前一交易日收盘及以前的数据(PIT, 见 strategy/pit_nav.py 的逐日净值重建)。
  2. 资金按高手收益排名加权(线性名次权重): 集合内按同一指标排序, 第 k 名权重 ∝ (M+1-k),
     收益高的高手分到的资金多, 收益低的少——不再是 v1 的每人等分。
  3. 目标持仓 = Σ_高手 资金权重 × 该高手的仓位权重(镜像其持仓); 单票上限 20%, <1% 视为噪音。
     同一只票被多位高手持有时仓位叠加。高手掉出前 N → 它贡献的目标仓位归零 → 卖出。

两种执行口径(都算, 如实对比):
  t1_open   信号日(T)收盘后算目标, T+1 开盘价 ± 0.2% 滑点调仓 (与 v1 相同, 保守)
  intraday  开盘先按昨收目标调仓(处理名单变化), 盘中跟随集合内高手的每笔调仓:
            在高手成交价上加 0.3% 追价滑点同日成交(近似"监控 5 分钟内跟单");
            A 股 T+1: 当天买入的部分当天不能卖
共同: 佣金 0.08%(最低5元) + 卖出印花税 0.05%; 开盘/成交价触及涨停不买、跌停不卖;
      调仓变化 < 0.5% 净值不动(省手续费); 按后复权价做价值记账(不做 100 股取整)。

⚠️ 幸存者偏差: 可选高手名单 = 2026-09 中下旬出现在东财高手流水里的组合(约 4 千个)。
   PIT 排名消掉了"用当前收益率选人"这层前视, 但名单本身是事后幸存者, 回测收益会偏乐观。
   结论只能当上限参考, 真正的检验看模拟盘前向表现。

用法:
  python -m strategy.backtest_v2 [--start 2026-02-02] [--end 2026-09-23] [--variants all]
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from core.logger import logger, setup_logging
from strategy import pit_nav as PN

from paths import FOLLOW_BT_DIR, HISTORY_DIR

from core import trade_costs as _TC   # [2026-09-26 D2] 成本统一: 佣金万2.5最低5元/卖出印花税0.05%/滑点0.2%
SLIP_OPEN = _TC.SLIPPAGE
SLIP_INTRA = 0.003
COMMISSION = _TC.COMMISSION
STAMP_TAX = _TC.STAMP_TAX_SELL
MIN_FEE = _TC.MIN_COMMISSION
# 返回的成交明细条数 (页面展示用 500; 研究脚本需要全部入场样本时临时设 0 = 全部) [2026-09-29]
TRADES_KEEP = 500
STOCK_CAP = 0.20
MIN_TARGET = 0.01
REBAL_MIN = 0.005
RET_WINDOW = {"day": 1, "ret20": 20}

VARIANTS = {
    "v1_equiv":     {"top_n": 0,  "metric": "ret20", "weighting": "equal", "exec": "t1_open",
                     "label": "v1口径(仅关注列表·等权·T+1开盘)"},
    "v2_day_t1":    {"top_n": 10, "metric": "day",   "weighting": "rank",  "exec": "t1_open",
                     "label": "v2·关注+当日收益前10·名次加权·T+1开盘"},
    "v2_ret20_t1":  {"top_n": 10, "metric": "ret20", "weighting": "rank",  "exec": "t1_open",
                     "label": "v2·关注+近20日收益前10·名次加权·T+1开盘"},
    "v2_day_intra": {"top_n": 10, "metric": "day",   "weighting": "rank",  "exec": "intraday",
                     "label": "v2·关注+当日收益前10·名次加权·盘中跟单"},
    "v2_ret20_intra": {"top_n": 10, "metric": "ret20", "weighting": "rank", "exec": "intraday",
                       "label": "v2·关注+近20日收益前10·名次加权·盘中跟单"},
}


def _limit(code: str) -> float:
    c = str(code)
    if c.startswith(("300", "301", "688", "689")):
        return 0.20
    if c.startswith(("4", "8", "92")):
        return 0.30
    return 0.10


def load_all_histories(watch_ids: List[str], env: str = "sim") -> Dict[str, Dict[str, Any]]:
    h = PN.load_histories(env)
    if env != "live":                                    # 关注高手若不在批量名单里, 用关注列表回补的历史(仅模拟盘 legacy)
        for eid in watch_ids:
            if eid in h:
                continue
            fp = HISTORY_DIR / f"{eid}.json"
            if fp.exists():
                d = json.loads(fp.read_text(encoding="utf-8"))
                recs = [r for r in d.get("records") or [] if r.get("trade_date") and r.get("stock_code")]
                recs.sort(key=lambda r: r.get("trade_time") or r["trade_date"])
                h[eid] = {"name": d.get("name") or eid, "records": recs}
    return h


class Ctx:
    """一次性准备: 价格、全部高手净值、排名指标; 持仓按需重建并缓存。"""

    def __init__(self, end: str, watch: List[Tuple[str, str]], env: str = "sim",
                 ids: Optional[List[str]] = None, start: Optional[str] = None):
        self.env = env
        self.watch = [e for e, _ in watch]
        self.watch_names = dict(watch)
        self.hist = load_all_histories(self.watch, env)
        if ids:                                              # [2026-09-25] 限定高手宇宙 (如: 只跟关注3人)
            want = set(ids)
            self.hist = {e: h for e, h in self.hist.items() if e in want}
            self.watch = [e for e in self.watch if e in want]
        codes = sorted({r["stock_code"] for h in self.hist.values() for r in h["records"]})
        self.px = PN.PriceCtx(codes, end, start=start or (PN.NAV_START_LIVE if env == "live" else PN.NAV_START))   # [2026-09-27] start: 实盘模仿模型用全部流水 (2024-07 起)
        self.live_health: Optional[Dict[str, Any]] = None
        if env == "live":   # [2026-09-25] 实盘 rtV3 比例 → 调仓前后仓位; 无原文/起点不明的组合剔除 (data_health 标不可用)
            self.hist, self.live_health = PN.normalize_live(self.hist, self.px)
            self.watch = [e for e in self.watch if e in self.hist]
        self.experts = sorted(self.hist)
        T = len(self.px.dates)
        self.nav = np.full((T, len(self.experts)), np.nan)
        for i, e in enumerate(self.experts):
            self.nav[:, i], _ = PN.replay_expert(self.hist[e]["records"], self.px)
        self.eidx = {e: i for i, e in enumerate(self.experts)}
        # 最近一次有调仓记录的日下标(过滤长期不动的僵尸组合)
        self.last_trade = np.full((T, len(self.experts)), -1)
        for i, e in enumerate(self.experts):
            ts = sorted({self.px.day_of(r["trade_date"]) for r in self.hist[e]["records"]} - {None})
            k = 0
            last = -1
            for t in range(T):
                while k < len(ts) and ts[k] <= t:
                    last = ts[k]
                    k += 1
                self.last_trade[t, i] = last
        self._hold: Dict[str, List[Dict[int, float]]] = {}
        self._events: Dict[str, Dict[int, List[Dict[str, Any]]]] = {}
        logger.info(f"[follow-v2] 准备完成: {len(self.experts)} 个组合, {len(codes)} 只股票, {T} 个交易日")

    def metric(self, name: str, t: int) -> np.ndarray:
        k = RET_WINDOW[name]
        if t - k < 0:
            return np.full(len(self.experts), np.nan)
        m = self.nav[t] / self.nav[t - k] - 1
        m[(t - self.last_trade[t]) > 60] = np.nan        # 60 个交易日没调过仓的僵尸组合不参与排名
        m[np.abs(m) >= 0.5 * k] = np.nan                 # 重建异常(单日被截断到±50%)不参与
        return m

    def holdings(self, eid: str) -> List[Dict[int, float]]:
        if eid not in self._hold:
            _, h = PN.replay_expert(self.hist[eid]["records"], self.px, keep_holdings=True)
            self._hold[eid] = h
        return self._hold[eid]

    def events(self, eid: str) -> Dict[int, List[Dict[str, Any]]]:
        if eid not in self._events:
            ev: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
            for r in self.hist[eid]["records"]:
                t = self.px.day_of(r["trade_date"])
                if t is not None and r["stock_code"] in self.px.cidx:
                    ev[t].append(r)
            self._events[eid] = ev
        return self._events[eid]


def follow_set(ctx: Ctx, t_sig: int, cfg: Dict[str, Any]) -> List[Tuple[str, float, float]]:
    """t_sig 收盘后确定的跟随集合: [(expert_id, 资金权重, 指标值)], 按指标降序。"""
    m = ctx.metric(cfg["metric"], t_sig)
    members: Dict[str, float] = {}
    for e in ctx.watch:
        if e in ctx.eidx:
            v = m[ctx.eidx[e]]
            members[e] = float(v) if np.isfinite(v) else -np.inf
    if cfg["top_n"]:
        valid = np.where(np.isfinite(m))[0]
        for i in valid[np.argsort(-m[valid])]:
            if len([x for x in members if x not in ctx.watch]) >= cfg["top_n"]:
                break
            e = ctx.experts[i]
            if e not in members:
                members[e] = float(m[i])
    order = sorted(members, key=lambda e: -members[e])
    M = len(order)
    if not M:
        return []
    if cfg["weighting"] == "rank":
        raw = [M - k for k in range(M)]
    else:
        raw = [1.0] * M
    s = float(sum(raw))
    return [(e, raw[k] / s, members[e]) for k, e in enumerate(order)]


def target_weights(ctx: Ctx, fs: List[Tuple[str, float, float]], t_hold: int) -> Dict[int, float]:
    tw: Dict[int, float] = defaultdict(float)
    for e, we, _ in fs:
        for j, w in ctx.holdings(e)[t_hold].items():
            tw[j] += we * w
    return {j: min(STOCK_CAP, w) for j, w in tw.items() if w >= MIN_TARGET}


def run_variant(ctx: Ctx, cfg: Dict[str, Any], start: str, end: str, capital: float = 1_000_000.0,
                debug: bool = False) -> Dict[str, Any]:
    px = ctx.px
    days = [t for t, d in enumerate(px.dates) if start <= d <= end]
    adj, fac = px.adj, px.fac
    # 开盘价(后复权): 单独读一次 open 字段, 按 ctx 缓存 (v3 随机对照会连跑几十次)
    opn = getattr(ctx, "_opn_cache", None)
    if opn is None:
        p_open = PN._prices(px.codes, px.dates[0], px.dates[-1], fields=("open", "high", "low"))
        o_pos = {c: j for j, c in enumerate(p_open.codes)}
        opn = np.full(adj.shape, np.nan, dtype=np.float64)
        hi_a = np.full(adj.shape, np.nan, dtype=np.float64)
        lo_a = np.full(adj.shape, np.nan, dtype=np.float64)
        for c, j in px.cidx.items():
            if c in o_pos:
                opn[:, j] = p_open.f["open"][:, o_pos[c]] * fac[:, j]
                hi_a[:, j] = p_open.f["high"][:, o_pos[c]] * fac[:, j]
                lo_a[:, j] = p_open.f["low"][:, o_pos[c]] * fac[:, j]
        ctx._opn_cache, ctx._hi_cache, ctx._lo_cache = opn, hi_a, lo_a
    # [2026-09-26 D1] 止损/止盈/时间平仓 (与模拟盘同一份规则 core/exit_rules.py); cfg["exits"]=(止损比例, 止盈参数|None)
    exits = cfg.get("exits")
    hi_a, lo_a = getattr(ctx, "_hi_cache", None), getattr(ctx, "_lo_cache", None)
    entry_t: Dict[int, int] = {}
    pcost: Dict[int, float] = {}          # 不含滑点的持仓均价 (模拟盘 cost_price 口径)
    top: Dict[int, float] = {}            # 入场以来最高价
    tp_pending: Dict[int, str] = {}       # [2026-09-28 v2] 收盘判断的止盈/满期, 次日开盘卖

    cash = capital
    qty: Dict[int, float] = defaultdict(float)          # 后复权股数(价值记账)
    cost: Dict[int, float] = {}
    trades: List[Dict[str, Any]] = []
    nav_daily: List[Dict[str, Any]] = []
    fees = 0.0
    turnover = 0.0
    skipped = {"limit_up": 0, "limit_down": 0, "t1_lock": 0}
    fs_log: List[Dict[str, Any]] = []

    def value(t: int, price: np.ndarray) -> float:
        return cash + sum(q * (price[t, j] if np.isfinite(price[t, j]) else adj[t - 1, j]) for j, q in qty.items())

    def trade(t: int, j: int, delta_val: float, p: float, slip: float, bought_today: Dict[int, float], why: str) -> None:
        nonlocal cash, fees, turnover
        code = px.codes[j]
        prev = adj[t - 1, j]
        if not (np.isfinite(p) and p > 0 and np.isfinite(prev) and prev > 0):
            return
        lim = _limit(code)
        if delta_val > 0:
            if p >= prev * (1 + lim) * 0.998:
                skipped["limit_up"] += 1
                return
            fill = p * (1 + slip)
            amt = min(delta_val, cash - MIN_FEE)
            if amt < 1000:
                return
            fee = max(amt * COMMISSION, MIN_FEE)
            if amt + fee > cash:
                amt = cash - fee
            q = amt / fill
            old_q = qty.get(j, 0.0)
            cost[j] = (cost.get(j, fill) * old_q + amt) / (old_q + q)
            pcost[j] = (pcost.get(j, p) * old_q + q * p) / (old_q + q)
            if old_q <= 0:
                entry_t[j] = t
                top[j] = p
            qty[j] = old_q + q
            bought_today[j] = bought_today.get(j, 0.0) + q
            cash -= amt + fee
        else:
            if p <= prev * (1 - lim) * 1.002:
                skipped["limit_down"] += 1
                return
            fill = p * (1 - slip)
            sellable = qty.get(j, 0.0) - bought_today.get(j, 0.0)
            q = min(-delta_val / fill, sellable)
            if q <= 0:
                skipped["t1_lock"] += 1
                return
            amt = q * fill
            fee = max(amt * COMMISSION, MIN_FEE) + amt * STAMP_TAX
            qty[j] -= q
            cash += amt - fee
            trades.append({"date": px.dates[t], "code": code, "side": "sell", "amount": round(amt, 2),
                           "return_pct": round((fill / cost.get(j, fill) - 1) * 100, 2), "why": why})
            if qty[j] * fill < 100:
                qty.pop(j, None)
                cost.pop(j, None)
                pcost.pop(j, None)
                entry_t.pop(j, None)
                top.pop(j, None)
                tp_pending.pop(j, None)
        if delta_val > 0:
            trades.append({"date": px.dates[t], "code": code, "side": "buy", "amount": round(amt, 2), "why": why})
        fees += fee
        turnover += amt

    for t in days:
        if t < 1:
            continue
        bought_today: Dict[int, float] = {}
        # cfg["follow_fn"](t_sig) → [(expert_id, 资金权重, 指标)]: v3 用它传入按日高手池/关注列表 (不传 = v2 原逻辑)
        fs = cfg["follow_fn"](t - 1) if cfg.get("follow_fn") else follow_set(ctx, t - 1, cfg)
        fs_log.append({"date": px.dates[t], "experts": [(e, round(w, 3)) for e, w, _ in fs]})
        nav_open = value(t, opn)
        # ① 开盘: 调到"昨收后的目标"
        tw = target_weights(ctx, fs, t - 1)
        cur = {j: q * (opn[t, j] if np.isfinite(opn[t, j]) else adj[t - 1, j]) for j, q in qty.items()}
        for j in sorted(set(tw) | set(cur), key=lambda j: tw.get(j, 0) - cur.get(j, 0) / nav_open):
            d = tw.get(j, 0.0) * nav_open - cur.get(j, 0.0)
            if abs(d) < REBAL_MIN * nav_open:
                continue
            trade(t, j, d, opn[t, j], SLIP_OPEN, bought_today, "open_rebalance")
        # ② 盘中跟单
        if cfg["exec"] == "intraday":
            evs = []
            for e, we, _ in fs:
                for r in ctx.events(e).get(t, []):
                    evs.append((r.get("trade_time") or "", e, we, r))
            evs.sort(key=lambda x: x[0])
            for _, e, we, r in evs:
                j = px.cidx[r["stock_code"]]
                dw = we * ((r.get("weight_after") or 0) - (r.get("weight_before") or 0)) / 100
                if abs(dw) < REBAL_MIN:
                    continue
                if dw > 0:
                    cur_w = qty.get(j, 0.0) * adj[t - 1, j] / nav_open
                    dw = min(dw, STOCK_CAP - cur_w)
                    if dw <= 0:
                        continue
                p = (r.get("price") or 0) * fac[t, j]
                trade(t, j, dw * nav_open, p, SLIP_INTRA, bought_today, "intraday_follow")
        # ③ 止损/止盈/时间平仓 (模拟盘: 开盘先执行调仓单, 再每分钟复核; 当天买入的不查)
        if exits:
            from core import exit_rules as _ER
            from core.exit_policy import semantics as _xsem
            _v2 = _xsem() == "v2"      # [2026-09-28 统一退出口径] v2: 止盈/满期 收盘判断 → 次日开盘卖; v1: 开盘/收盘当场成交 (旧)
            stop, tp = exits
            for j in list(qty):
                if entry_t.get(j, t) >= t or j in bought_today:
                    continue
                c0 = pcost.get(j)
                if not c0:
                    continue
                o, h_, l_, c_ = opn[t, j], hi_a[t, j], lo_a[t, j], adj[t, j]
                days_held = t - entry_t[j] + 1
                lvl = c0 * (1 - stop)
                sell_px, why = None, None
                if _v2 and j in tp_pending and np.isfinite(o):                   # v2: 昨天收盘判断的止盈 → 今天开盘卖
                    sell_px, why = o, tp_pending.pop(j)
                elif np.isfinite(o) and o <= lvl:                                 # 开盘即跌破止损
                    sell_px, why = o, "stop_loss"
                elif not _v2 and np.isfinite(o) and _ER.take_profit_reason(o, c0, max(top.get(j, c0), o), days_held, tp):
                    sell_px, why = o, "take_profit_open"                          # (v1) 开盘第一次复核: 时间平仓/止盈
                elif np.isfinite(l_) and l_ <= lvl:                               # 盘中跌破止损
                    sell_px, why = lvl, "stop_loss"
                else:
                    top[j] = max(top.get(j, c0), h_ if np.isfinite(h_) else c0)
                    if np.isfinite(c_) and _ER.take_profit_reason(c_, c0, top[j], days_held, tp):
                        if _v2:
                            tp_pending[j] = "take_profit"                         # v2: 次日开盘卖
                        else:
                            sell_px, why = c_, "take_profit"
                if sell_px is not None:
                    trade(t, j, -qty[j] * sell_px * 1.01, sell_px, SLIP_OPEN, bought_today, why)
                elif j in qty:
                    top[j] = max(top.get(j, c0), h_ if np.isfinite(h_) else c0)
        nv = value(t, adj)
        row = {"date": px.dates[t], "nav": round(float(nv), 2), "cash": round(float(cash), 2), "n_pos": len(qty)}
        if cfg.get("record_holdings") and nv > 0:     # [2026-09-26] 对账框架: 每日收盘持仓权重
            row["w"] = {px.codes[j]: round(float(q * adj[t, j] / nv), 5) for j, q in qty.items()
                        if np.isfinite(adj[t, j]) and q * adj[t, j] > 0}
        if debug:
            top = sorted(((float(q * adj[t, j]) if np.isfinite(adj[t, j]) else float("nan"), px.codes[j], float(q))
                          for j, q in qty.items()), key=lambda x: -abs(x[0]) if np.isfinite(x[0]) else 0)[:3]
            row["top"] = top
        nav_daily.append(row)

    return {"cfg": cfg, "metrics": _metrics(nav_daily, trades, fees, turnover, capital, skipped, start, end),
            "nav_daily": nav_daily, "trades": trades[-TRADES_KEEP:] if TRADES_KEEP else trades, "follow_sets": fs_log[-30:]}


def _metrics(nav_daily, trades, fees, turnover, capital, skipped, start, end) -> Dict[str, Any]:
    navs = [x["nav"] for x in nav_daily]
    dates = [x["date"] for x in nav_daily]
    rets = [navs[i] / navs[i - 1] - 1 for i in range(1, len(navs))]
    cum, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        cum *= 1 + r
        peak = max(peak, cum)
        mdd = max(mdd, 1 - cum / peak)
    total = navs[-1] / capital - 1 if navs else 0.0
    days = max((date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days, 1) if dates else 1
    ann = (1 + total) ** (365 / days) - 1 if total > -1 else -1.0
    mu = float(np.mean(rets)) if rets else 0.0
    sd = float(np.std(rets, ddof=1)) if len(rets) > 1 else 0.0
    sells = [t for t in trades if t["side"] == "sell"]
    wins = [t for t in sells if (t.get("return_pct") or 0) > 0]
    out = {
        "total_return_pct": round(total * 100, 2), "annualized_return_pct": round(ann * 100, 2),
        "max_drawdown_pct": round(mdd * 100, 2), "sharpe": round(mu / sd * math.sqrt(252), 2) if sd > 0 else 0.0,
        "trading_days": len(navs), "n_buy": len([t for t in trades if t["side"] == "buy"]), "n_sell": len(sells),
        "win_rate_pct": round(len(wins) / len(sells) * 100, 1) if sells else None,
        "fees_total": round(fees, 2), "fee_drag_pct": round(fees / capital * 100, 2),
        "turnover_x": round(turnover / capital, 1), "avg_positions": round(float(np.mean([x["n_pos"] for x in nav_daily])), 1) if nav_daily else 0,
        "skipped": skipped,
    }
    try:
        from strategy.follow_backtest import load_index
        idx = load_index("000300.SH", start, end)
        bd = [d for d in dates if d in idx]
        if bd:
            out["benchmark_return_pct"] = round((idx[bd[-1]] / idx[bd[0]] - 1) * 100, 2)
            out["excess_return_pct"] = round(out["total_return_pct"] - out["benchmark_return_pct"], 2)
        from strategy import alpha as alpha_mod
        al = alpha_mod.regress(dates[1:], rets, alpha_mod.bench_returns(start, end))
        if al:
            out.update({"alpha_pct": round(al["alpha"], 2), "beta": round(al["beta"], 3), "alpha_t": round(al["alpha_t"], 2)})
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[follow-v2] 基准/alpha 计算失败: {e}")
    return out


def persist(res: Dict[str, Any], key: str, fp: Path, watch_ids: List[str]) -> None:
    """汇总写 xcq_expert_follow_backtest_runs(strategy 列区分 v1/v2), 供 arena.gate_for("expert_follow_v2") 查询。"""
    from core.db import qexec
    m = res["metrics"]

    def n(v):
        return "NULL" if v is None else str(v)
    run_id = res["run_id"] + "_" + key
    qexec(f"""INSERT OR IGNORE INTO xcq_expert_follow_backtest_runs
              (run_id, strategy, expert_ids, start_date, end_date, total_return, annualized_return, sharpe_ratio,
               max_drawdown, win_rate, total_trades, benchmark_return, alpha, beta, alpha_t, json_path)
              VALUES ('{run_id[:32]}', 'v2:{key}', '{",".join(watch_ids)}', '{res['start']}', '{res['end']}',
                      {n(m.get('total_return_pct'))}, {n(m.get('annualized_return_pct'))}, {n(m.get('sharpe'))},
                      {n(m.get('max_drawdown_pct'))}, {n(m.get('win_rate_pct'))}, {n(m.get('n_sell'))},
                      {n(m.get('benchmark_return_pct'))}, {n(m.get('alpha_pct'))}, {n(m.get('beta'))},
                      {n(m.get('alpha_t'))}, '{fp}')""")


def main() -> None:
    ap = argparse.ArgumentParser(description="跟随社区高手 v2 回测")
    ap.add_argument("--start", default="2026-02-02")
    ap.add_argument("--end", default="2026-09-23")
    ap.add_argument("--variants", default="all")
    ap.add_argument("--no-persist", action="store_true")
    a = ap.parse_args()
    setup_logging()
    from core.db import q
    watch = [(r["expert_id"], r.get("name") or r["expert_id"]) for r in q("SELECT expert_id, name FROM xcq_expert_watchlist")]
    ctx = Ctx(a.end, watch)
    keys = list(VARIANTS) if a.variants == "all" else a.variants.split(",")
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary = {}
    FOLLOW_BT_DIR.mkdir(parents=True, exist_ok=True)
    for k in keys:
        r = run_variant(ctx, VARIANTS[k], a.start, a.end)
        r.update({"run_id": run_id, "variant": k, "start": a.start, "end": a.end,
                  "roster_size": len(ctx.experts), "watchlist": [n for _, n in watch]})
        fp = FOLLOW_BT_DIR / f"follow_v2_{run_id}_{k}.json"
        fp.write_text(json.dumps(r, ensure_ascii=False, default=str), encoding="utf-8")
        if not a.no_persist:
            try:
                persist(r, k, fp, [e for e, _ in watch])
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[follow-v2] 汇总入库失败: {e}")
        summary[k] = {"label": VARIANTS[k]["label"], **r["metrics"]}
        logger.info(f"[follow-v2] {k}: {summary[k]}")
    (FOLLOW_BT_DIR / f"follow_v2_{run_id}_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=float))
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=float))


if __name__ == "__main__":
    main()
