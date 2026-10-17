"""跟随高手 v3 —— 高手池: 逐日(PIT)指标、四规则漏斗、正向/反向批次。

所有指标在交易日下标 tp 收盘时计算, 只用 tp 及以前的数据 (信号日 t 用 tp = t-1):
  收益/回撤  逐日重建净值 ctx.nav (scripts/experts/pit_nav.py) 在 [tp-W, tp] 上的收益与最大回撤
  调仓次数   (tp-W, tp] 内仓位有变化的调仓笔数; 最近交易 = tp 前最后一次调仓
  平仓/胜率  每只股按加权平均成本记账, 每次减仓 = 一笔平仓, (减仓价/成本 - 1 - 往返成本) > 0 记为胜;
            只统计平仓发生在 (tp-W, tp] 内的

迁移自 scripts/expert_v3/pool.py (2026-10-16)。变更:
  * logger 导入改 core.logger; 其余计算逻辑原样 (纯 numpy, 无存储依赖);
  * ctx 的类型来自并行任务 strategy.backtest_v2.Ctx (属性 .nav[T,N] .experts .eidx .hist .px;
    px 有 .dates .day_of() .cidx .adj .fac .limits) —— 用 TYPE_CHECKING 字符串注解, 避免循环导入;
  * 原 import scripts.core.db.q / _cal 的用法本文件没有, 无需替换。
"""
from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import numpy as np

from core.logger import logger

if TYPE_CHECKING:                     # 并行任务产出的 Ctx; 仅类型注解用, 运行时不导入
    from strategy.backtest_v2 import Ctx

RET_CAP = 10.0   # 重建异常(窗口收益 ≥ 1000%) 的组合不参与


