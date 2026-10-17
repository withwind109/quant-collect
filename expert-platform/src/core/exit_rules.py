"""统一止损 / 止盈 / 时间平仓规则 (2026-09-26 用户决策 D1)。

模拟盘盘中复核 (scripts/dashboard/paper_trading.check_intraday_stop_take) 与跟随高手回测引擎
(scripts/experts/follow_backtest_v2.run_variant / scripts/expert_follow/engine.run_signal) 用同一份判断, 口径一致:
  止损   价格 ≤ 成本 × (1 − 止损比例); 比例 = 策略设置, 未设置 → 平台统一 8%; 任何情况下兜底 20%
  止盈   平台统一: 持有天数 > max_hold_days (默认 20) → 时间平仓;
         盈利 ≥ 8% 后, 距入场以来最高价回撤超过分档阈值 (盈利 >30% / >20% / >12% / 其余 → 10% / 8% / 6% / 4%) → 移动止盈;
         盈利 > 25% 且持有 > 12 天 → 止盈
         (策略配置带顶分止盈时: 盈利 > 12% 且最近几根 K 线出现顶分 → 止盈; 管线 exit 模式与 WF 开启止盈时带)
  止损/止盈规则的来源: 策略配置有就按配置 (WF json5 backtest.stop_loss/take_profit, 管线参数, 跟随高手 risk_controls),
  没有才用平台统一规则 (用户决策 2026-09-26)。
  入场当天不检查 (A 股 T+1)。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

DEFAULT_STOP = 0.08
FLOOR_STOP = 0.20
TAKE_PROFIT_DEFAULTS: Dict[str, Any] = {
    "max_hold_days": 20,
    "trail_pct_small": 4, "trail_pct_medium": 6, "trail_pct_large": 8, "trail_pct_huge": 10,
}


def effective_stop(pct: Optional[float]) -> float:
    """策略止损 → 实际生效值: 未设置用平台统一 8%, 宽于 20% 或关闭按 20% 兜底。"""
    p = DEFAULT_STOP if pct in (None, "") else float(pct)
    return FLOOR_STOP if p <= 0 or p > FLOOR_STOP else p


def resolve(risk_controls: Optional[Dict[str, Any]]) -> Tuple[float, Optional[Dict[str, Any]]]:
    """策略文件 risk_controls → (生效止损比例, 止盈参数 或 None=关闭止盈)。"""
    rc = risk_controls or {}
    stop = effective_stop(rc.get("stop_loss"))
    tp = rc.get("take_profit", "default")
    if tp == "off":
        return stop, None
    params = dict(TAKE_PROFIT_DEFAULTS)
    if isinstance(tp, dict):
        params.update(tp)
    return stop, params


def top_fractal_hit(highs) -> bool:
    """顶分形态 (与 backtest_pipeline.detect_sell_signal / stock_walkforward._take_profit_signal 同一写法):
    持有期最近 6 根 K 线里, 某根最高价同时高于前一根和后一根。highs = 入场以来逐日最高价 (最后一个是当前)。"""
    n = len(highs)
    for i in range(max(2, n - 6), n - 1):
        a, b, c = highs[i - 1], highs[i], highs[i + 1]
        if a is not None and b is not None and c is not None and b > a and b > c:
            return True
    return False


def take_profit_reason(price: float, cost: float, high_since_entry: float, days_held: int,
                       params: Optional[Dict[str, Any]], highs=None) -> Optional[str]:
    """止盈 / 时间平仓判断 (与模拟盘 _take_profit_signal_intraday 逐条一致)。None = 不卖。
    params["top_fractal"]=True 时 (管线 exit 模式 / WF 开启止盈, 与各自回测一致) 盈利 > 12% 再检查顶分止盈, 需要 highs。"""
    if params is None or params.get("enabled") is False or not cost or not price:
        return None
    p = {**TAKE_PROFIT_DEFAULTS, **params}
    profit_pct = (price - cost) / cost * 100
    if days_held > int(p.get("max_hold_days", 20)):
        return f"时间平仓({profit_pct:.1f}%)"
    if p.get("trail") is False:
        return None
    if profit_pct >= 8 and high_since_entry > 0:
        dd = (high_since_entry - price) / high_since_entry * 100
        if profit_pct > 30:
            trail = p.get("trail_pct_huge", 10)
        elif profit_pct > 20:
            trail = p.get("trail_pct_large", 8)
        elif profit_pct > 12:
            trail = p.get("trail_pct_medium", 6)
        else:
            trail = p.get("trail_pct_small", 4)
        if dd > trail:
            return f"移动止盈({dd:.1f}%>{trail}%)"
    if p.get("top_fractal") and profit_pct > 12 and highs is not None and top_fractal_hit(list(highs)):
        return f"顶分止盈{profit_pct:.1f}%"
    if profit_pct > 25 and days_held > 12:
        return f"止盈{profit_pct:.1f}%"
    return None
