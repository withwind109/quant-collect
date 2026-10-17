"""跟随高手 · 统一回测 (2026-09-25): 回测配置 (configs/expert_follow/backtests/<id>.json5) → 全部方案 + 对照 + 闸门。

每个方案 (策略文件 × 参数扫描) 在同一回测期、同一数据环境、同一执行引擎下运行, 并与同一基准比较。
闸门 (用户决策: 取以往 v2 / v2.1 / v3.1 三套闸门的并集, 运行前写在回测配置里, 不放宽):
  α t > 0 · 总收益 ≥ 自身随机对照 80 分位 · ≥ 多重比较修正 80 分位 · α 比基准高 ≥ 1pp · 前后两半 α 增益都 ≥ 0
  · 最大回撤不比基准差 · 买入 ≥ 60 笔 · (有筛选) α 比不筛选对照高 ≥ 1pp · (有风控剔除) α 增益 ≥ 随机剔除 80 分位
页面显示 (用户决策): 基准 + 通过闸门 + 未通过里总收益最好的 top_n 个; 其余归档 (summary 里都保留, 可展开)。
实盘数据: 数据体检不通过 → 运行记为 blocked, 不产出结果 (页面显示"数据不可用")。

迁移自 scripts/expert_follow/runner.py (2026-10-16): 导入改绝对路径 (strategy.* / core.* /
paths), 原函数内惰性导入 (alpha / data_health / trading_calendar / setup_logging) 按约定提升到
模块顶层; 输出目录 OUT_ROOT 由 PROJECT_ROOT/models/expert_follow 改为 paths.MODELS_DIR/
expert_follow; argparse 入口与其余逻辑逐行原样。

用法: python -m strategy.runner --backtest unified_sim   (cwd=项目根, PYTHONPATH=src)
"""
from __future__ import annotations

import json
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from core.logger import logger, setup_logging
from paths import MODELS_DIR
from strategy import alpha as A
from strategy import config as C
from strategy import data_health as DH
from strategy import engine as E
from strategy import store
from strategy.prices import trading_calendar as _cal
from strategy.selector import Selector

OUT_ROOT = MODELS_DIR / "expert_follow"
KEEP = ("total_return_pct", "annualized_return_pct", "max_drawdown_pct", "sharpe", "alpha_pct", "alpha_t", "beta",
        "n_buy", "n_sell", "win_rate_pct", "fee_drag_pct", "avg_positions", "benchmark_return_pct",
        "excess_return_pct", "turnover_x")


def _status(fp: Optional[str], **kw) -> None:
    if not fp:
        return
    p = Path(fp)
    try:
        cur = json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        cur = {}
    cur.update(kw, updated_at=datetime.now().isoformat(timespec="seconds"))
    p.write_text(json.dumps(cur, ensure_ascii=False))


def keep(m: Dict[str, Any]) -> Dict[str, Any]:
    return {k: (float(m[k]) if isinstance(m.get(k), (int, float, np.floating)) else m.get(k)) for k in KEEP}


def period_stats(nav: List[Tuple[str, float]], s: str, e: str) -> Dict[str, Any]:
    """净值序列在 [s, e] 上的收益 / α / α t / 回撤 (前后两半检验用, 不重跑回测)。"""
    pts = [(d, v) for d, v in nav if s <= d <= e and v]
    if len(pts) < 5:
        return {"total_return_pct": None, "alpha_pct": None, "alpha_t": None, "max_drawdown_pct": None, "days": len(pts)}
    ds = [d for d, _ in pts]
    vs = [v for _, v in pts]
    rets = [vs[i] / vs[i - 1] - 1 for i in range(1, len(vs))]
    peak, mdd = vs[0], 0.0
    for v in vs:
        peak = max(peak, v)
        mdd = max(mdd, 1 - v / peak)
    out = {"total_return_pct": round((vs[-1] / vs[0] - 1) * 100, 2), "max_drawdown_pct": round(mdd * 100, 2),
           "alpha_pct": None, "alpha_t": None, "days": len(pts)}
    try:
        al = A.regress(ds[1:], rets, A.bench_returns(ds[0], ds[-1]))
        if al:
            out.update(alpha_pct=round(al["alpha"], 2), alpha_t=round(al["alpha_t"], 2))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[expert-follow] 分段 α 计算失败: {exc}")
    return out


def _nav(r: Dict[str, Any], cap: float) -> List[Tuple[str, float]]:
    return [(x["date"], round(x["nav"] / cap, 4)) for x in r.get("nav_daily") or []]


