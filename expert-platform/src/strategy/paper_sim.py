"""模拟盘前向模拟 (2026-10-16) —— 已发布跟随策略账户的每日前向净值。

原平台由 scripts/dashboard/paper_trading.py 的自动化循环驱动（实时行情 / 条件单 /
涨跌停精细处理），未整体迁移。这里用**同一套选人与目标仓位函数**（strategy.live.compute，
与回测共用的 Selector / target_weights）按日重放：

  每个交易日 T：用截至 T-1 的数据算目标仓位（PIT，无前视）→ T+1 开盘 ±滑点调仓 → 记净值。
口径与回测一致：t1_open 执行、佣金万 2.5（最低 5 元）、卖出印花税 0.05%、滑点 0.2%、
单票上限 stock_cap、调仓变化 < 0.5% 净值不动；不含日内止损/止盈的精细模拟
（止损止盈规则已在回测 exits 里近似，前向以目标仓位口径为准，结论偏保守/中性）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import numpy as np

from core import trade_costs as TC
from core.db import q, qexec
from core.logger import logger
from strategy import backtest_v2 as FB
from strategy import config as C
from strategy import engine as E
from strategy import live as L


def _account(account_id: str) -> Optional[dict]:
    rows = q("SELECT a.id, a.name, a.initial_capital, a.enabled, a.created_at, s.strategy_id, s.spec_json "
             "FROM xcq_paper_accounts a JOIN xcq_strategies s ON s.strategy_id=a.strategy_id WHERE a.id=?",
             (account_id,))
    return rows[0] if rows else None


def _nav_rows(account_id: str) -> List[dict]:
    return q("SELECT trade_date, total_nav, cum_return_pct, cash, positions_json "
             "FROM xcq_paper_nav_daily WHERE account_id=? ORDER BY trade_date", (account_id,))


def _next_day(d: str) -> str:
    return (datetime.strptime(d[:10], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def update_account(account_id: str, end: Optional[str] = None) -> Dict[str, Any]:
    """增量更新一个模拟账户的前向净值（幂等，可反复调用）。返回 {n_days, last_date, nav}。"""
    acc = _account(account_id)
    if not acc:
        raise ValueError(f"账户不存在: {account_id}")
    spec = json.loads(acc.get("spec_json") or "{}")
    cfg_id = spec.get("config_id")
    if not cfg_id:
        raise ValueError(f"账户 {account_id} 不是策略文件发布的跟随账户 (缺 config_id)")
    s = C.load_strategy(cfg_id)
    if C.strategy_hash(s) != spec.get("config_hash"):
        raise ValueError(f"策略文件 {cfg_id} 已修改 (指纹不同), 请重新回测/发布后再累计前向证据")
    s["universe"]["data_env"] = spec.get("data_env") or s["universe"]["data_env"]
    env = s["universe"]["data_env"]

    rows = _nav_rows(account_id)
    if rows:
        start = _next_day(rows[-1]["trade_date"])
        cash = float(rows[-1]["cash"] or 0.0)
        pos: Dict[str, float] = {c: float(w) for c, w in (json.loads(rows[-1]["positions_json"] or "{}")).items()}
        initial = float(acc["initial_capital"])
    else:
        start = str(acc.get("created_at") or "")[:10] or "2015-01-01"
        cash = float(acc["initial_capital"])
        pos = {}
        initial = cash

    ctx = E.get_ctx(env, end or "9999-12-31")   # 先用最大 end 预热（按 env 缓存，循环内逐日 compute 命中同一份）
    px = ctx.px
    dates = px.dates
    if not dates:
        raise ValueError("行情/高手历史为空: 先爬取数据 (crawl + bulk-history) 并初始化价格库 (prices)")
    last = end or dates[-1]
    days = [t for t, d in enumerate(dates) if start <= d <= last and t >= 1]
    if not days:
        last_nav = float(rows[-1]["total_nav"]) if rows else initial
        return {"n_days": 0, "last_date": rows[-1]["trade_date"] if rows else None,
                "nav": round(last_nav, 2), "msg": "没有需要更新的交易日"}

    FB.run_variant(ctx, {"exec": "t1_open", "follow_fn": lambda t: []}, dates[1], dates[1], initial)  # 触发开高低缓存
    opn, adj = ctx._opn_cache, px.adj
    stock_cap = float(s["execution"].get("stock_cap") or 0.20)
    n_written = 0
    last_date, nav = (rows[-1]["trade_date"] if rows else None), initial

    for t in days:
        # T 日收盘后用截至 T 的信号, T+1 开盘执行 (与回测 t1_open 一致)
        try:
            res = L.compute(s, dates[t])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[paper-sim] {account_id} {dates[t]} 算目标失败: {exc}")
            continue
        if res is None:
            continue
        targets = res.get("targets") or []
        if not targets:
            continue
        tw = {x["code"]: float(x["weight"]) for x in targets}
        nxt = t + 1
        if nxt >= len(dates):
            break
        nav = cash + sum(pos.get(c, 0.0) * (adj[nxt, j] if np.isfinite(adj[nxt, j]) else adj[t, j])
                         for c, j in ((c, px.cidx[c]) for c in pos if c in px.cidx))
        if nav <= 0:
            continue
        # 目标市值 → 交易金额 (相对当前持仓市值)
        cur_val = {c: pos.get(c, 0.0) * (adj[nxt, j] if np.isfinite(adj[nxt, j]) else adj[t, j])
                   for c, j in ((c, px.cidx[c]) for c in pos if c in px.cidx)}
        orders: List[tuple] = []       # (code, j, delta_value)
        for c, w in tw.items():
            if c not in px.cidx:
                continue
            j = px.cidx[c]
            tgt = min(w, stock_cap) * nav
            delta = tgt - cur_val.get(c, 0.0)
            if abs(delta) < FB.REBAL_MIN * nav:
                continue
            orders.append((c, j, delta))
        for c in list(cur_val):
            if c not in tw and c in px.cidx:
                orders.append((c, px.cidx[c], -cur_val[c]))
        for c, j, delta in orders:
            o = opn[nxt, j]
            prev = adj[nxt - 1, j]
            if not (np.isfinite(o) and o > 0 and np.isfinite(prev) and prev > 0):
                continue
            if delta > 0:      # 买: 涨停不买
                if o >= prev * (1 + FB._limit(px.codes[j])) * 0.998:
                    continue
                amt = min(delta, max(cash - TC.MIN_COMMISSION, 0.0))
                if amt < 1000:
                    continue
                fill = o * (1 + FB.SLIP_OPEN)
                fee = max(amt * FB.COMMISSION, FB.MIN_FEE)
                cash -= amt + fee
                pos[c] = pos.get(c, 0.0) + amt / fill
            else:              # 卖: 跌停不卖
                if o <= prev * (1 - FB._limit(px.codes[j])) * 1.002:
                    continue
                qty = min(-delta / max(o, 1e-9), pos.get(c, 0.0))
                if qty <= 0:
                    continue
                fill = o * (1 - FB.SLIP_OPEN)
                amt = qty * fill
                fee = max(amt * FB.COMMISSION, FB.MIN_FEE) + amt * FB.STAMP_TAX
                cash += amt - fee
                pos[c] = pos.get(c, 0.0) - qty
                if pos[c] <= 1e-9:
                    pos.pop(c, None)
        nav = cash + sum(pos.get(c, 0.0) * (adj[nxt, j] if np.isfinite(adj[nxt, j]) else adj[t, j])
                         for c, j in ((c, px.cidx[c]) for c in pos if c in px.cidx))
        pos_j = {c: round(float(pos.get(c, 0.0) * (adj[nxt, j] if np.isfinite(adj[nxt, j]) else adj[t, j]) / nav), 5)
                 for c, j in ((c, px.cidx[c]) for c in pos if c in px.cidx)} if nav > 0 else {}
        last_date = dates[nxt]
        qexec("INSERT OR REPLACE INTO xcq_paper_nav_daily"
              "(account_id, trade_date, total_nav, cum_return_pct, cash, positions_json, note) "
              "VALUES (?,?,?,?,?,?,?)",
              (account_id, last_date, round(float(nav), 2), round((nav / initial - 1) * 100, 4),
               round(float(cash), 2), json.dumps(pos_j, ensure_ascii=False), "paper_sim"))
        n_written += 1
    return {"n_days": n_written, "last_date": last_date, "nav": round(float(nav), 2),
            "cum_return_pct": round((nav / initial - 1) * 100, 2)}


def update_all(end: Optional[str] = None) -> List[Dict[str, Any]]:
    """更新所有开启的跟随类模拟账户。"""
    rows = q("SELECT a.id FROM xcq_paper_accounts a JOIN xcq_strategies s ON s.strategy_id=a.strategy_id "
             "WHERE s.strategy_id LIKE 'expert_follow%' AND a.enabled=1 ORDER BY a.created_at")
    out = []
    for r in rows:
        try:
            out.append({"account_id": r["id"], **update_account(r["id"], end)})
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[paper-sim] {r['id']} 更新失败: {exc}")
            out.append({"account_id": r["id"], "error": str(exc)})
    return out


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="模拟盘前向净值模拟")
    ap.add_argument("--account", default="", help="只更新指定账户")
    ap.add_argument("--end", default="", help="数据截止日 YYYY-MM-DD")
    a = ap.parse_args()
    if a.account:
        print(update_account(a.account, a.end or None))
    else:
        for r in update_all(a.end or None):
            print(r)
