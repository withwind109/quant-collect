"""统一退出策略 (2026-09-28 用户决策: 回测 / 模拟盘 / 实盘待确认单 / 卖出通知 共用一套规则与执行口径)。

执行口径 v2 (决策 1):
  止损      盘中触发: 当日最低价 ≤ 止损线 → 成交价 = 开盘价 (开盘即跌破) 或 止损线 (盘中跌破); T+1: 买入当天不止损。
            模拟盘 / 实盘: 盘中实时 (分钟最低价) 触发, 成交价同上 (相当于条件单)。
  止盈 / 满期 / 策略信号卖出 (排名掉出、高手卖出 …)
            收盘判断 → 下一交易日开盘成交 (回测: 次日开盘价; 模拟盘: 次日 09:30:30 之后第一次执行; 通知: 收盘后提醒"明日开盘卖出")。
  以前各引擎不一致 (管线 / WF / 跟随回测有"收盘判断、按同一收盘价成交"的乐观偏差), 见
  docs/统一退出策略_方案与实施_20260928.md。
规则参数: 策略配置有就用配置, 没有用平台统一 (止损 8% / 移动止盈分档 / 满 20 日), 任何情况兜底止损 20%
  (底层判断函数仍是 scripts/core/exit_rules.py, 这里只统一"何时判断、按什么价成交")。
管线专属持有规则 (双趋势持有 40 日 / 赢家站上 MA10 持有到 60 日 / 硬止损 −15%) 保留为管线专属 (决策 2), 只统一执行口径。
旧口径 v1 保留用于新旧对比 (环境变量 XCQ_EXIT_SEMANTICS=v1); 默认 v2。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from core import exit_rules as XR

POLICY_VERSION = 2


def semantics() -> str:
    """回测引擎用的执行口径: v2 (统一, 默认) / v1 (旧, 仅用于新旧对比重跑)。"""
    return "v1" if os.environ.get("XCQ_EXIT_SEMANTICS", "v2").lower() == "v1" else "v2"


@dataclass
class ExitPolicy:
    stop: float = XR.DEFAULT_STOP                     # 生效止损比例 (已含兜底)
    tp: Optional[Dict[str, Any]] = field(default_factory=dict)   # 止盈参数 (交给 exit_rules.take_profit_reason); None = 关闭
    label: str = "平台统一"

    # ── 止损 (盘中) ──
    def stop_level(self, cost: float) -> float:
        return cost * (1 - self.stop)

    def stop_fill(self, open_px: Optional[float], low_px: Optional[float], cost: float) -> Optional[Tuple[float, str]]:
        """一根日线 (或当日截至目前的开盘 / 最低) → (成交价, 原因) 或 None。"""
        if not cost or cost <= 0:
            return None
        lvl = self.stop_level(cost)
        if open_px is not None and open_px > 0 and open_px <= lvl:
            return float(open_px), "止损(开盘跌破)"
        if low_px is not None and low_px > 0 and low_px <= lvl:
            return float(lvl), "止损"
        return None

    # ── 收盘判断 (次日开盘执行) ──
    def close_exit(self, close_px: float, cost: float, high_since: float, days_held: int, highs=None) -> Optional[str]:
        if self.tp is None:
            return None
        return XR.take_profit_reason(close_px, cost, high_since, days_held, self.tp, highs)


def from_risk_controls(rc: Optional[Dict[str, Any]]) -> ExitPolicy:
    """跟随高手策略文件 risk_controls → ExitPolicy。"""
    stop, tp = XR.resolve(rc)
    return ExitPolicy(stop=stop, tp=tp, label="策略文件" if rc else "平台统一")


def from_parts(stop_pct: Optional[float], take_profit: Any = "default", top_fractal: bool = False) -> ExitPolicy:
    """通用: 止损比例 (小数, None=平台统一) + 止盈 ('default' / 'off' / dict)。"""
    stop = XR.effective_stop(stop_pct)
    if take_profit == "off" or take_profit is False:
        return ExitPolicy(stop=stop, tp=None)
    params = dict(XR.TAKE_PROFIT_DEFAULTS)
    if isinstance(take_profit, dict):
        params.update(take_profit)
    if top_fractal:
        params["top_fractal"] = True
    return ExitPolicy(stop=stop, tp=params)


def from_strategy(strategy: Optional[Dict[str, Any]]) -> ExitPolicy:
    """推荐体系策略行 (xcq_strategies) → ExitPolicy (与模拟盘 stops.resolve / resolve_exit 同一来源)。"""
    from strategy import stops as _stops
    s, e = _stops.resolve(strategy), _stops.resolve_exit(strategy)
    tp = None if e.get("mode") == "off" else {**XR.TAKE_PROFIT_DEFAULTS, **(e.get("params") or {})}
    if tp is not None and tp.get("enabled") is False:
        tp = None
    return ExitPolicy(stop=float(s["pct"]), tp=tp, label=f"{s.get('label')} · {e.get('label')}")
