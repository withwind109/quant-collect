"""β 调整后的 α (D25, 2026-09-22): 擂台闸门与 E1 用它替代"收益 − 基准"的超额。

为什么: 回测管线的仓位/暴露和指数很不一样(样本诊断里 288 个管线对中证500的 β≈0), "收益 − 基准"只是在减去大盘的涨跌:
基准从 +1.03% 变成 −7.86%, 样本外的"超额"就凭空抬高约 9 个点, 164/288 个管线因此通过闸门, 而它们没有一个 α 显著。
做法: 回测日净值的日收益 r 对基准日收益 b 做 OLS: r = a + β·b + ε, 报告 β、窗口 α(a×天数, 百分点)、α 的 t 值。
日净值取自回测运行的 equity_curve; WF 取各窗口 summary.json 的 combined.equity_curve (逐窗口重置为 100, 窗口边界的那一天不计)。

迁移自 scripts/recs/alpha.py (2026-10-16)。变更:
  * 基准行情: 源经 qlib 个股 K 线 + xcq_index_bars 取中证500 (BENCH_CODE=IDX_000905 / 000905.SH);
    按迁移约定改为 strategy.prices.index_closes("000300.SH", start, end) (沪深300, 本地价格库,
    缺失自动 akshare 下载) —— 基准指数由中证500 换为沪深300, 其余计算逻辑原样;
  * 存储: xcq_backtest_runs (alpha.runs_stats 读) 本项目无回测运行数据, recs_store 建空表保持查询链
    (回测运行属并行任务 strategy.backtest_v2), 查询返回空 -> 对应 run_id 结果为 None;
  * bench_closes 保留源的 12 日回看 (保证 bench_returns 首日有前收盘), 只是数据源换成 index_closes。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from strategy import prices, recs_store as store

BENCH_INDEX = "000300.SH"   # 迁移约定: 基准指数 (源为中证500 000905.SH)
BENCH_NAME = "沪深300"

_BENCH: Dict[Tuple[str, str], Dict[str, float]] = {}
_RUNS: Dict[int, Optional[Dict[str, float]]] = {}       # 回测记录不可变 -> 按 run_id 永久缓存
_SER: Dict[int, Tuple[List[str], List[float]]] = {}      # 日收益序列(给"纯运气"自助法用)
_NULL: Dict[Tuple[int, int, int], Optional[Dict[str, Any]]] = {}


def bench_closes(start: str, end: str) -> Dict[str, float]:
    k = (start, end)
    if k not in _BENCH:
        s = (date.fromisoformat(start) - timedelta(days=12)).isoformat()
        _BENCH[k] = prices.index_closes(BENCH_INDEX, s, end)
    return _BENCH[k]


def bench_returns(start: str, end: str) -> Dict[str, float]:
    c = bench_closes(start, end)
    ds = sorted(c)
    return {ds[i]: c[ds[i]] / c[ds[i - 1]] - 1 for i in range(1, len(ds))}


def regress(days: List[str], rets: List[float], b: Dict[str, float]) -> Optional[Dict[str, float]]:
    y, x = [], []
    for d, r in zip(days, rets):
        if d in b:
            y.append(r)
            x.append(b[d])
    if len(y) < 20:
        return None
    y, x = np.array(y), np.array(x)
    X = np.column_stack([np.ones_like(x), x])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ coef
    s2 = float(resid @ resid) / (len(y) - 2)
    cov = s2 * np.linalg.inv(X.T @ X)
    a, beta = float(coef[0]), float(coef[1])
    t = a / float(np.sqrt(cov[0, 0])) if cov[0, 0] > 0 else 0.0
    return {"beta": beta, "alpha": a * len(y) * 100.0, "alpha_t": t, "n": len(y)}


def _curve_rets(ec: List[Dict[str, Any]], initial: float) -> Tuple[List[str], List[float]]:
    days = [x["d"] for x in ec]
    eq = np.array([float(x["e"]) for x in ec])
    prev = np.concatenate([[initial], eq[:-1]])
    return days, list(eq / prev - 1)


def runs_stats(run_ids: List[int], window: Tuple[str, str]) -> Dict[int, Optional[Dict[str, float]]]:
    """批量: {run_id: {beta, alpha, alpha_t, n}|None}。只读库里没算过的, 结果按 run_id 缓存。"""
    todo = [i for i in run_ids if i not in _RUNS]
    if todo:
        b = bench_returns(*window)
        for k in range(0, len(todo), 100):
            chunk = todo[k:k + 100]
            ph = ",".join(f":i{j}" for j in range(len(chunk)))
            rows = store.fetch(f"SELECT id,equity_curve,initial_capital FROM xcq_backtest_runs WHERE id IN ({ph})",
                               {f"i{j}": v for j, v in enumerate(chunk)})
            got = set()
            for r in rows:
                got.add(int(r["id"]))
                try:
                    ec = json.loads(r["equity_curve"]) if r["equity_curve"] else []
                    d, rt = _curve_rets(ec, float(r["initial_capital"] or 1_000_000)) if ec else ([], [])
                    _RUNS[int(r["id"])] = regress(d, rt, b) if d else None
                    if d:
                        _SER[int(r["id"])] = (d, rt)
                except Exception:  # noqa: BLE001
                    _RUNS[int(r["id"])] = None
            for i in chunk:
                if i not in got:
                    _RUNS[i] = None
    return {i: _RUNS.get(i) for i in run_ids}


def wf_stats(file_path: Optional[str]) -> Optional[Dict[str, float]]:
    """WF 滚动结果: 把各窗口的日收益拼起来对基准回归。"""
    if not file_path:
        return None
    root = Path(file_path)
    days: List[str] = []
    rets: List[float] = []
    for w in sorted(root.glob("window_*/summary.json")):
        try:
            ec = json.loads(w.read_text(encoding="utf-8"))["combined"]["equity_curve"]
        except Exception:  # noqa: BLE001
            continue
        if len(ec) < 2:
            continue
        d, r = _curve_rets(ec, float(ec[0]["e"]))     # 窗口起点 e=100, 首日收益记 0 -> 丢掉
        days += d[1:]
        rets += r[1:]
    if len(days) < 20:
        return None
    return regress(days, rets, bench_returns(min(days), max(days)))


def null_draws(run_id: int, window: Tuple[str, str], n: int = 300, seed: int = 20260922) -> Optional[Dict[str, np.ndarray]]:
    """"若该回测的 α 真的为 0"会长什么样: 保留它观察到的 β 和残差波动, 把残差有放回重抽 n 次, 得到 n 组
    (α 的 t 值, 总收益%, 夏普)。用来估计"纯运气通过闸门的概率"。
    偏乐观: 真没技能的策略还有交易成本拖累(α<0), 这里 α=0, 所以算出来的"运气通过数"偏多, 用来提醒"通过≠有能力"是保守的。"""
    key = (run_id, n, seed)
    if key in _NULL:
        return _NULL[key]
    ser = _SER.get(run_id)
    out = None
    if ser:
        b = bench_returns(*window)
        y, x = [], []
        for d, r in zip(*ser):
            if d in b:
                y.append(r)
                x.append(b[d])
        if len(y) >= 20:
            y, x = np.array(y), np.array(x)
            X = np.column_stack([np.ones_like(x), x])
            coef, *_ = np.linalg.lstsq(X, y, rcond=None)
            beta = float(coef[1])
            resid = y - X @ coef
            rng = np.random.default_rng(seed + run_id)
            R = beta * x[None, :] + resid[rng.integers(0, len(y), size=(n, len(y)))]        # n × T, α=0
            XtXi = np.linalg.inv(X.T @ X)
            C = (XtXi @ X.T) @ R.T                                                           # 2 × n
            E = R.T - X @ C
            s2 = (E * E).sum(axis=0) / (len(y) - 2)
            se_a = np.sqrt(s2 * XtXi[0, 0])
            t = np.where(se_a > 0, C[0] / np.where(se_a > 0, se_a, 1), 0.0)
            ret = (np.prod(1 + R, axis=1) - 1) * 100
            sd = R.std(axis=1, ddof=1)
            sh = np.where(sd > 0, R.mean(axis=1) / np.where(sd > 0, sd, 1) * np.sqrt(252), 0.0)
            out = {"alpha_t": t, "ret": ret, "sharpe": sh}
    _NULL[key] = out
    return out
