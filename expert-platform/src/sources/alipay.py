"""支付宝 / 蚂蚁财富「理财盘友圈」实盘榜（基金维度）。

⚠️ 重要：支付宝/蚂蚁财富**没有公开 API**。实盘榜（基金持仓 + 申购/转换记录）位于支付宝 App
内，后端为支付宝网关签名，普通抓包无法直接复用。因此本源是「抓包就绪」骨架：

  1. 用电脑浏览器登录支付宝 / 蚂蚁财富，进入「理财盘友圈 → 实盘榜」；
  2. 打开开发者工具(Network)，找到返回高手榜的 JSON 请求，把其 URL 填到
     config.yaml 的 `alipay.rank_url`（或 .env 的 ALIPAY_RANK_URL）；
  3. 再找到「单个高手持仓/操作」的 JSON 请求，URL 中用户 ID 处用 {uid} 占位，
     填到 `alipay.trade_url`（或 ALIPAY_TRADE_URL）；
  4. 把支付宝登录态 Cookie 填到 `alipay.cookie`（或 ALIPAY_COOKIE）。

字段名以本文件顶部 FIELD_MAP 为准——不同版本返回字段可能不同，按你抓到的真实 JSON
微调候选键即可。未配置 rank_url 时，hot_experts 会抛出带上述指引的异常（被 pipeline
捕获并记为该源采集失败，不影响其他源）。

数据语义：支付宝实盘是**基金**，不是 A 股个股；买入 = 申购/转换，故 asset_type="fund"，
action 取「申购」。这与东财/雪球/同花顺的股票维度并列，下游按 asset_type 区分聚合。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from loguru import logger

from core.http import HttpClient
from core.models import (
    ACTION_BUY,
    ACTION_SELL,
    Expert,
    TradeRecord,
)
from core.utils import to_float
from .base import BaseSource

# 候选字段名（按真实返回 JSON 的键顺序尝试，取第一个命中）
RANK_FIELD_MAP = {
    "id": ("uid", "userId", "user_id", "id"),
    "name": ("nickName", "nick", "name", "userName", "realName"),
    "return_rate": ("totalReturnRate", "yieldRate", "returnRate", "rate", "profitRate"),
    "followers": ("fansCount", "followerCount", "fans", "followCount"),
}
TRADE_FIELD_MAP = {
    "fund_code": ("fundCode", "code", "prodCode", "symbol"),
    "fund_name": ("fundName", "name", "prodName", "shortName"),
    "action": ("operateType", "action", "type", "bizType"),
    "time": ("operateTime", "time", "gmtCreate", "date", "tradeTime"),
}


def _pick(obj: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in obj and obj[k] not in (None, ""):
            return obj[k]
    return default


class AlipaySource(BaseSource):
    name = "alipay"

    def __init__(
        self,
        http: HttpClient,
        rank_url: str | None = None,
        trade_url: str | None = None,
        cookie: str | None = None,
    ) -> None:
        super().__init__(http)
        self.rank_url = rank_url or ""
        self.trade_url = trade_url or ""
        self.cookie = cookie or ""

    @property
    def configured(self) -> bool:
        return bool(self.rank_url)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json, text/plain, */*"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def _require_config(self) -> None:
        if not self.configured:
            raise RuntimeError(
                "支付宝实盘端点未配置：请在 config.yaml 的 alipay.rank_url（或 .env 的 "
                "ALIPAY_RANK_URL）填入你从支付宝登录态抓包得到的高手榜 JSON 接口 URL；"
                "详见 src/sources/alipay.py 顶部说明。"
            )

    # ── 榜单 ──────────────────────────────────────────────
    def hot_experts(self, limit: int = 50) -> list[Expert]:
        self._require_config()
        data = self.http.get_json(self.rank_url, headers=self._headers())
        items = self._as_list(data)
        experts: list[Expert] = []
        for it in items:
            o = it if isinstance(it, dict) else {}
            exp_id = str(_pick(o, *RANK_FIELD_MAP["id"], default="") or "")
            if not exp_id:
                continue
            experts.append(
                Expert(
                    source=self.name,
                    asset_type="fund",
                    channel="蚂蚁财富实盘",
                    expert_id=exp_id,
                    name=str(_pick(o, *RANK_FIELD_MAP["name"], default=exp_id)),
                    owner=str(_pick(o, *RANK_FIELD_MAP["name"], default=exp_id)),
                    rank_label="理财盘友圈实盘榜",
                    followers=int(to_float(_pick(o, *RANK_FIELD_MAP["followers"], default=0)) or 0),
                    return_rate=to_float(_pick(o, *RANK_FIELD_MAP["return_rate"])),
                    market_type="live",  # 蚂蚁财富即实盘 [2026-09-23 打标]
                    extra={"raw": o},
                )
            )
            if len(experts) >= limit:
                break
        logger.info(f"[支付宝] 实盘榜 解析得 {len(experts)} 位高手")
        return experts

    # ── 交易明细 ──────────────────────────────────────────
    def trades(self, experts: list[Expert], trade_date: date) -> list[TradeRecord]:
        if not self.trade_url:
            logger.warning("[支付宝] 未配置 trade_url（高手持仓明细接口），跳过逐笔申购；榜单数据仍会输出。")
            return []
        if not self.cookie:
            logger.warning("[支付宝] 未配置 ALIPAY_COOKIE（需登录态），跳过持仓明细。")
            return []

        target = trade_date.strftime("%Y-%m-%d")
        records: list[TradeRecord] = []
        for exp in experts:
            try:
                url = self.trade_url.replace("{uid}", exp.expert_id)
                data = self.http.get_json(url, headers=self._headers())
            except Exception as exc:
                logger.warning(f"[支付宝] 高手 {exp.expert_id} 持仓抓取失败：{exc}")
                continue
            for it in self._as_list(data):
                o = it if isinstance(it, dict) else {}
                code = str(_pick(o, *TRADE_FIELD_MAP["fund_code"], default="") or "")
                if not code:
                    continue
                action_raw = str(_pick(o, *TRADE_FIELD_MAP["action"], default="申购") or "申购")
                trade_time = str(_pick(o, *TRADE_FIELD_MAP["time"], default="") or "")
                if trade_time and not trade_time.startswith(target):
                    continue
                records.append(
                    TradeRecord(
                        source=self.name,
                        asset_type="fund",
                        channel="蚂蚁财富实盘",
                        expert_id=exp.expert_id,
                        expert_name=exp.name,
                        owner=exp.owner,
                        stock_code=code,
                        stock_name=str(_pick(o, *TRADE_FIELD_MAP["fund_name"], default="")),
                        market="",
                        action=ACTION_BUY if ("申" in action_raw or "买" in action_raw or "转" in action_raw) else ACTION_SELL,
                        return_rate=exp.return_rate,
                        trade_time=trade_time,
                        trade_date=trade_time[:10] if trade_time else target,
                        market_type="live",  # 蚂蚁财富即实盘 [2026-09-23 打标]
                        raw={"action_raw": action_raw},
                    )
                )
        logger.info(f"[支付宝] 当日({target})申购记录 {len(records)} 条")
        return records

    @staticmethod
    def _as_list(data: Any) -> list:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("list", "data", "items", "result", "records"):
                if isinstance(data.get(key), list):
                    return data[key]
        return []
