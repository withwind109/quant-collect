"""结果落盘：JSON / CSV / Excel。"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from loguru import logger

from .models import Expert, StockAggregate, TradeRecord

TRADE_COLUMNS = [
    "source", "asset_type", "channel", "expert_id", "expert_name", "owner", "stock_code",
    "stock_name", "market", "action", "price", "volume", "amount", "weight_before",
    "weight_after", "return_rate", "day_return_rate", "trade_time", "trade_date",
    "market_type", "weight_label",
]
EXPERT_COLUMNS = ["source", "asset_type", "channel", "expert_id", "name", "owner", "rank_label",
                  "followers", "return_rate", "total_return", "market_type"]
AGG_COLUMNS = ["stock_code", "asset_type", "stock_name", "buy_experts", "buy_records", "avg_price",
               "amount", "net_amount", "sources", "note", "latest_time"]
EXPERT_BUY_COLUMNS = ["source", "asset_type", "expert_id", "expert_name", "owner", "rank_label",
                      "market_type", "return_rate", "day_return_rate", "followers", "stock_code", "stock_name",
                      "action", "price", "volume", "amount", "weight_before", "weight_after",
                      "trade_time"]


def _df(records: list, columns: list[str]) -> pd.DataFrame:
    rows = [r.model_dump() if hasattr(r, "model_dump") else r for r in records]
    df = pd.DataFrame(rows)
    for col in columns:
        if col not in df.columns:
            df[col] = None
    return df[columns]


def write_json(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    logger.info(f"已写入 {path}")
    return path


def write_csv(path: Path, records: list, columns: list[str]) -> Path:
    _df(records, columns).to_csv(path, index=False, encoding="utf-8-sig")
    logger.info(f"已写入 {path}")
    return path


def write_excel(path: Path, trades: list[TradeRecord], experts: list[Expert],
                aggregates: list[StockAggregate]) -> Path:
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        _df(trades, TRADE_COLUMNS).to_excel(writer, sheet_name="成交明细", index=False)
        _df(aggregates, AGG_COLUMNS).to_excel(writer, sheet_name="个股买入热度", index=False)
        _df(experts, EXPERT_COLUMNS).to_excel(writer, sheet_name="热门高手", index=False)
    logger.info(f"已写入 {path}")
    return path
