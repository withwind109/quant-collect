"""单组合调仓历史回补（方案 P1，2026-09-23；2026-10-16 迁移到 expert-platform）。

从东财 ``spo_hldchg_detail`` 匿名翻页拉取某个模拟盘组合的全部调仓记录，
落盘到 ``data/history/<zjzh>.json``（含组合基础信息 + 记录列表 + 元信息）。

与 xcq-platform 版的区别：不再入库 MySQL（history_backfill 的职责由本地 SQLite 侧
按需读取 JSON 完成），路径统一走 paths 模块。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from loguru import logger

from paths import DATA_DIR
from pipeline import build_sources


def run_history(
    settings: dict,
    zjzh: str,
    since: str = "",
    out_dir: Path | None = None,
) -> dict:
    """执行一次单组合历史回补，返回摘要 dict。

    zjzh: 模拟盘组合号（1000… 长号）。实盘组合号（9 开头）不支持——
          spo_* 网关会报"组合号错误"，实盘明细走 rtV3 流水（每日爬取自动累积）。
    since: "YYYY-MM-DD"，只保留该日（含）之后的记录；空 = 全历史。
    """
    client = build_sources(settings, force_http=True)["eastmoney"]
    try:
        records = client.portfolio_history(zjzh, since=since)
        basic = client.portfolio_basic(zjzh)
    finally:
        try:
            if hasattr(client.http, "close"):
                client.http.close()
        except Exception:
            pass

    payload = {
        "zjzh": zjzh,
        "name": str(basic.get("zuheName") or basic.get("name") or ""),
        "owner": str(basic.get("userName") or basic.get("owner") or ""),
        "return_rate": basic.get("zsyl"),
        "basic": basic,
        "since": since or "",
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(records),
        "records": [r.model_dump() for r in records],
    }

    target = (out_dir or DATA_DIR) / "history"
    target.mkdir(parents=True, exist_ok=True)
    fp = target / f"{zjzh}.json"
    fp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    logger.info(f"[history] 已写入 {fp}（{len(records)} 条）")

    return {
        "zjzh": zjzh,
        "name": payload["name"],
        "since": since or "全部",
        "count": len(records),
        "first_date": records[-1].trade_date if records else "",
        "last_date": records[0].trade_date if records else "",
        "output": str(fp),
    }