def run(backtest_id: str, end: Optional[str] = None, status_file: Optional[str] = None,
        run_id: Optional[str] = None) -> Dict[str, Any]:
    t0 = time.time()
    b = C.load_backtest(backtest_id)
    run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    env, cap, per = b["data_env"], b["capital"], b["period"]
    out_dir = OUT_ROOT / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    entries = [e for e in (b.get("strategies") or []) if e.get("enabled", True)]
    families = sorted({C.load_strategy(e["ref"])["family"] for e in entries})
    store.save_run(run_id, backtest_id=backtest_id, data_env=env, families=",".join(families), status="running",
                   period_start=per["start"], archived=0)
    _status(status_file, run_id=run_id, state="running", step="数据体检", progress=0.01)
    # ── 快照: 回测配置 + 当时的策略文件 (可复现) ──
    snap = out_dir / "configs"
    snap.mkdir(exist_ok=True)
    shutil.copy(C.backtest_path(backtest_id), snap / f"backtest_{backtest_id}.json5")
    for ref in {e["ref"] for e in entries} | {b["baseline"]}:
        fp = C.strategy_path(ref)
        if fp:
            shutil.copy(fp, snap / fp.name)
    health = None
    if env == "live":
        health = DH.env_ok("live", b.get("data_health"), refresh=True)
        if not health["ok"]:
            summary = {"run_id": run_id, "backtest_id": backtest_id, "data_env": env, "status": "blocked",
                       "blocked_reason": "实盘数据体检未通过: " + health["reason"], "health": health}
            (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
            store.save_run(run_id, backtest_id=backtest_id, data_env=env, status="blocked", health_ok=0,
                           error=summary["blocked_reason"], finished_at=datetime.now())
            _status(status_file, state="done", step="实盘数据体检未通过, 未运行", progress=1.0)
            logger.warning(f"[expert-follow] {run_id} 实盘体检未通过: {health['reason']}")
            return summary
    end = end or _cal()[-1]
    _status(status_file, step=f"加载{'实盘' if env == 'live' else '模拟盘'}高手净值与行情", progress=0.03)
    ctx = E.get_ctx(env, end)
    dates = ctx.px.dates
    end = min(end, dates[-1])
    start = max(per["start"], dates[1])
    split = per["split"]
    g = b["gate"]

    def bt(s, fn):
        return E.run_strategy(ctx, s, fn, start, end, cap)

    # ── 基准 ──
    (bkey, bs), = C.expand({"ref": b["baseline"]}, env)
    bsel = Selector(ctx, bs)
    br = bt(bs, bsel.follow)
    bnav = _nav(br, cap)
    base = {"key": bkey, "label": C.describe(bs), "name": bs["name"], "family": bs["family"], "metrics": keep(br["metrics"]),
            "nav": bnav, "h1": period_stats(bnav, start, _prev(dates, split)), "h2": period_stats(bnav, split, end),
            "strategy_hash": C.strategy_hash(bs)}
    # ── 方案 ──
    variants: List[Tuple[str, Dict[str, Any], str]] = []
    for e in entries:
        for key, s in C.expand(e, env):
            variants.append((key, s, e["ref"]))
    simple = [(k, s, ref) for k, s, ref in variants if s["execution"]["engine"] != "v3_tiers"]
    tiers = [(k, s, ref) for k, s, ref in variants if s["execution"]["engine"] == "v3_tiers"]
    sels: Dict[str, Selector] = {}
    res: Dict[str, Dict[str, Any]] = {}
    n_seeds, q = int(b["null"]["seeds"]), float(b["null"]["q"])
    for i, (k, s, ref) in enumerate(simple):
        _status(status_file, step=f"回测 {k}", progress=0.05 + 0.25 * i / max(len(simple), 1))
        sel = Selector(ctx, s)
        sels[k] = sel
        r = bt(s, sel.follow)
        nav = _nav(r, cap)
        days = list(sel.k_log.values())
        v = {"key": k, "ref": ref, "family": s["family"], "name": s["name"], "label": C.describe(s),
             "engine": s["execution"]["engine"], "strategy_hash": C.strategy_hash(s), "metrics": keep(r["metrics"]),
             "nav": nav, "h1": period_stats(nav, start, _prev(dates, split)), "h2": period_stats(nav, split, end),
             "avg_experts": round(float(np.mean(days)), 1) if days else 0.0,
             "days_no_list": int(sum(1 for d in days if d == 0))}
        if s.get("screen") or s.get("curve_screen"):
            u = bt(s, sel.unscreened_fn())
            v["unscreened"] = keep(u["metrics"])
        if s.get("risk"):
            gains = []
            for sd in range(n_seeds):
                x = bt(s, sel.random_exclusion_fn(sd))
                gains.append((x["metrics"].get("alpha_pct") or 0) - (base["metrics"].get("alpha_pct") or 0))
            v["random_exclusion"] = {"p50": round(float(np.median(gains)), 2), "p80": round(float(np.percentile(gains, q * 100)), 2)}
            v["avg_excluded"] = round(float(np.mean(list(sel.excluded_log.values()))), 2) if sel.excluded_log else 0.0
        res[k] = v
    # ── 随机对照 (排名/选人有没有用) + 多重比较修正 ──
    nulls: Dict[str, List[float]] = {k: [] for k in res}
    for sd in range(n_seeds):
        for j, (k, s, _) in enumerate(simple):
            nulls[k].append(bt(s, sels[k].null_fn(1000 * sd + j))["metrics"]["total_return_pct"])
        _status(status_file, step=f"随机对照 {sd + 1}/{n_seeds}", progress=0.32 + 0.43 * (sd + 1) / n_seeds)
    mc = [max(nulls[k][sd] for k in nulls) for sd in range(n_seeds)] if nulls else []
    multi_p80 = round(float(np.percentile(mc, q * 100)), 2) if mc else None
    for k, v in res.items():
        v["null"] = {"p50": round(float(np.median(nulls[k])), 2), "p80": round(float(np.percentile(nulls[k], q * 100)), 2),
                     "multi_p80": multi_p80}
    # ── v3 分批 + 模型 (交给 expert_v3 训练+走步回测) ──
    for k, s, ref in tiers:
        _status(status_file, step=f"v3 分批+模型: 训练与走步回测 ({k})", progress=0.78)
        try:
            R = E.run_v3_tiers(s, end)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[expert-follow] v3_tiers 失败: {exc}")
            res[k] = {"key": k, "ref": ref, "family": s["family"], "name": s["name"], "label": C.describe(s),
                      "error": str(exc)[:300]}
            continue
        for vk, vv in (R.get("variants") or {}).items():
            if vk.startswith(("W", "R_")):          # 关注名单参考 / 反向否决叠加 (另有对照口径), 不进统一对比
                continue
            nav = [(d, float(x)) for d, x in vv.get("nav") or []]
            key = f"{k}[{vk}]"
            res[key] = {"key": key, "ref": ref, "family": s["family"], "name": s["name"],
                        "label": f"{C.describe(s)} · {vv.get('label') or vk}", "engine": "v3_tiers",
                        "strategy_hash": C.strategy_hash(s), "metrics": _v3_metrics(vv.get("metrics") or {}), "nav": nav,
                        "h1": period_stats(nav, start, _prev(dates, split)), "h2": period_stats(nav, split, end),
                        "null": {"p50": (vv.get("null") or {}).get("p50"), "p80": (vv.get("null") or {}).get("p80"),
                                 "multi_p80": (R.get("null") or {}).get("multi_p80")},
                        "v3_run_id": R.get("run_id"), "test_start": R.get("test_start"),
                        "period_note": f"v3 训练需要历史, 测试期 {R.get('test_start')} ~ {R.get('data_end')}"}
    # ── 闸门 ──
    bm, b1, b2 = base["metrics"], base["h1"], base["h2"]
    for k, v in res.items():
        if v.get("error"):
            v["gate"] = {"passed": False, "checks": {}}
            continue
        m = v["metrics"]
        tot = m.get("total_return_pct")
        tot = -999.0 if tot is None else tot
        a = m.get("alpha_pct")
        a = -999.0 if a is None else a
        gain1 = _sub(v["h1"].get("alpha_pct"), b1.get("alpha_pct"))
        gain2 = _sub(v["h2"].get("alpha_pct"), b2.get("alpha_pct"))
        v["alpha_gain_pp"] = {"full": _sub(a, bm.get("alpha_pct")), "h1": gain1, "h2": gain2}
        ck = {"alpha_t": (m.get("alpha_t") if m.get("alpha_t") is not None else -9) > g["alpha_t_gt"]}
        if g["beat_null_own"]:
            ck["beat_null_own"] = v["null"].get("p80") is not None and tot >= v["null"]["p80"]
        if g["beat_null_multi"]:
            ck["beat_null_multi"] = v["null"].get("multi_p80") is not None and tot >= v["null"]["multi_p80"]
        ck["alpha_vs_baseline"] = (v["alpha_gain_pp"]["full"] or -99) >= g["alpha_gain_vs_baseline_pp"]
        ck["halves"] = gain1 is not None and gain2 is not None and gain1 >= g["halves_gain_pp"] and gain2 >= g["halves_gain_pp"]
        if g["mdd_not_worse"]:
            ck["mdd_not_worse"] = (m.get("max_drawdown_pct") or 999) <= (bm.get("max_drawdown_pct") or 0)
        ck["min_buys"] = (m.get("n_buy") or 0) >= g["min_buys"]
        if "unscreened" in v:
            ck["alpha_vs_unscreened"] = _sub(a, v["unscreened"].get("alpha_pct")) is not None and \
                a - (v["unscreened"].get("alpha_pct") or 0) >= g["alpha_gain_vs_unscreened_pp"]
        if "random_exclusion" in v and g["beat_random_exclusion"]:
            ck["beat_random_exclusion"] = (v["alpha_gain_pp"]["full"] or -99) >= v["random_exclusion"]["p80"]
        v["gate"] = {"passed": all(ck.values()), "checks": ck}
    # ── 显示分组 (基准 + 通过 + 未通过里最好的 top_n, 其余归档) ──
    passed = sorted([k for k, v in res.items() if v["gate"]["passed"]],
                    key=lambda k: -(res[k]["metrics"].get("total_return_pct") or -999))
    rest = sorted([k for k in res if k not in passed and not res[k].get("error")],
                  key=lambda k: -(res[k]["metrics"].get("total_return_pct") or -999))
    top_n = int(b["display"]["top_n"])
    groups = {"passed": passed, "best": rest[:top_n], "archived": rest[top_n:] + [k for k in res if res[k].get("error")]}
    summary = {"run_id": run_id, "backtest_id": backtest_id, "backtest_name": b.get("name"), "data_env": env,
               "status": "done", "created_at": datetime.now().isoformat(timespec="seconds"),
               "period": {"start": start, "end": end, "split": split}, "capital": cap, "gate_rules": g,
               "null": {"seeds": n_seeds, "q": q, "multi_p80": multi_p80}, "baseline": base, "variants": res,
               "groups": groups, "passed": passed, "families": families, "health": health,
               "config_hash": C.strategy_hash({"id": "bt", **{k: b[k] for k in ("gate", "period", "null")}}),
               "elapsed_s": round(time.time() - t0)}
    tmp = out_dir / "summary.tmp"
    tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    tmp.replace(out_dir / "summary.json")
    store.save_run(run_id, backtest_id=backtest_id, data_env=env, families=",".join(families), status="done",
                   period_start=start, period_end=end, n_variants=len(res), passed_json=json.dumps(passed),
                   config_hash=summary["config_hash"], health_ok=None if health is None else int(health["ok"]),
                   finished_at=datetime.now())
    _status(status_file, state="done", step="完成", progress=1.0)
    logger.info(f"[expert-follow] {run_id} 完成: {len(res)} 个方案, 通过 {passed or '无'} ({summary['elapsed_s']}s)")
    return summary


def _v3_metrics(m: Dict[str, Any]) -> Dict[str, Any]:
    """expert_v3 逐笔引擎的买入笔数叫 n_accepted_buys。"""
    out = keep(m)
    if out.get("n_buy") is None:
        out["n_buy"] = m.get("n_accepted_buys")
    return out


def _prev(dates: List[str], d: str) -> str:
    before = [x for x in dates if x < d]
    return before[-1] if before else d


def _sub(a, b):
    return None if a is None or b is None else round(float(a) - float(b), 2)


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="跟随高手统一回测")
    ap.add_argument("--backtest", default="unified_sim")
    ap.add_argument("--end", default="")
    ap.add_argument("--status-file", default="")
    ap.add_argument("--run-id", default="")
    a = ap.parse_args()
    setup_logging()
    try:
        s = run(a.backtest, a.end or None, a.status_file or None, a.run_id or None)
    except Exception as exc:
        rid = a.run_id or ""
        if rid:
            store.save_run(rid, backtest_id=a.backtest, data_env="?", status="error", error=str(exc)[:500],
                           finished_at=datetime.now())
        _status(a.status_file or None, state="error", error=str(exc)[:300])
        raise
    print(json.dumps({"run_id": s["run_id"], "status": s["status"], "passed": s.get("passed"),
                      "baseline": (s.get("baseline") or {}).get("metrics")}, ensure_ascii=False, default=float))


if __name__ == "__main__":
    main()
