"""数据源抽象。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from core.http import Fetcher
from core.models import Expert, StockAggregate, TradeRecord


class BaseSource(ABC):
    """所有社区数据源的统一接口。"""

    #: 数据源标识，与 CLI 的 --sources 参数一致
    name: str = "base"
    #: 是否提供站点自带的聚合买入榜
    supports_stock_rank: bool = False

    def __init__(self, http: Fetcher) -> None:
        self.http = http  # 抓取后端：curl_cffi(HttpClient) 或 Playwright(BrowserClient)
        # 由 pipeline 注入的目标交易日
        self.trade_date: date | None = None

    @abstractmethod
    def hot_experts(self, limit: int = 50) -> list[Expert]:
        """抓取热门高手/席位榜。"""

    @abstractmethod
    def trades(self, experts: list[Expert], trade_date: date) -> list[TradeRecord]:
        """抓取 trade_date 当日的调仓/买入明细。"""

    def hot_buy_stocks(self, limit: int = 50) -> list[StockAggregate]:
        """站点自带的聚合买入榜（仅 supports_stock_rank=True 的源需要实现）。"""
        return []

    def candidate_dates(self) -> dict[date, int]:
        """本数据源最近几个候选交易日 -> 该日的活跃记录数。

        用途：默认日期取 `date.today()` 时，周末/节假日各源都会返回空结果
        （实测 2026-09-12 周六，东财仍能返回 09-11 的 874 条记录，却被日期过滤全部丢弃）。
        pipeline 汇总各源的候选日期后投票选出目标交易日：
        先比「有多少个源在该日有数据」，再比「当日活跃记录总数」，最后取较新的日期。

        为什么不能各源直接取 max(日期)：雪球组合允许非交易日手动调仓，
        实测 09-12（周六）有 1 个组合调仓、09-11（周五）有 24 个，
        若取 max 会选中周六，导致东财/同花顺当日数据被全部过滤为空。
        """
        return {}

    def signals(self) -> list[dict]:
        """可选的补充信号：能拿到「谁在买」但拿不到标的的公开动态。

        例如同花顺模拟炒股首页的「高手动态」只披露 昵称 + 时间 + 买卖方向，
        股票代码被 `zhuikaen` 占位符掩盖。这类数据不进入 expert_buys（会污染主视图），
        而是单独落盘为 signals.json，在前端作为独立卡片呈现。
        """
        return []

    def healthcheck(self) -> tuple[bool, str]:
        """检查凭据与接口可用性。"""
        try:
            experts = self.hot_experts(limit=3)
            return True, f"榜单可用，样例 {len(experts)} 条"
        except Exception as exc:
            return False, f"不可用：{exc}"