class Pool:
    def __init__(self, ctx: "Ctx", spec: Dict[str, Any]):
        self.ctx, self.spec = ctx, spec
        px = ctx.px
        T, N = len(px.dates), len(ctx.experts)
        tr = np.zeros((T, N), np.int32)
        cl = np.zeros((T, N), np.int32)
        wn = np.zeros((T, N), np.int32)
        last = np.full((T, N), -1, np.int32)
        cost = spec["win_cost"]
        for i, e in enumerate(ctx.experts):
            pos: Dict[str, List[float]] = defaultdict(lambda: [0.0, 0.0])   # code -> [仓位%, 平均成本(复权)]
            prev_w: Dict[str, float] = {}   # [2026-09-25] 实盘流水无 weight_before: 用该股上一条 weight_after 推断调仓前仓位
            for r in ctx.hist[e]["records"]:
                t = px.day_of(r["trade_date"])
                c = r.get("stock_code")
                if t is None or c not in px.cidx:
                    continue
                wb, wa = r.get("weight_before"), r.get("weight_after")
                wb = float(wb) if wb not in (None, "") else None
                wa = float(wa) if wa not in (None, "") else None
                if wb is None:
                    wb = prev_w.get(c, 0.0)
                if wa is not None:
                    prev_w[c] = wa
                if wa is None or wa == wb:
                    continue
                tr[t, i] += 1
                p = float(r.get("price") or 0) * px.fac[t, px.cidx[c]]
                if not (p > 0):
                    continue
                st = pos[c]
                if wa > wb:
                    st[1] = (st[1] * st[0] + p * (wa - wb)) / wa if wa > 0 else p
                    st[0] = wa
                else:
                    if st[1] > 0:
                        cl[t, i] += 1
                        wn[t, i] += int(p / st[1] - 1 - cost > 0)
                    st[0] = wa
                    if wa <= 0:
                        st[1] = 0.0
            ts = np.where(tr[:, i] > 0)[0]
            k, lt = 0, -1
            for t in range(T):
                while k < len(ts) and ts[k] <= t:
                    lt = ts[k]
                    k += 1
                last[t, i] = lt
        self.c_tr, self.c_cl, self.c_wn = (np.cumsum(a, 0) for a in (tr, cl, wn))
        self.last = last
        self._cache: Dict[int, Dict[str, Any]] = {}
        logger.info(f"[v3-pool] 就绪: {N} 个组合, 窗口 {spec['window']} 日")

    def _win_sum(self, c: np.ndarray, tp: int, W: int) -> np.ndarray:
        return c[tp] - (c[tp - W] if tp - W >= 0 else 0)

    def metrics(self, tp: int) -> Optional[Dict[str, Any]]:
        """tp 收盘时每位高手的窗口指标 + 漏斗掩码 + 批次排序。窗口不够长返回 None。"""
        if tp in self._cache:
            return self._cache[tp]
        s, W = self.spec, self.spec["window"]
        if tp < W:
            return None
        win = self.ctx.nav[tp - W:tp + 1]
        complete = np.isfinite(win).all(0)
        with np.errstate(invalid="ignore", divide="ignore"):
            ret = win[-1] / win[0] - 1
            ratio = win / np.fmax.accumulate(win, axis=0)
            # 全 NaN 列（组合窗口内无净值）填中性值 1.0, 避免 nanmin 的
            # "All-NaN slice" RuntimeWarning; 该列本就被 complete 排除
            ratio[:, np.isnan(ratio).all(axis=0)] = 1.0
            mdd = 1 - np.nanmin(ratio, axis=0)
        n_tr = self._win_sum(self.c_tr, tp, W)
        n_cl = self._win_sum(self.c_cl, tp, W)
        n_wn = self._win_sum(self.c_wn, tp, W)
        with np.errstate(invalid="ignore", divide="ignore"):
            wr = np.where(n_cl > 0, n_wn / np.maximum(n_cl, 1), np.nan)
        ok = complete & np.isfinite(ret) & (np.abs(ret) < RET_CAP)
        active = (n_tr >= s["min_trades"]) & (self.last[tp] >= tp - s["recent_days"] + 1) & (self.last[tp] >= 0)
        m1 = ok & (ret > s["min_ret"])
        m2 = m1 & (mdd <= s["max_mdd"])
        m3 = m2 & active
        m4 = m3 & (n_cl >= s["min_closed"]) & (wr >= s["min_win"])
        # 不含规则① (收益>0) 的合格池: v3.1 "筛选 + 排名" 模式对照用 (长窗收益会反转, 规则①可能有害)
        m4b = ok & (mdd <= s["max_mdd"]) & active & (n_cl >= s["min_closed"]) & (wr >= s["min_win"])
        passed = np.where(m4)[0]
        passed = passed[np.argsort(-ret[passed], kind="stable")]
        neg = np.where(ok & (ret < 0) & active)[0]
        neg = neg[np.argsort(ret[neg], kind="stable")]           # 最差在前
        out = {"tp": tp, "ret": ret, "mdd": mdd, "n_tr": n_tr, "n_cl": n_cl, "wr": wr,
               "funnel": {"名单": int(len(ret)), "窗口净值完整": int(ok.sum()), "①收益>0": int(m1.sum()),
                          "②回撤≤阈值": int(m2.sum()), "③活跃": int(m3.sum()), "④胜率达标": int(m4.sum())},
               "passed": passed, "neg": neg,
               "elig_noret": np.where(m4b)[0], "active": np.where(ok & active)[0],
               "pos_idx": {int(i): k for k, i in enumerate(passed)},
               "neg_idx": {int(i): k for k, i in enumerate(neg)}}
        self._cache[tp] = out
        return out

    def tier_n(self, n_total: int, x: float, positive: bool) -> int:
        if positive and n_total < self.spec["min_pool"]:
            return 0
        return n_total if x >= 1 else max(1, int(round(n_total * x))) if n_total else 0

    def tier(self, tp: int, x: float, positive: bool = True) -> List[str]:
        m = self.metrics(tp)
        if m is None:
            return []
        arr = m["passed"] if positive else m["neg"]
        return [self.ctx.experts[i] for i in arr[:self.tier_n(len(arr), x, positive)]]

    def table(self, tp: int, x: float, positive: bool = True, limit: int = 200) -> List[Dict[str, Any]]:
        """页面预览: 批次里每位高手的指标。"""
        m = self.metrics(tp)
        if m is None:
            return []
        arr = m["passed"] if positive else m["neg"]
        rows = []
        for k, i in enumerate(arr[:min(self.tier_n(len(arr), x, positive), limit)]):
            e = self.ctx.experts[i]
            rows.append({"rank": k + 1, "expert_id": e, "name": self.ctx.hist[e].get("name") or e,
                         "ret_pct": round(float(m["ret"][i]) * 100, 2), "mdd_pct": round(float(m["mdd"][i]) * 100, 2),
                         "trades": int(m["n_tr"][i]), "closed": int(m["n_cl"][i]),
                         "win_pct": round(float(m["wr"][i]) * 100, 1) if np.isfinite(m["wr"][i]) else None})
        return rows


