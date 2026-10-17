"""跟随集合 (谁被跟、各给多少资金) —— 由策略文件的 universe / screen / rank / risk / weighting 决定 (2026-09-25)。

回测 (engine.py) 与模拟盘 (live.py) 共用这里, 以前散落的几套选人逻辑都等价地落在这一处:
  v2      universe=all_active (+关注), rank.days=1, pick.top_k=10     ≡ backtest_v2.follow_set
  v2.1    同上 + risk (剔除后下一名递补)                              ≡ follow_v21.follow_set_v21
  v3.1    screen (长窗口质量筛选) + rank (近 N 日排名)                ≡ v3_pool.screen_rank_set
  v1 镜像 universe=watchlist, pick.all                                ≡ 关注高手等权
排名指标 = 逐日重建净值的近 N 日收益 (只用信号日及以前的数据); 超过 max_idle_days 没调仓的僵尸组合与重建异常值不参与排名。

迁移自 scripts/expert_follow/selector.py (2026-10-16): 三处惰性导入改绝对路径
(scripts.expert_v3.pool → strategy.v3_pool, scripts.expert_follow.curve → strategy.curve,
scripts.experts.follow_v21 → strategy.follow_v21); 其余逐行原样。
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

FollowSet = List[Tuple[str, float, float]]      # [(expert_id, 资金权重, 排名指标)]
RET_CAP = 10.0


def metric(ctx, t: int, n: int, max_idle: int = 60) -> np.ndarray:
    """截至 t 收盘的近 n 日收益; n=1 时与 backtest_v2.Ctx.metric('day') 完全一致。"""
    if t - n < 0:
        return np.full(len(ctx.experts), np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        m = ctx.nav[t] / ctx.nav[t - n] - 1
    m[(t - ctx.last_trade[t]) > max_idle] = np.nan
    m[np.abs(m) >= min(0.5 * n, RET_CAP)] = np.nan
    return m


def weights(es: List[str], scheme: str, ms: Optional[List[float]] = None) -> FollowSet:
    n = len(es)
    if not n:
        return []
    raw = [n - k for k in range(n)] if scheme == "rank" else [1.0] * n
    tot = float(sum(raw))
    ms = ms or [0.0] * n
    return [(e, raw[k] / tot, float(ms[k])) for k, e in enumerate(es)]


_POOLS: Dict[str, Any] = {}


def pool_for(ctx, screen: Dict[str, Any]):
    """质量筛选池 (v3_pool.Pool), 按 (ctx, 筛选参数) 缓存; 收益>0 开/关共用同一个池 (passed / elig_noret)。"""
    from strategy.v3_pool import Pool
    sc = {**screen, "min_ret": 0.0 if screen.get("min_ret") is None else float(screen["min_ret"])}
    key = f"{id(ctx)}:{json.dumps(sc, sort_keys=True)}"
    if key not in _POOLS:
        _POOLS[key] = Pool(ctx, {**sc, "pos_tiers": [0.1], "neg_tiers": [1.0]})
    return _POOLS[key]


class Selector:
    def __init__(self, ctx, s: Dict[str, Any]):
        self.ctx, self.s = ctx, s
        u = s["universe"]
        self.max_idle = int(u.get("max_idle_days") or 60)
        if u["source"] == "watchlist":
            self.members = [e for e in ctx.watch if e in ctx.eidx]
        elif u["source"] == "expert_ids":
            self.members = [e for e in (u.get("expert_ids") or []) if e in ctx.eidx]
        else:
            self.members = None                       # 全体
        self.member_idx = None if self.members is None else np.array([ctx.eidx[e] for e in self.members], dtype=int)
        self.watch = [e for e in ctx.watch if e in ctx.eidx] if u.get("include_watchlist") else []
        self.pool = pool_for(ctx, s["screen"]) if s.get("screen") else None
        self.curve = None
        if s.get("curve_screen"):
            from strategy.curve import CurvePool
            self.curve = CurvePool(ctx, s["curve_screen"])
        self.rank = s.get("rank") or {}
        self.pick = self.rank.get("pick") or {}
        self.risk = s.get("risk") or None
        self._first_t = None
        self.excluded_log: Dict[int, int] = {}
        self.k_log: Dict[int, int] = {}

    # ── 候选 (排名前的资格): universe ∩ 排名指标有效 ∩ 质量筛选 ──
    def candidates(self, t: int, screened: bool = True) -> Tuple[np.ndarray, np.ndarray]:
        n = int(self.rank.get("days") or 1)
        m = metric(self.ctx, t, n, self.max_idle)
        idx = np.where(np.isfinite(m))[0] if self.member_idx is None else self.member_idx[np.isfinite(m[self.member_idx])]
        if screened and self.pool is not None:
            pm = self.pool.metrics(t)
            if pm is None:
                return m, np.array([], dtype=int)
            ok = pm["passed"] if self.s["screen"].get("min_ret") is not None else pm["elig_noret"]
            if len(ok) < int(self.s["screen"]["min_pool"]):
                return m, np.array([], dtype=int)
            idx = np.intersect1d(idx, ok)
        elif not screened and self.pool is not None:       # 不筛选对照: 只要求近 recent_days 日有交易
            rd = int(self.s["screen"]["recent_days"])
            idx = idx[(t - self.ctx.last_trade[t][idx]) < rd]
        if self.curve is not None:
            if screened:
                ok = self.curve.passed(t)
                idx = np.intersect1d(idx, ok) if len(ok) >= int(self.curve.cfg["min_pool"]) else np.array([], dtype=int)
            else:                                          # 不筛选对照: 同样的活跃要求, 不看曲线
                idx = idx[(t - self.ctx.last_trade[t][idx]) < int(self.curve.cfg["recent_days"])]
        order = idx[np.argsort(-m[idx], kind="stable")] if len(idx) else idx
        return m, order

    def _k(self, n_cand: int) -> int:
        if self.pick.get("top_k"):
            return int(self.pick["top_k"])
        if self.pick.get("top_pct"):
            return max(1, int(round(n_cand * float(self.pick["top_pct"])))) if n_cand else 0
        return n_cand

    def _excluded(self, i: int, t: int, m1: np.ndarray) -> bool:
        from strategy import follow_v21 as V21
        return V21.excluded(self.ctx, i, t, m1, self.risk)

    def follow(self, t: int) -> FollowSet:
        ctx = self.ctx
        if self.pick.get("all"):
            es = [e for e in (self.members or ctx.experts) if np.isfinite(ctx.nav[t, ctx.eidx[e]])]
            self.k_log[t] = len(es)
            return weights(es, self.s["weighting"])
        m, order = self.candidates(t)
        if self.pick.get("band"):
            lo, hi = self.pick["band"]
            order = order[lo - 1:hi]
            k = len(order)
        else:
            k = self._k(len(order))
        m1 = metric(ctx, t, 1, self.max_idle) if self.risk else None
        out: List[int] = []
        n_ex = 0
        wset = set(self.watch)
        for i in order:
            if len(out) >= k:
                break
            if wset and ctx.experts[i] in wset:              # 关注名单另外全员入选, 不占动态名额 (同 v2)
                continue
            if self.risk and self._excluded(int(i), t, m1):
                n_ex += 1
                continue
            out.append(int(i))
        self.excluded_log[t] = n_ex
        es = [ctx.experts[i] for i in out]
        if self.watch:                                     # v2 线上版: 关注名单全员 + 动态前 K (按指标统一排序加权)
            val = {e: (float(m[ctx.eidx[e]]) if np.isfinite(m[ctx.eidx[e]]) else -np.inf) for e in self.watch}
            for i in out:
                val.setdefault(ctx.experts[i], float(m[i]))
            es = sorted(val, key=lambda e: -val[e])
        self.k_log[t] = len(es)
        return weights(es, self.s["weighting"], [float(m[ctx.eidx[e]]) if np.isfinite(m[ctx.eidx[e]]) else 0.0 for e in es])

    # ── 对照组 ──
    def null_fn(self, seed: int) -> Callable[[int], FollowSet]:
        """随机对照 (排名有没有用): 每天从同一资格池 (筛选后、排名前) 随机抽与真实名单同样多的人。"""
        def fn(t: int) -> FollowSet:
            k = self.k_log.get(t)
            if k is None:
                k = len(self.follow(t))
            if not k:
                return []
            if self.pick.get("all") or self.s["universe"]["source"] != "all_active":
                _, cand = self.candidates_all_active(t)
            elif self.curve is not None and float(self.pick.get("top_pct") or 0) >= 1:
                _, cand = self.candidates(t, screened=False)   # 跟全部合格者: 随机对照 = 从全体活跃里随机挑同样多的人 (问题 = 曲线筛选有没有用)
            else:
                _, cand = self.candidates(t)
            if not len(cand):
                return []
            rng = np.random.default_rng(seed * 100003 + t)
            pick = rng.choice(cand, min(k, len(cand)), replace=False)
            return weights([self.ctx.experts[i] for i in pick], self.s["weighting"])
        return fn

    def candidates_all_active(self, t: int):
        """关注/指定名单类策略的随机对照池: 全体活跃高手 (问题 = 这几位是否好过随机挑同样多的活跃高手)。"""
        m = metric(self.ctx, t, int(self.rank.get("days") or 1), self.max_idle)
        return m, np.where(np.isfinite(m))[0]

    def unscreened_fn(self) -> Callable[[int], FollowSet]:
        """不筛选对照 (筛选有没有用): 同排名周期、同人数, 候选只要求近 recent_days 日活跃。"""
        def fn(t: int) -> FollowSet:
            k = self.k_log.get(t)
            if k is None:
                k = len(self.follow(t))
            if not k:
                return []
            _, order = self.candidates(t, screened=False)
            return weights([self.ctx.experts[i] for i in order[:k]], self.s["weighting"])
        return fn

    def random_exclusion_fn(self, seed: int) -> Callable[[int], FollowSet]:
        """随机剔除对照 (风控规则有没有用): 从排名前 K+剔除数 里随机去掉同样多的人, 其余按名次取前 K。"""
        def fn(t: int) -> FollowSet:
            if t not in self.excluded_log:
                self.follow(t)
            n_ex = self.excluded_log.get(t, 0)
            _, order = self.candidates(t)
            k = self._k(len(order))
            head = list(order[:k + n_ex])
            if n_ex:
                drop = set(np.random.default_rng(seed * 100003 + t).choice(len(head), min(n_ex, len(head)), replace=False))
                head = [x for j, x in enumerate(head) if j not in drop]
            return weights([self.ctx.experts[i] for i in head[:k]], self.s["weighting"])
        return fn
