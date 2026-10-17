"""迁移自 scripts/experts/follow_v21.py (2026-10-16)。变更 (仅导入路径/存储后端/文件路径, 业务逻辑逐行保持):
  * 导入: scripts.* -> core.* / strategy.* (backtest_v2 / follow_v2 / recs.ledger.Rec -> strategy.ledger);
  * _cal (scripts/stock_graph/data.py 的 qlib 日历) -> strategy.prices.trading_calendar (本地价格库日历, 同为升序 list[str]);
  * 路径: OUT_DIR -> paths.FOLLOW_BT_DIR; 两处日志文案里的 "qlib 行情" 随价格后端改为 "行情"。

跟随高手 v2.1 —— 在 v2 (全体活跃高手按近 1 日收益取前 10, 名次加权镜像持仓, 次日开盘) 上加短周期风控。

背景: v3 / v3.1 证实长窗口筛选有害 (高手 60/120 日业绩之后反转), 唯一稳健的信号是 v2 的近 1 日排名。
v2.1 不加长窗口筛选, 只在排名名单上做三类短周期风控; 被剔除的高手由下一名递补 (始终跟 10 人):
  R1 当日涨幅异常: 信号日收益 > 阈值 (多半是涨停/异常脉冲, 次日追不到同价, 或净值重建毛刺)
  R2 单票过度集中: 最大单票仓位 > 阈值 (一只票决定全部收益, 跟单风险集中)
  R3 刚开户的组合: 本地数据里第一笔交易距信号日 < N 个交易日 (新组合的短期收益最不可信)
     (限制: 高手历史从 2025-12-01 起抓取, 更早开户的组合"第一笔"最早也只能落在 12 月; 回测从 2026-02 开始, 仍可区分新开户)

预注册 (2026-09-25, 运行前写定, 不放宽):
  方案: R1>10% / R1>15% / R2>80% / R2>95% / R3<20日 / R3<60日 / ALL(R1>10% + R2>80% + R3<20日)
  基准: v2 不含关注名单 (关注名单是事后挑的, 会干扰比较); 另报 v2 线上版 (含关注) 仅供参考
  期间: 全期 2026-02-02 ~ 数据末, 前半 2026-02-02 ~ 05-29, 后半 2026-06-01 ~ 数据末
  闸门 (全部满足):
    1. 全期 α − 基准 α ≥ 1pp
    2. 前半、后半各自 α − 基准 α > 0 (两段都要改善, 防运气)
    3. 全期最大回撤 ≤ 基准最大回撤
    4. 全期 α 增益 ≥ "随机剔除"对照增益的 80 分位 (每天随机剔除同样多的人再递补, 20 个种子)
    5. 全期 α t > 0

    python -m strategy.follow_v21 [--end YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from core.logger import logger
from paths import FOLLOW_BT_DIR

TOP_N = 10
N_NULL = 20
CONTROLS: Dict[str, Dict[str, Any]] = {
    "R1_10": {"max_day_ret": 0.10, "label": "剔除当日涨幅 >10%"},
    "R1_15": {"max_day_ret": 0.15, "label": "剔除当日涨幅 >15%"},
    "R2_80": {"max_top_w": 0.80, "label": "剔除单票仓位 >80%"},
    "R2_95": {"max_top_w": 0.95, "label": "剔除单票仓位 >95%"},
    "R3_20": {"min_age": 20, "label": "剔除开户(首笔交易) <20 个交易日"},
    "R3_60": {"min_age": 60, "label": "剔除开户(首笔交易) <60 个交易日"},
    "ALL": {"max_day_ret": 0.10, "max_top_w": 0.80, "min_age": 20, "label": "组合: 涨幅>10% + 单票>80% + 开户<20日"},
}
GATE = {"alpha_gain_pp": 1.0, "halves_gain_pp": 0.0, "null_q": 0.80, "alpha_t": 0.0}
H1_END, H2_START = "2026-05-29", "2026-06-01"


def first_trade_idx(ctx) -> np.ndarray:
    """每位高手在本地数据里第一笔交易的日下标 (没有 = 很大)。"""
    cached = getattr(ctx, "_first_t", None)
    if cached is not None:
        return cached
    out = np.full(len(ctx.experts), 10 ** 6)
    for i, e in enumerate(ctx.experts):
        ts = [ctx.px.day_of(r["trade_date"]) for r in ctx.hist[e]["records"]]
        ts = [t for t in ts if t is not None]
        if ts:
            out[i] = min(ts)
    ctx._first_t = out
    return out


def excluded(ctx, i: int, t_sig: int, m: np.ndarray, c: Dict[str, Any]) -> bool:
    if c.get("max_day_ret") is not None and m[i] > c["max_day_ret"]:
        return True
    if c.get("min_age") is not None and t_sig - first_trade_idx(ctx)[i] < c["min_age"]:
        return True
    if c.get("max_top_w") is not None:
        h = ctx.holdings(ctx.experts[i])[t_sig]
        if h and max(h.values()) > c["max_top_w"]:
            return True
    return False


def ranked(ctx, t_sig: int) -> Tuple[np.ndarray, np.ndarray]:
    """v2 排名: 近 1 日收益 (ctx.metric 已剔除僵尸组合与重建异常), 降序下标。"""
    m = ctx.metric("day", t_sig)
    valid = np.where(np.isfinite(m))[0]
    return m, valid[np.argsort(-m[valid], kind="stable")]


def rank_w(es: List[str]) -> List[Tuple[str, float, float]]:
    n = len(es)
    raw = [n - k for k in range(n)]
    return [(e, raw[k] / sum(raw), 0.0) for k, e in enumerate(es)] if n else []


def follow_set_v21(ctx, t_sig: int, c: Optional[Dict[str, Any]], top_n: int = TOP_N, log: Optional[dict] = None):
    """风控后的跟随集合 [(expert, 资金权重, 0)]; log 记录当天被剔除人数 (供随机对照)。"""
    m, order = ranked(ctx, t_sig)
    out, n_ex = [], 0
    for i in order:
        if len(out) >= top_n:
            break
        if c and excluded(ctx, i, t_sig, m, c):
            n_ex += 1
            continue
        out.append(ctx.experts[i])
    if log is not None:
        log[t_sig] = n_ex
    return rank_w(out)


def random_exclusion(ctx, t_sig: int, k: int, seed: int, top_n: int = TOP_N):
    """对照: 从排名前 top_n + k 里随机剔除 k 人 (不看任何特征), 其余按名次取前 top_n。"""
    _, order = ranked(ctx, t_sig)
    head = list(order[:top_n + k])
    if k:
        drop = set(np.random.default_rng(seed * 100003 + t_sig).choice(len(head), min(k, len(head)), replace=False))
        head = [x for j, x in enumerate(head) if j not in drop]
    return rank_w([ctx.experts[i] for i in head[:top_n]])


# ── 名次段检验 (2026-09-25 预注册, 见文档第五节) ──
RANK_BANDS: Dict[str, Tuple[int, int]] = {"RK11_20": (11, 20), "RK11_30": (11, 30)}
BAND_POOL = 30          # 随机对照: 从名次 1~30 随机抽同样人数


def follow_set_band(ctx, t_sig: int, lo: int, hi: int):
    """跟排名第 lo~hi 名 (1 起算), 名次加权。"""
    _, order = ranked(ctx, t_sig)
    return rank_w([ctx.experts[i] for i in order[lo - 1:hi]])


def random_band(ctx, t_sig: int, n: int, seed: int):
    _, order = ranked(ctx, t_sig)
    head = list(order[:BAND_POOL])
    if len(head) <= n:
        return rank_w([ctx.experts[i] for i in head])
    pick = sorted(np.random.default_rng(seed * 100003 + t_sig).choice(len(head), n, replace=False))
    return rank_w([ctx.experts[head[j]] for j in pick])


def main_bands(end: Optional[str] = None, env: str = "sim") -> Dict[str, Any]:
    from strategy import backtest_v2 as FB
    from strategy.prices import trading_calendar as _cal
    end = end or _cal()[-1]
    ctx = FB.Ctx(end, [], env=env)
    end = min(end, ctx.px.dates[-1])
    periods = {"full": ("2026-02-02", end), "h1": ("2026-02-02", H1_END), "h2": (H2_START, end)}

    def bt(fn, per):
        s, e = periods[per]
        return FB.run_variant(ctx, {"exec": "t1_open", "follow_fn": fn}, s, e)["metrics"]

    keep = lambda m: {k: m.get(k) for k in ("total_return_pct", "max_drawdown_pct", "sharpe", "alpha_pct", "alpha_t",  # noqa: E731
                                            "n_buy", "win_rate_pct", "fee_drag_pct")}
    base = {p: keep(bt(lambda t: follow_set_v21(ctx, t, None), p)) for p in periods}
    res: Dict[str, Any] = {"base": {"label": "v2 基准 (名次1~10, 不含关注)", **base}, "variants": {}}
    for key, (lo, hi) in RANK_BANDS.items():
        v = {p: keep(bt(lambda t, lo=lo, hi=hi: follow_set_band(ctx, t, lo, hi), p)) for p in periods}
        n = hi - lo + 1
        gains = [(bt(lambda t, s=s, n=n: random_band(ctx, t, n, 1000 * s + lo), "full").get("alpha_pct") or 0)
                 - (base["full"].get("alpha_pct") or 0) for s in range(N_NULL)]
        g = {p: round((v[p].get("alpha_pct") or 0) - (base[p].get("alpha_pct") or 0), 2) for p in periods}
        checks = {"alpha_gain": g["full"] >= GATE["alpha_gain_pp"],
                  "both_halves": g["h1"] > 0 and g["h2"] > 0,
                  "mdd_not_worse": (v["full"].get("max_drawdown_pct") or 99) <= (base["full"].get("max_drawdown_pct") or 0) + 1e-9,
                  "beat_random_top30": g["full"] >= float(np.percentile(gains, GATE["null_q"] * 100)),
                  "alpha_t": (v["full"].get("alpha_t") or -9) > 0}
        res["variants"][key] = {"label": f"名次 {lo}~{hi}", **v, "alpha_gain_pp": g,
                                "null_gain": {"p50": round(float(np.median(gains)), 2),
                                              "p80": round(float(np.percentile(gains, GATE["null_q"] * 100)), 2)},
                                "gate": {"passed": all(checks.values()), "checks": checks}}
    # R3_60 剔除的人落在哪些名次 (03-05 之后, 规则定义良好的区间)
    c = CONTROLS["R3_60"]
    top10 = mid = n_days = 0
    for t in range(len(ctx.px.dates) - 1):
        if ctx.px.dates[t + 1] < "2026-03-05":
            continue
        m, order = ranked(ctx, t)
        n_days += 1
        for k, i in enumerate(order[:30]):
            if excluded(ctx, i, t, m, c):
                top10 += k < 10
                mid += k >= 10
    res["r3_60_excluded_by_rank"] = {"days": n_days, "avg_in_rank_1_10": round(top10 / max(n_days, 1), 2),
                                     "avg_in_rank_11_30": round(mid / max(n_days, 1), 2)}
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = {"run_id": run_id, "env": env, "periods": periods, **res,
           "passed": [k for k, v in res["variants"].items() if v["gate"]["passed"]]}
    FOLLOW_BT_DIR.mkdir(parents=True, exist_ok=True)
    (FOLLOW_BT_DIR / f"follow_v21_bands_{run_id}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float))
    return out


# ── 模拟盘接入 (2026-09-25 用户指示: 回测闸门未过, 开模拟账户累积前向证据) ──
LIVE_NAMES = {"R3_60": "跟随高手v2.1·剔除开户<60日 (前向观察)", None: "跟随高手v2·不含关注 (v2.1 对照, 前向观察)"}


def strategy_id(control: Optional[str]) -> str:
    return f"expert_follow_v21:{control or 'base'}"


def generate_recs(spec: Dict[str, Any], data_date: str, pred_date: str):
    """paper_signals 的 expert_follow_v21 分支: data_date 收盘后的风控名单 → 目标仓位推荐 (与回测同一套函数)。"""
    from strategy import backtest_v2 as FB
    from strategy import follow_v2
    from strategy.ledger import Rec
    ctx = follow_v2._ctx(data_date)
    if data_date not in ctx.px.didx:
        logger.warning(f"[v2.1] 行情还没有 {data_date}, 不生成")
        return None
    t = ctx.px.didx[data_date]
    c = CONTROLS.get(spec.get("control")) if spec.get("control") else None
    fs = follow_set_v21(ctx, t, c)
    tw = FB.target_weights(ctx, fs, t)
    names: Dict[str, str] = {}
    for e, _, _ in fs:
        for r in ctx.hist[e]["records"]:
            names.setdefault(r["stock_code"], r.get("stock_name") or "")
    recs = []
    for k, (j, w) in enumerate(sorted(tw.items(), key=lambda x: -x[1]), 1):
        code = ctx.px.codes[j]
        recs.append(Rec(code=code, name=names.get(code, ""), action="买入", reason_code="FOLLOW_V21",
                        reason_text=f"跟随高手v2.1·{spec.get('control') or '基准'} · 目标仓位{w:.1%} · 跟随 {len(fs)} 位高手",
                        score=float(w), rank_no=k, target_weight=round(float(w), 4), pool_ref=f"follow_v21:{pred_date}",
                        meta={"experts": [e for e, _, _ in fs]}))
    logger.info(f"[v2.1] {spec.get('control') or 'base'} {data_date}→{pred_date}: 跟随 {len(fs)} 位, 目标 {len(recs)} 只")
    return recs


def main(end: Optional[str] = None, env: str = "sim") -> Dict[str, Any]:
    from core.db import q
    from strategy import backtest_v2 as FB
    from strategy.prices import trading_calendar as _cal
    end = end or _cal()[-1]
    if env == "live":
        # [2026-09-25] 实盘回测: 对照名单=全部有流水的实盘组合
        watch = [(r["expert_id"], r.get("expert_name") or r["expert_id"])
                 for r in q("SELECT DISTINCT expert_id, MAX(expert_name) AS expert_name FROM expert_trades "
                            "WHERE market_type='live' AND stock_code<>'' GROUP BY expert_id ORDER BY expert_id")]
    else:
        # [2026-09-25 决策] 实盘高手(market_type=live)只入库+推送观察, 不进模拟盘信号
        watch = [(r["expert_id"], r.get("name") or r["expert_id"])
                 for r in q("SELECT expert_id, name FROM xcq_expert_watchlist "
                            "WHERE COALESCE(market_type,'sim') <> 'live'")]
    ctx = FB.Ctx(end, [], env=env)           # 不含关注名单: 公平比较
    end = min(end, ctx.px.dates[-1])
    periods = {"full": ("2026-02-02", end), "h1": ("2026-02-02", H1_END), "h2": (H2_START, end)}

    def bt(fn, per, key=None):
        s, e = periods[per]
        r = FB.run_variant(ctx, {"exec": "t1_open", "follow_fn": fn}, s, e)
        if key and per == "full":            # [2026-09-25] 供 UI 逐月收益: 全期曲线
            curves[key] = r.get("nav_daily") or []
        return r["metrics"]

    curves: Dict[str, list] = {}
    keep = lambda m: {k: m.get(k) for k in ("total_return_pct", "annualized_return_pct", "max_drawdown_pct", "sharpe",  # noqa: E731
                                            "alpha_pct", "alpha_t", "n_buy", "win_rate_pct", "fee_drag_pct",
                                            "benchmark_return_pct", "turnover_x")}

    base = {p: keep(bt(lambda t: follow_set_v21(ctx, t, None), p, key="base")) for p in periods}
    res: Dict[str, Any] = {"base": {"label": "v2 基准 (不含关注)", **base}, "variants": {}}
    excl_log: Dict[str, dict] = {}
    for key, c in CONTROLS.items():
        lg: dict = {}
        v = {p: keep(bt(lambda t, c=c, lg=lg: follow_set_v21(ctx, t, c, log=lg), p, key=key if p == "full" else None))
             for p in periods}
        excl_log[key] = lg
        ks = [x for x in lg.values()]
        v["avg_excluded"] = round(float(np.mean(ks)), 2) if ks else 0.0
        res["variants"][key] = {"label": c["label"], **v}
        logger.info(f"[v2.1] {key}: 全期 {v['full']['total_return_pct']}% (基准 {base['full']['total_return_pct']}%)")
    # 随机剔除对照 (全期)
    for key in CONTROLS:
        lg = excl_log[key]
        gains = []
        for s in range(N_NULL):
            m = bt(lambda t, s=s, lg=lg: random_exclusion(ctx, t, lg.get(t, 0), 1000 * s + list(CONTROLS).index(key)), "full")
            gains.append((m.get("alpha_pct") or 0) - (base["full"].get("alpha_pct") or 0))
        res["variants"][key]["null_gain"] = {"p50": round(float(np.median(gains)), 2),
                                             "p80": round(float(np.percentile(gains, GATE["null_q"] * 100)), 2)}
    for key, v in res["variants"].items():
        g = {p: round((v[p].get("alpha_pct") or 0) - (base[p].get("alpha_pct") or 0), 2) for p in periods}
        v["alpha_gain_pp"] = g
        checks = {"alpha_gain": g["full"] >= GATE["alpha_gain_pp"],
                  "both_halves": g["h1"] > GATE["halves_gain_pp"] and g["h2"] > GATE["halves_gain_pp"],
                  "mdd_not_worse": (v["full"].get("max_drawdown_pct") or 99) <= (base["full"].get("max_drawdown_pct") or 0) + 1e-9,
                  "beat_random_exclusion": g["full"] >= v["null_gain"]["p80"],
                  "alpha_t": (v["full"].get("alpha_t") or -9) > GATE["alpha_t"]}
        v["gate"] = {"passed": all(checks.values()), "checks": checks}
    # 参考: v2 线上版 (含关注, 有事后选择偏差)
    ctx_w = FB.Ctx(end, watch, env=env)
    res["v2_online_ref"] = keep(FB.run_variant(ctx_w, {"exec": "t1_open", "top_n": 10, "metric": "day", "weighting": "rank"},
                                               *periods["full"])["metrics"])
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = {"run_id": run_id, "env": env, "created_at": datetime.now().isoformat(timespec="seconds"), "periods": periods,
           "gate_rules": GATE, "controls": CONTROLS, "curves": curves, **res,
           "passed": [k for k, v in res["variants"].items() if v["gate"]["passed"]]}
    FOLLOW_BT_DIR.mkdir(parents=True, exist_ok=True)
    fp = FOLLOW_BT_DIR / f"follow_v21_{run_id}.json"
    fp.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    (FOLLOW_BT_DIR / "follow_v21_LATEST").write_text(fp.name)
    logger.info(f"[v2.1] 完成 {run_id}: 通过 {out['passed'] or '无'} → {fp}")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--end", default="")
    ap.add_argument("--env", default="sim", choices=["sim", "live"], help="回测数据环境")
    ap.add_argument("--bands", action="store_true", help="名次段检验 (RK11_20 / RK11_30)")
    a = ap.parse_args()
    if a.bands:
        r = main_bands(a.end or None, env=a.env)
        b = r["base"]
        print(f"基准 名次1~10: 全期 {b['full']['total_return_pct']}% α {b['full']['alpha_pct']} 回撤 {b['full']['max_drawdown_pct']} "
              f"| 前半 {b['h1']['total_return_pct']}% | 后半 {b['h2']['total_return_pct']}%")
        for k, v in r["variants"].items():
            bad = [c for c, ok in v["gate"]["checks"].items() if not ok]
            print(f"{k}: 全期 {v['full']['total_return_pct']}% 回撤 {v['full']['max_drawdown_pct']} αt {v['full']['alpha_t']} "
                  f"| α增益 全{v['alpha_gain_pp']['full']} 前{v['alpha_gain_pp']['h1']} 后{v['alpha_gain_pp']['h2']} "
                  f"| 随机(名次1~30) p50 {v['null_gain']['p50']} p80 {v['null_gain']['p80']} | {'PASS' if v['gate']['passed'] else bad}")
        print("R3_60 每天剔除:", r["r3_60_excluded_by_rank"])
        raise SystemExit(0)
    r = main(a.end or None, env=a.env)
    b = r["base"]
    print(f"基准 v2(不含关注): 全期 {b['full']['total_return_pct']}% α {b['full']['alpha_pct']} 回撤 {b['full']['max_drawdown_pct']} | "
          f"前半 {b['h1']['total_return_pct']}% | 后半 {b['h2']['total_return_pct']}%  (v2 线上版参考 {r['v2_online_ref']['total_return_pct']}%)")
    for k, v in r["variants"].items():
        bad = [c for c, ok in v["gate"]["checks"].items() if not ok]
        print(f"{k:6} {v['label']:28} 全期 {v['full']['total_return_pct']:7}% 回撤 {v['full']['max_drawdown_pct']:6} αt {v['full']['alpha_t']} "
              f"| α增益 全{v['alpha_gain_pp']['full']} 前{v['alpha_gain_pp']['h1']} 后{v['alpha_gain_pp']['h2']} | 随机剔除p80 {v['null_gain']['p80']} "
              f"| 日均剔除 {v['avg_excluded']} | {'PASS' if v['gate']['passed'] else bad}")
