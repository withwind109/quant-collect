"""前向 vs 回测 对账 (2026-09-26, 行动计划 P1-5; 季度复盘的基础)。

对每个跟随类模拟账户 (绑定了策略文件的: v1/v2/v2.1 候选与对照/ev_live3/ev21_r3_60_live …):
  用同一份策略文件、同一本金、从账户第一个交易日起跑一遍回测 (与模拟盘同一套成本与止损止盈规则),
  逐日比较:
    收益      模拟盘日收益 vs 回测日收益, 差值与累计差
    持仓      收盘持仓权重的重合度 = 1 − ½·Σ|w_模拟盘 − w_回测| (1 = 完全一致)
    执行      模拟盘成交价相对当日开盘价的偏离 (bps, 买入为正表示买贵了)
    交易      当日成交笔数 模拟盘 vs 回测
  偏差来源判读: 持仓重合度低 → 选股/数据/规则不同; 重合度高但收益差大 → 执行价/成本/时点不同。
结果: 表 xcq_paper_recon_daily (逐日) + models/expert_follow/recon/<account>.json (汇总); 每晚由 backfill_nightly.sh 调用。
用法: python -m strategy.recon [--account ev21_r3_60]   (cwd=项目根, PYTHONPATH=src)

迁移自 scripts/expert_follow/recon.py (2026-10-16):
  * core.db / core.logger / paths 改绝对导入; OUT 由 PROJECT_ROOT/models/expert_follow/recon
    改为 paths.MODELS_DIR/expert_follow/recon;
  * scripts.recs.store → strategy.recs_store (fetch/run/run_many 同签名, ":name" 占位符,
    date/datetime 参数自动转 ISO 字符串, 因此 computed_at 的 datetime 参数原样保留);
  * scripts.recs.prices.load_bars (qlib 前复权 K 线) → _load_bars: 本地 SQLite 价格库
    (strategy.prices.load_panel 的 adjclose = 后复权, 与回测 record_holdings 的 adj 同口径;
    open/high/low/vol 取 price_daily 原始价, 供 exec_bps 成交价对比);
  * xcq_paper_recon_daily 建表 DDL 由 MySQL 方言改 SQLite (VARCHAR→TEXT, DOUBLE→REAL,
    INT→INTEGER, DATE/DATETIME→TEXT, 去 DEFAULT CHARSET/COMMENT);
  * 其余业务逻辑逐行原样 (函数内惰性导入的放置位置与源一致)。
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np

from core.db import q
from core.logger import logger
from paths import MODELS_DIR, PRICE_DB_PATH

OUT = MODELS_DIR / "expert_follow" / "recon"


def _load_bars(codes: List[str], start: str, end: str) -> Dict[str, List[tuple]]:
    """{code: [(date, open, close, high, low, vol)...]} 升序; 未取到的代码不在返回里。

    [2026-10-16] 替代源 scripts/recs/prices.load_bars (qlib 前复权): close 用
    strategy.prices.load_panel 的 adjclose (后复权, 与回测 record_holdings 的 adj 同口径),
    缺 adjclose 回退原始收盘; open/high/low/vol 用 price_daily 原始价 (exec_bps 的成交价对比口径)。
    """
    from strategy import prices
    uniq = sorted({str(c).split(".")[0] for c in codes})
    if not uniq:
        return {}
    panel = prices.load_panel(start, end, codes=uniq, fields=("adjclose",))
    didx = {d: i for i, d in enumerate(panel.dates)}
    cidx = {c: j for j, c in enumerate(panel.codes)}
    adj = panel.f["adjclose"]
    out: Dict[str, List[tuple]] = {}
    for c in uniq:
        rows = q("SELECT date, open, high, low, close, volume FROM price_daily "
                 "WHERE code=%s AND date>=%s AND date<=%s ORDER BY date", (c, start, end),
                 db_path=PRICE_DB_PATH)
        bars: List[tuple] = []
        for r in rows:
            try:
                o, h, lo = float(r["open"]), float(r["high"]), float(r["low"])
                v = float(r["volume"] or 0)
            except (TypeError, ValueError):
                continue
            if o != o or h != h or lo != lo:                # NaN
                continue
            d = str(r["date"])[:10]
            cl = adj[didx[d], cidx[c]] if (d in didx and c in cidx) else float("nan")
            if not np.isfinite(cl):
                cl = float(r["close"]) if r["close"] is not None else float("nan")
            if not np.isfinite(cl):
                continue
            bars.append((d, o, cl, h, lo, v))
        if bars:
            out[c] = bars
    return out


def ensure_schema() -> None:
    from strategy import recs_store as S
    S.run("""CREATE TABLE IF NOT EXISTS xcq_paper_recon_daily (
        account_id TEXT NOT NULL, trade_date TEXT NOT NULL,
        paper_nav REAL, bt_nav REAL, paper_ret_pct REAL, bt_ret_pct REAL, diff_pct REAL, cum_diff_pct REAL,
        overlap REAL, n_hold_paper INTEGER, n_hold_bt INTEGER, n_trades_paper INTEGER, n_trades_bt INTEGER, exec_bps REAL,
        computed_at TEXT, PRIMARY KEY (account_id, trade_date))""")


def accounts() -> List[Dict[str, Any]]:
    """跟随类账户 → 绑定的策略文件 (旧账户按 bind_strategy_id, 新账户按 spec.config_id)。"""
    from strategy import config as C
    cfgs = [c for c in C.list_strategies() if not c.get("error")]
    bind = {c["bind_strategy_id"]: c["id"] for c in cfgs if c.get("bind_strategy_id")}
    out = []
    for r in q("SELECT a.id, a.initial_capital, a.strategy_id, s.spec_json FROM xcq_paper_accounts a "
               "JOIN xcq_strategies s ON s.strategy_id=a.strategy_id WHERE s.strategy_id LIKE 'expert_follow%' AND a.enabled=1"):
        try:
            spec = json.loads(r.get("spec_json") or "{}")
        except ValueError:
            spec = {}
        cid = bind.get(r["strategy_id"]) or spec.get("config_id")
        if cid:
            out.append({"account_id": r["id"], "config_id": cid, "capital": float(r["initial_capital"] or 1_000_000),
                        "data_env": spec.get("data_env")})
    return out


def _paper_daily(acct: str, dates: List[str]) -> Dict[str, Dict[str, Any]]:
    """模拟盘逐日: 净值 + 收盘持仓权重 (由成交记录 + 分红送转调整累积出持仓股数, 按收盘价估值)。"""
    nav = {str(r["trade_date"])[:10]: float(r["total_nav"]) for r in
           q(f"SELECT trade_date, total_nav FROM xcq_paper_nav_daily WHERE account_id='{acct}'") if r.get("total_nav")}
    trades = q(f"SELECT stock_code, side, qty, price, trade_date FROM xcq_paper_trades WHERE account_id='{acct}' ORDER BY id")
    try:
        cas = q(f"SELECT stock_code, trade_date, qty_before, qty_after FROM xcq_paper_corp_actions WHERE account_id='{acct}'")
    except Exception:  # noqa: BLE001
        cas = []
    codes = sorted({t["stock_code"] for t in trades})
    bars = _load_bars(codes, dates[0], dates[-1]) if codes else {}
    close = {c: {b[0]: b[2] for b in v} for c, v in bars.items()}
    opn = {c: {b[0]: b[1] for b in v} for c, v in bars.items()}
    qty: Dict[str, float] = defaultdict(float)
    by_day: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for t in trades:
        by_day[str(t["trade_date"])[:10]].append(t)
    ca_day: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for c in cas:
        ca_day[str(c["trade_date"])[:10]].append(c)
    out: Dict[str, Dict[str, Any]] = {}
    for d in dates:
        bps = []
        for t in by_day.get(d, []):
            qty[t["stock_code"]] += float(t["qty"]) * (1 if t["side"] == "buy" else -1)
            o = opn.get(t["stock_code"], {}).get(d)
            if o:
                x = (float(t["price"]) / o - 1) * 1e4
                bps.append(x if t["side"] == "buy" else -x)
        for c in ca_day.get(d, []):
            qty[c["stock_code"]] += float(c["qty_after"]) - float(c["qty_before"])
        nv = nav.get(d)
        w = {}
        if nv:
            for c, n in qty.items():
                px = close.get(c, {}).get(d)
                if n > 0 and px:
                    w[c] = n * px / nv
        out[d] = {"nav": nv, "w": w, "n_trades": len(by_day.get(d, [])), "exec_bps": float(np.mean(bps)) if bps else None}
    return out


def reconcile(a: Dict[str, Any], end: Optional[str] = None) -> Optional[Dict[str, Any]]:
    from strategy import config as C, engine as E
    from strategy.selector import Selector
    from strategy.prices import trading_calendar as _cal
    acct = a["account_id"]
    first = q(f"SELECT MIN(trade_date) d FROM xcq_paper_trades WHERE account_id='{acct}'")
    if not first or not first[0]["d"]:
        return None
    start = str(first[0]["d"])[:10]
    s = C.load_strategy(a["config_id"])
    if a.get("data_env"):
        s["universe"]["data_env"] = a["data_env"]
    end = end or _cal()[-1]
    ctx = E.get_ctx(s["universe"]["data_env"], end)
    if start > ctx.px.dates[-1]:
        return None
    sel = Selector(ctx, s)
    bt = E.run_strategy(ctx, s, sel.follow, start, end, a["capital"], record_holdings=True)
    bt_rows = {x["date"]: x for x in bt["nav_daily"]}
    bt_trades = defaultdict(int)
    for tr in bt.get("trades") or []:
        bt_trades[tr["date"]] += 1
    dates = [d for d in ctx.px.dates if start <= d <= end]
    paper = _paper_daily(acct, dates)
    rows, cum, prev_p, prev_b = [], 0.0, a["capital"], a["capital"]
    for d in dates:
        p, b = paper.get(d) or {}, bt_rows.get(d)
        if not p.get("nav") or not b:
            continue
        pr, br = (p["nav"] / prev_p - 1) * 100, (b["nav"] / prev_b - 1) * 100
        prev_p, prev_b = p["nav"], b["nav"]
        cum += pr - br
        wp, wb = p["w"], b.get("w") or {}
        overlap = 1 - 0.5 * sum(abs(wp.get(c, 0.0) - wb.get(c, 0.0)) for c in set(wp) | set(wb))
        rows.append({"account_id": acct, "trade_date": d, "paper_nav": p["nav"], "bt_nav": b["nav"], "paper_ret_pct": round(pr, 4),
                     "bt_ret_pct": round(br, 4), "diff_pct": round(pr - br, 4), "cum_diff_pct": round(cum, 4),
                     "overlap": round(overlap, 4), "n_hold_paper": len(wp), "n_hold_bt": len(wb),
                     "n_trades_paper": p["n_trades"], "n_trades_bt": bt_trades.get(d, 0),
                     "exec_bps": None if p["exec_bps"] is None else round(p["exec_bps"], 1), "computed_at": datetime.now()})
    if not rows:
        return None
    from strategy import recs_store as S
    ensure_schema()
    S.run_many("REPLACE INTO xcq_paper_recon_daily (account_id, trade_date, paper_nav, bt_nav, paper_ret_pct, bt_ret_pct, "
               "diff_pct, cum_diff_pct, overlap, n_hold_paper, n_hold_bt, n_trades_paper, n_trades_bt, exec_bps, computed_at) "
               "VALUES (:account_id,:trade_date,:paper_nav,:bt_nav,:paper_ret_pct,:bt_ret_pct,:diff_pct,:cum_diff_pct,:overlap,"
               ":n_hold_paper,:n_hold_bt,:n_trades_paper,:n_trades_bt,:exec_bps,:computed_at)", rows)
    ex = [r["exec_bps"] for r in rows if r["exec_bps"] is not None]
    summ = {"account_id": acct, "config_id": a["config_id"], "start": rows[0]["trade_date"], "end": rows[-1]["trade_date"],
            "days": len(rows), "paper_return_pct": round((rows[-1]["paper_nav"] / a["capital"] - 1) * 100, 2),
            "bt_return_pct": round((rows[-1]["bt_nav"] / a["capital"] - 1) * 100, 2),
            "cum_diff_pct": round(rows[-1]["cum_diff_pct"], 2),
            "overlap_mean": round(float(np.mean([r["overlap"] for r in rows])), 3),
            "exec_bps_mean": round(float(np.mean(ex)), 1) if ex else None,
            "trades_paper": sum(r["n_trades_paper"] for r in rows), "trades_bt": sum(r["n_trades_bt"] for r in rows),
            "computed_at": datetime.now().isoformat(timespec="seconds")}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{acct}.json").write_text(json.dumps({**summ, "daily": [{k: (str(v) if k == "computed_at" else v)
                                                                        for k, v in r.items()} for r in rows]},
                                                 ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return summ


def run_all(only: Optional[str] = None) -> List[Dict[str, Any]]:
    out = []
    for a in accounts():
        if only and a["account_id"] != only:
            continue
        try:
            r = reconcile(a)
            if r:
                out.append(r)
                logger.info(f"[recon] {r['account_id']}: {r['days']} 天, 模拟盘 {r['paper_return_pct']}% vs 回测 {r['bt_return_pct']}%, "
                            f"累计差 {r['cum_diff_pct']}pp, 持仓重合 {r['overlap_mean']}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[recon] {a['account_id']} 对账失败: {exc}")
    return out


def summaries() -> Dict[str, Any]:
    out = {}
    for fp in OUT.glob("*.json") if OUT.exists() else []:
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
            d.pop("daily", None)
            out[d["account_id"]] = d
        except (OSError, ValueError, KeyError):
            continue
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="")
    a = ap.parse_args()
    for r in run_all(a.account or None):
        print(json.dumps(r, ensure_ascii=False))
