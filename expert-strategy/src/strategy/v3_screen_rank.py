"""跟随高手 v3.1 —— "质量筛选(长窗口) + 排名(短窗口)" 回测矩阵 (2026-09-24)。

背景 (docs/跟随高手v3_方案与回测_20260924.md 第七节): 高手过去 60/120 日收益在之后 20 日强烈反转
(前10%−后10% = −6.4% / −13.0%), 1~5 日才有轻微延续。所以把两件事拆开:
  质量筛选 (谁有资格): 长窗口 window 日内 回撤 ≤ max_mdd、活跃、胜率 ≥ min_win (平仓 ≥ min_closed 笔),
                       规则① 收益 > 0 "开/关"两种都测 (S1 / S0)
  排名 (跟谁、跟多少): 合格池按近 N 日收益 (N ∈ rank_windows) 降序, 取前 top_k 名 (T) 或前 X% (P, X ∈ pos_tiers)
  执行: 镜像持仓 (v1/v2 同款目标仓位引擎), 名次线性加权, 次日开盘调仓, 单票 ≤ 20%

预注册 (2026-09-24 用户决策后、运行前写定, 不放宽):
  方案 = {S1,S0} × rank_windows × ({T top_k} ∪ {P x}) —— 默认 2×3×4 = 24 个, 只直接跟单, 不用模型
  对照1 (筛选有没有用): 同 N、同人数、不筛选 (近 recent_days 日有交易的全部组合按近 N 日收益取前 k)
  对照2 (排名有没有用): 从同一合格池随机抽同样人数 (20 个种子); 多重比较: 每个种子取全部方案的最大值, 须超过其 80 分位
  闸门: α t > 0; 总收益 ≥ 自身随机对照 80 分位; 总收益 ≥ 多重比较修正 80 分位; 买入 ≥ 60 笔;
        α − 不筛选对照 α ≥ 1pp (= 质量筛选确实加分)

迁移自 scripts/expert_v3/screen_rank.py (2026-10-16)。变更:
  * scripts.expert_v3.{config,pool} -> strategy.{v3_config,v3_pool}; scripts.core.db.q -> core.db.q;
  * scripts.experts.follow_backtest_v2 (FB.Ctx / FB.run_variant) -> strategy.backtest_v2 (并行任务,
    未就位前 run() 内 ImportError, 模块导入不受影响);
  * 关注列表 xcq_expert_watchlist: 本项目无关注列表数据, strategy.v3_store.ensure_schema 建空表
    保持查询链 (sim 环境 watch=[]; live 环境取 expert_trades 无关, 由调用方配置)。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

import numpy as np

from core.db import q
from strategy import v3_config as C
from strategy import v3_pool as PL

N_NULL = 20
GATE = {"alpha_t": 0.0, "null_q": 0.80, "min_buys": 60, "alpha_gain_vs_unscreened_pp": 1.0}


def variants(spec: Dict[str, Any]) -> List[str]:
    groups = ([f"T{spec['top_k']}"] if spec["top_k"] else []) + [f"P{x}" for x in spec["pos_tiers"]]
    return [f"S{r}_N{n}_{g}" for r in (1, 0) for n in spec["rank_windows"] for g in groups]


def label(v: str) -> str:
    rule, n, g = PL.parse_sr(v)
    who = f"前{g[1]}名" if g[0] == "top" else f"前{C.pct_label(g[1])}"
    return f"{'含' if rule else '不含'}规则①·近{n}日排名·{who}"


def run(spec: Dict[str, Any], end: str, status) -> Dict[str, Any]:
    from strategy import backtest_v2 as FB          # 并行任务; 未就位时此处 ImportError
    t0 = time.time()
    env = spec.get("data_env") or "sim"
    if env == "live":
        watch = [(r["expert_id"], r.get("name") or r["expert_id"])
                 for r in q("SELECT expert_id, name FROM xcq_expert_watchlist "
                            "WHERE source='eastmoney' AND market_type='live'")]
    else:
        watch = [(r["expert_id"], r.get("name") or r["expert_id"])
                 for r in q("SELECT expert_id, name FROM xcq_expert_watchlist "
                            "WHERE market_type IS NULL OR market_type<>'live'")]
    status(step=f"加载高手净值与行情({'实盘' if env == 'live' else '模拟盘'}数据)", progress=0.03)
    ctx = FB.Ctx(end, watch, env=env, ids=spec.get("expert_ids") or None)
    dates = ctx.px.dates
    end = min(end, dates[-1])
    pool = PL.Pool(ctx, spec)
    W = spec["window"]
    if len(dates) <= W + 2:
        raise RuntimeError(f"数据只有 {len(dates)} 个交易日, 不够 {W} 日筛选窗口")
    start = dates[W + 1]
    cap = spec["capital"]
    keys = variants(spec)
    sets: Dict[str, Dict[int, List[str]]] = {k: {} for k in keys}

    def fs_fn(key):
        rule, n, g = PL.parse_sr(key)

        def fn(tp):
            if tp not in sets[key]:
                sets[key][tp] = PL.screen_rank_set(pool, tp, rule, n, g)
            return PL.rank_weights(sets[key][tp])
        return fn

    def unscreened_fn(key):
        rule, n, g = PL.parse_sr(key)
        base = fs_fn(key)

        def fn(tp):
            k = len(base(tp))
            return PL.rank_weights(PL.unscreened_set(pool, tp, n, k))
        return fn

    def random_fn(key, seed):
        rule, n, g = PL.parse_sr(key)
        base = fs_fn(key)

        def fn(tp):
            k = len(base(tp))
            m = pool.metrics(tp)
            if not k or m is None:
                return []
            cand = m["passed"] if rule else m["elig_noret"]
            pick = np.random.default_rng(seed * 100003 + tp).choice(cand, min(k, len(cand)), replace=False)
            return [(ctx.experts[i], 1.0 / len(pick), 0.0) for i in pick]
        return fn

    def bt(fn):
        return FB.run_variant(ctx, {"exec": "t1_open", "follow_fn": fn}, start, end, cap)

    def keep(m):
        ks = ("total_return_pct", "annualized_return_pct", "max_drawdown_pct", "sharpe", "alpha_pct", "alpha_t", "beta",
              "n_buy", "n_sell", "win_rate_pct", "fee_drag_pct", "avg_positions", "benchmark_return_pct",
              "excess_return_pct", "turnover_x")
        out = {k: (float(m[k]) if isinstance(m.get(k), (int, float, np.floating)) else m.get(k)) for k in ks}
        out["n_accepted_buys"] = out.get("n_buy")
        return out

    res: Dict[str, Any] = {}
    for i, k in enumerate(keys):
        status(step=f"回测 {label(k)}", progress=0.05 + 0.30 * i / len(keys))
        r = bt(fs_fn(k))
        u = bt(unscreened_fn(k))
        days = [len(v) for v in sets[k].values()]
        res[k] = {"label": label(k), "engine": "mirror", "metrics": keep(r["metrics"]),
                  "nav": [(x["date"], round(x["nav"] / cap, 4)) for x in r["nav_daily"]],
                  "unscreened": keep(u["metrics"]),
                  "unscreened_nav": [(x["date"], round(x["nav"] / cap, 4)) for x in u["nav_daily"]],
                  "avg_experts": round(float(np.mean(days)), 1) if days else 0.0,
                  "days_no_list": int(sum(1 for d in days if d == 0))}
    v2 = FB.run_variant(ctx, {"exec": "t1_open", "top_n": 10, "metric": "day", "weighting": "rank"}, start, end, cap)
    baseline = keep(v2["metrics"])
    baseline["label"] = "v2 线上版(关注+当日收益前10·名次加权·镜像·次日开盘)"
    w = [e for e, _ in watch if e in ctx.eidx]
    wr = FB.run_variant(ctx, {"exec": "t1_open", "follow_fn": lambda tp: [(e, 1 / len(w), 0.0) for e in w] if w else []},
                        start, end, cap)
    watch_res = keep(wr["metrics"])
    watch_res["label"] = f"仅关注高手({len(w)}人)·镜像 (有事后选择偏差, 仅参考)"

    nulls: Dict[str, List[float]] = {k: [] for k in keys}
    for s in range(N_NULL):
        for k in keys:
            nulls[k].append(bt(random_fn(k, 1000 * s + keys.index(k)))["metrics"]["total_return_pct"])
        status(step=f"随机对照 {s + 1}/{N_NULL}", progress=0.38 + 0.55 * (s + 1) / N_NULL)
    mc_p80 = float(np.percentile([max(nulls[k][s] for k in keys) for s in range(N_NULL)], GATE["null_q"] * 100))
    for k, v in res.items():
        m = v["metrics"]
        own = float(np.percentile(nulls[k], GATE["null_q"] * 100))
        v["null"] = {"p50": round(float(np.median(nulls[k])), 2), "p80": round(own, 2), "mc_p80": round(mc_p80, 2)}
        tot = m.get("total_return_pct") or -999
        checks = {"alpha_t": (m.get("alpha_t") or -9) > GATE["alpha_t"],
                  "beat_null_p80": tot >= own,
                  "beat_null_multi": tot >= mc_p80,
                  "min_buys": (m.get("n_accepted_buys") or 0) >= GATE["min_buys"],
                  "alpha_vs_unscreened": (m.get("alpha_pct") or -99) - (v["unscreened"].get("alpha_pct") or 0)
                                          >= GATE["alpha_gain_vs_unscreened_pp"]}
        v["gate"] = {"passed": all(checks.values()), "checks": checks}
    tp_last = len([d for d in dates if d <= end]) - 1
    months = sorted({d[:7] for d in dates if start <= d <= end})
    out = {"mode": "screen_rank", "data_env": env, "data_end": end, "test_start": start, "test_months": months,
           "funnel_last": (pool.metrics(tp_last) or {}).get("funnel"), "n_events": 0,
           "variants": res, "baseline_v2": baseline, "baseline_v2_mirror": baseline, "watchlist": watch_res,
           "models": {}, "null": {"n_seeds": N_NULL, "multi_p80": round(mc_p80, 2)}, "gate_rules": GATE,
           "passed": [k for k, v in res.items() if v["gate"]["passed"]], "elapsed_s": round(time.time() - t0)}
    if env == "live":
        from strategy import v3_config as C
        w = C.live_sample_warning()
        if w:
            out["data_warning"] = w
    return out
