"""统一交易成本 (2026-09-26 用户决策 D2: 按真实费率, 回测与模拟盘统一)。

    佣金   万 2.5, 双边, 每笔最低 5 元
    印花税 0.05%, 仅卖出
    滑点   0.2%, 买入按成交价上浮、卖出下浮

所有模拟盘执行 (scripts/dashboard/paper_trading.py / paper_automation.py) 与回测引擎
(follow_backtest_v2 / expert_follow.engine / stock_walkforward / backtest_pipeline) 都从这里取, 不再各写一份。
"""
from __future__ import annotations

COMMISSION = 0.00025
MIN_COMMISSION = 5.0
STAMP_TAX_SELL = 0.0005
SLIPPAGE = 0.002

# 按比例近似用 (不计最低佣金): 买入 / 卖出 单边成本率 (不含滑点)
BUY_FEE_RATE = COMMISSION
SELL_FEE_RATE = COMMISSION + STAMP_TAX_SELL


def commission(amount: float) -> float:
    return max(MIN_COMMISSION, abs(amount) * COMMISSION) if amount else 0.0


def buy_fill(px: float) -> float:
    """买入成交价 (含滑点)。"""
    return px * (1 + SLIPPAGE)


def sell_fill(px: float) -> float:
    """卖出成交价 (含滑点)。"""
    return px * (1 - SLIPPAGE)


def buy_qty(budget: float, px: float, lot: int = 100) -> int:
    """预算内能买的股数 (整手, 已留出佣金)。"""
    fill = buy_fill(px)
    q = int(budget / (fill * (1 + COMMISSION)) / lot) * lot
    while q > 0 and q * fill + commission(q * fill) > budget:
        q -= lot
    return max(q, 0)


def buy_cost(qty: float, px: float) -> float:
    """买入总花费 = 成交额 + 佣金。"""
    amt = qty * buy_fill(px)
    return amt + commission(amt)


def sell_proceeds(qty: float, px: float) -> float:
    """卖出净得 = 成交额 − 佣金 − 印花税。"""
    amt = qty * sell_fill(px)
    return amt - commission(amt) - amt * STAMP_TAX_SELL


def sell_fee(amount: float) -> float:
    """卖出成交额对应的佣金 + 印花税。"""
    return commission(amount) + abs(amount) * STAMP_TAX_SELL


def round_trip_pct() -> float:
    """一买一卖的比例成本 (%, 不计最低佣金): 2×滑点 + 2×佣金 + 印花税。"""
    return (2 * SLIPPAGE + 2 * COMMISSION + STAMP_TAX_SELL) * 100