# ── v3.1 "筛选 + 排名" (2026-09-24): 长窗口只做质量筛选 (谁有资格), 短窗口做排名 (跟谁、跟多少) ──

def short_ret(ctx: "Ctx", tp: int, n: int) -> np.ndarray:
    """截至 tp 收盘的近 n 日收益 (逐日重建净值)。"""
    if tp - n < 0:
        return np.full(len(ctx.experts), np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        r = ctx.nav[tp] / ctx.nav[tp - n] - 1
    r[~np.isfinite(r) | (np.abs(r) >= RET_CAP)] = np.nan
    return r


def parse_sr(variant: str):
    """'S1_N3_T10' → (规则①开, 3 日, ('top', 10)); 'S0_N1_P0.05' → (关, 1 日, ('pct', 0.05)); 其他返回 None。"""
    parts = str(variant).split("_")
    if len(parts) != 3 or parts[0] not in ("S0", "S1") or not parts[1].startswith("N"):
        return None
    try:
        n = int(parts[1][1:])
        g = ("top", int(parts[2][1:])) if parts[2].startswith("T") else ("pct", float(parts[2][1:]))
    except ValueError:
        return None
    return parts[0] == "S1", n, g


def screen_rank_set(pool: "Pool", tp: int, ret_rule: bool, n: int, group) -> List[str]:
    """合格池(可选含规则①)按近 n 日收益降序取 top K / 前 X%。合格池 < min_pool 人时当天不出名单。"""
    m = pool.metrics(tp)
    if m is None:
        return []
    cand = m["passed"] if ret_rule else m["elig_noret"]
    if len(cand) < pool.spec["min_pool"]:
        return []
    r = short_ret(pool.ctx, tp, n)
    cand = np.array([i for i in cand if np.isfinite(r[i])], dtype=int)
    if not len(cand):
        return []
    order = cand[np.argsort(-r[cand], kind="stable")]
    k = group[1] if group[0] == "top" else max(1, int(round(len(order) * group[1])))
    return [pool.ctx.experts[i] for i in order[:k]]


def unscreened_set(pool: "Pool", tp: int, n: int, k: int) -> List[str]:
    """对照: 不做质量筛选, 所有近 recent_days 日有交易的组合按近 n 日收益取前 k (与 v2 口径一致, 人数与筛选方案相同)。"""
    if k <= 0:
        return []
    act = np.where((pool.last[tp] >= tp - pool.spec["recent_days"] + 1) & (pool.last[tp] >= 0))[0]
    r = short_ret(pool.ctx, tp, n)
    act = act[np.isfinite(r[act])]
    order = act[np.argsort(-r[act], kind="stable")]
    return [pool.ctx.experts[i] for i in order[:k]]


def rank_weights(es: List[str]):
    """名次线性加权 (与 v2 相同): [(expert, 资金权重, 0)]。"""
    n = len(es)
    raw = [n - k for k in range(n)]
    return [(e, raw[k] / sum(raw), 0.0) for k, e in enumerate(es)] if n else []
