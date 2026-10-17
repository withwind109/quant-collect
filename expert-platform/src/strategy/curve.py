"""PnL 曲线"稳定向右上"筛选 (2026-09-27 用户决策: 容忍版) —— 策略文件 curve_screen 字段, 与画像页 ⭐ 硬条件同一口径。

逐组合逐日 (PIT, 只用 t 及以前): 从组合第一个有净值的日子起算
  累计收益最低点 ≥ min_cum_floor (−5%)
  log 净值对时间线性拟合 斜率 > 0 且 R² ≥ min_r2 (0.7)
  每 seg (20) 个交易日一段, 上涨段占比 ≥ min_up_seg (2/3) (最后一段不满 20 日也算一段)
  有净值天数 ≥ min_days (60), 近 recent_days 日有调仓
画像页 scripts/expert_profile/run.py 的 curve() 是同一算法 (那里只算最新一天)。

迁移自 scripts/expert_follow/curve.py (2026-10-16): 无项目内导入, 逐行原样。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np

CURVE_DEFAULTS: Dict[str, Any] = {"min_cum_floor": -0.05, "min_r2": 0.70, "min_up_seg": 2 / 3, "seg": 20,
                                  "min_days": 60, "recent_days": 10, "min_pool": 1}


def curve_ok_matrix(nav: np.ndarray, cfg: Dict[str, Any]) -> np.ndarray:
    """T×E 布尔矩阵: 该组合在 t 日收盘时曲线是否"稳定向上" (不含活跃度)。"""
    T, E = nav.shape
    L = int(cfg["seg"])
    ok = np.zeros((T, E), dtype=bool)
    for i in range(E):
        v = nav[:, i]
        fin = np.where(np.isfinite(v) & (v > 0))[0]
        if len(fin) < cfg["min_days"]:
            continue
        f = fin[0]
        s = v[f:].copy()
        for k in range(1, len(s)):                      # 中途缺值 (极少) 用前值
            if not (np.isfinite(s[k]) and s[k] > 0):
                s[k] = s[k - 1]
        n_all = len(s)
        cmin = np.fmin.accumulate(s) / s[0] - 1
        y = np.log(s)
        x = np.arange(n_all, dtype=float)
        n = x + 1
        Sx, Sy, Sxx, Syy, Sxy = np.cumsum(x), np.cumsum(y), np.cumsum(x * x), np.cumsum(y * y), np.cumsum(x * y)
        with np.errstate(invalid="ignore", divide="ignore"):
            cov = n * Sxy - Sx * Sy
            vx = n * Sxx - Sx * Sx
            vy = n * Syy - Sy * Sy
            r2 = np.where((vx > 0) & (vy > 0), cov * cov / (vx * vy), 0.0)
        seg_start = np.arange(0, n_all, L)
        full_up = np.zeros(n_all, dtype=int)                # t 时已走完的整段里上涨的段数
        for k0 in seg_start:
            k1 = k0 + L
            if k1 < n_all and s[k1] > s[k0]:
                full_up[k1:] += 1
        for p in range(int(cfg["min_days"]) - 1, n_all):
            n_full = p // L                                   # 已完整的段 (终点 ≤ p)
            last0 = n_full * L
            n_seg, up = n_full, full_up[p]
            if last0 < p:                                     # 不满一段的尾段
                n_seg += 1
                up += int(s[p] > s[last0])
            if n_seg == 0:
                continue
            ok[f + p, i] = bool(cmin[p] >= cfg["min_cum_floor"] and cov[p] > 0 and r2[p] >= cfg["min_r2"]
                                and up / n_seg >= cfg["min_up_seg"])
    return ok


_CACHE: Dict[str, np.ndarray] = {}


class CurvePool:
    def __init__(self, ctx, cfg: Optional[Dict[str, Any]]):
        self.ctx = ctx
        self.cfg = {**CURVE_DEFAULTS, **(cfg or {})}
        key = f"{id(ctx)}:{sorted(self.cfg.items())}"
        if key not in _CACHE:
            _CACHE[key] = curve_ok_matrix(ctx.nav, self.cfg)
        self.ok = _CACHE[key]

    def passed(self, t: int) -> np.ndarray:
        act = (t - self.ctx.last_trade[t]) < int(self.cfg["recent_days"])
        act &= self.ctx.last_trade[t] >= 0
        return np.where(self.ok[t] & act)[0]
