"""统一数据模型。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# 动作类型：买入(新建仓) / 加仓 / 减仓 / 卖出(清仓) / 申购(基金)
ACTION_BUY = "买入"
ACTION_ADD = "加仓"
ACTION_REDUCE = "减仓"
ACTION_SELL = "卖出"
ACTION_SUBSCRIBE = "申购"  # 基金申购（支付宝/蚂蚁财富实盘）

BUY_ACTIONS = (ACTION_BUY, ACTION_ADD, ACTION_SUBSCRIBE)


class Expert(BaseModel):
    """社区中的实盘/组合高手。"""

    source: str = Field(description="来源：xueqiu / eastmoney / tonghuashun / alipay")
    asset_type: str = Field(default="stock", description="资产类型：stock=股票 / fund=基金（支付宝实盘）")
    channel: str = Field(default="", description="频道：雪球热门组合 / 东财实盘榜 / 东财高手操作 / 同花顺模拟炒股 / 蚂蚁财富实盘")
    expert_id: str = Field(default="", description="组合号或选手 ID")
    name: str = Field(default="", description="组合名或昵称")
    owner: str = Field(default="", description="操作者昵称")
    rank_label: str = Field(default="", description="所属榜单")
    followers: int = 0
    return_rate: float | None = Field(default=None, description="榜单收益率(%)")
    total_return: float | None = None
    market_type: str = Field(
        default="sim",
        description="实盘/模拟盘打标：live=实盘 sim=模拟（推断规则见 core.utils.infer_market_type）",
    )
    extra: dict[str, Any] = Field(default_factory=dict)


class TradeRecord(BaseModel):
    """一条调仓/成交明细。"""

    source: str
    asset_type: str = Field(default="stock", description="资产类型：stock=股票 / fund=基金")
    channel: str = ""
    expert_id: str = ""
    expert_name: str = ""
    owner: str = ""
    stock_code: str = ""
    stock_name: str = ""
    market: str = ""
    action: str = ""
    price: float | None = None
    weight_before: float | None = Field(default=None, description="调仓前仓位(%)")
    weight_after: float | None = Field(default=None, description="调仓后仓位(%)")
    weight_label: str | None = Field(
        default=None, description="平台原始仓位文字(东财实盘 rtV3 positionRatio, 如'1成以下'/'9成以上'); "
        "买卖都是 成交金额/组合总资产, 按'成'向下取整分档 [2026-09-25 股数账本核对]")
    amount: float | None = Field(default=None, description="成交金额（元）；社区高手操作通常无此字段")
    volume: float | None = Field(default=None, description="成交数量（股/份）；同花顺交易表可拿到，东财/雪球部分缺失")
    return_rate: float | None = Field(
        default=None, description="操作所属高手的累计收益率(%)；东财=zsyl(总收益) / 雪球=total_gain / 同花顺=总盈利率"
    )
    day_return_rate: float | None = Field(
        default=None, description="操作所属高手的当日收益率(%)；东财=syl_dr / 雪球=daily_gain"
    )
    trade_time: str = ""
    trade_date: str = ""
    market_type: str = Field(
        default="sim",
        description="实盘/模拟盘打标：live=实盘 sim=模拟（推断规则见 core.utils.infer_market_type）",
    )
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_buy(self) -> bool:
        return self.action in BUY_ACTIONS


class StockAggregate(BaseModel):
    """按股票聚合的买入热度。"""

    stock_code: str
    asset_type: str = Field(default="stock", description="资产类型：stock=股票 / fund=基金")
    stock_name: str = ""
    buy_experts: int = 0
    buy_records: int = 0
    sources: list[str] = Field(default_factory=list)
    avg_price: float | None = None
    amount: float | None = Field(default=None, description="合计买入金额（元），按有成交金额的渠道累加")
    latest_time: str = ""
    net_amount: float | None = Field(default=None, description="龙虎榜净买入额（元）")
    note: str = Field(default="", description="补充说明（如龙虎榜上榜原因）")
