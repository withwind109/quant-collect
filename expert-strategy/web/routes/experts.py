"""社区高手页 API（对齐源项目 routes_experts_crawl.py 的功能面）。

GET  /api/expert-crawl/dates          可用交易日（DB ∪ 文件目录）
GET  /api/expert-crawl/data           指定/最新日期爬取数据
GET  /api/expert-crawl/status         任务状态（crawl/history/bulk_history）
GET  /api/expert-crawl/log            任务日志尾部
POST /api/expert-crawl/run            触发爬取（子进程，东财 Cookie 认证）
POST /api/expert-crawl/history        单组合调仓历史回补
POST /api/expert-crawl/bulk-history   批量回补（强制 curl 轻量后端）
GET  /api/expert-crawl/history-file   单组合已回补的历史记录
GET  /api/expert-crawl/watchlist      关注列表
POST /api/expert-crawl/watchlist      加关注
DELETE /api/expert-crawl/watchlist/{expert_id}  取消关注
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from paths import CRAWL_DATA_DIR, DATA_DIR, DB_PATH
from web import jobs

router = APIRouter(prefix="/api/expert-crawl", tags=["expert-crawl"])

SOURCE_CHOICES = ["eastmoney", "tonghuashun", "xueqiu", "alipay"]
SOURCE_LABELS = {"eastmoney": "东财", "xueqiu": "雪球",
                 "tonghuashun": "同花顺", "alipay": "支付宝"}

#: 全部后台任务名（状态文件 logs/job_<name>.json）；策略页的回测/净值更新/体检也走这套
_JOB_NAMES = ("crawl", "history", "bulk_history", "backtest", "paper_update", "health")


# ── 数据读取（SQLite 优先，回退 data/crawl/<日期>/*.json） ─────────────────

def _db():
    from core import db
    db.ensure_schema(DB_PATH)
    db.ensure_strategy_schema(DB_PATH)
    return db


def _dates() -> List[str]:
    db = _db()
    dates = set(db.list_dates(DB_PATH))
    if CRAWL_DATA_DIR.is_dir():
        for p in CRAWL_DATA_DIR.iterdir():
            if p.is_dir() and (p / "expert_buys.json").exists():
                dates.add(p.name)
    return sorted(dates, reverse=True)


def _load_json(fp: Path):
    if not fp.exists():
        return []
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _read_day(date: str) -> Optional[Dict[str, Any]]:
    data = _db().read_day(date, DB_PATH)
    if data is not None:
        return data
    day_dir = CRAWL_DATA_DIR / date
    if not (day_dir / "expert_buys.json").exists():
        return None
    return {
        "experts": _load_json(day_dir / "experts.json"),
        "expert_buys": _load_json(day_dir / "expert_buys.json"),
        "buys": _load_json(day_dir / "trades.json"),
        "buy_aggregated": _load_json(day_dir / "buy_aggregated.json"),
        "stock_rank": _load_json(day_dir / "stock_rank.json"),
        "signals": _load_json(day_dir / "signals.json"),
        "meta": _load_json(day_dir / "meta.json") or {},
    }


# ── 端点 ─────────────────────────────────────────────────────────────────

@router.get("/dates")
async def dates() -> Dict[str, Any]:
    ds = _dates()
    return {"dates": ds, "latest": ds[0] if ds else None}


@router.get("/data")
async def data(date: str = Query("", description="YYYY-MM-DD，空=最新日期")) -> Dict[str, Any]:
    ds = _dates()
    if not ds:
        return {"date": None, "msg": "还没有数据，先触发一次爬取"}
    target = date if date in ds else (date or ds[0])
    d = _read_day(target)
    if d is None:
        raise HTTPException(404, f"{target} 没有可展示的数据")
    meta = d.get("meta") or {}
    n_live = sum(1 for e in d.get("expert_buys") or [] if e.get("market_type") == "live")
    return {
        "date": target,
        "dates_total": len(ds),
        "summary": {
            "experts": len(d.get("experts") or []),
            "expert_buys": len(d.get("expert_buys") or []),
            "trades": len(d.get("buys") or []),
            "stocks": len(d.get("buy_aggregated") or []),
            "live_experts": n_live,
            "coverage": len(meta.get("coverage") or []),
            "date_mode": meta.get("date_mode") or "—",
            "crawled_at": (meta.get("crawled_at") or "—")[:19],
        },
        "coverage": meta.get("coverage") or [],
        "experts": d.get("experts") or [],
        "expert_buys": d.get("expert_buys") or [],
        "trades": d.get("buys") or [],
        "buy_aggregated": d.get("buy_aggregated") or [],
        "stock_rank": d.get("stock_rank") or [],
        "signals": d.get("signals") or [],
    }


@router.get("/status")
async def status() -> Dict[str, Any]:
    """全部后台任务状态（crawl/history/bulk_history/backtest/paper_update/health）。"""
    return {name: jobs.job_view(name, include_log=False) for name in _JOB_NAMES}


@router.get("/log")
async def log(job: str = Query("crawl", pattern="^(crawl|history|bulk_history|backtest|paper_update|health)$")) -> Dict[str, Any]:
    return {"job": job, "log_tail": jobs.read_log_tail(job)}


class RunBody(BaseModel):
    sources: List[str] = ["eastmoney", "tonghuashun"]
    top: int = 50
    date: str = ""


@router.post("/run")
async def run(body: RunBody) -> Dict[str, Any]:
    names = [s for s in body.sources if s in SOURCE_CHOICES]
    if not names:
        raise HTTPException(400, f"至少选一个有效数据源（{SOURCE_CHOICES}）")
    argv = ["-m", "cli", "crawl", "--sources", ",".join(names), "--top", str(body.top)]
    if body.date:
        argv += ["--date", body.date]
    try:
        jobs.start_job("crawl", argv)
    except jobs.JobBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "msg": "爬取已启动", "args": names}


class HistoryBody(BaseModel):
    zjzh: str
    since: str = ""


@router.post("/history")
async def history(body: HistoryBody) -> Dict[str, Any]:
    zjzh = body.zjzh.strip()
    if not zjzh:
        raise HTTPException(400, "请输入组合号")
    argv = ["-m", "cli", "history", "--zjzh", zjzh]
    if body.since.strip():
        argv += ["--since", body.since.strip()]
    try:
        jobs.start_job("history", argv)
    except jobs.JobBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "msg": "回补已启动", "zjzh": zjzh}


class BulkBody(BaseModel):
    limit: int = 0
    since: str = "2025-12-01"


@router.post("/bulk-history")
async def bulk_history(body: BulkBody) -> Dict[str, Any]:
    argv = ["-m", "cli", "bulk-history", "--limit", str(body.limit), "--since", body.since]
    try:
        jobs.start_job("bulk_history", argv)
    except jobs.JobBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "msg": "批量回补已启动", "limit": body.limit}


@router.get("/history-file")
async def history_file(zjzh: str = Query("")) -> Dict[str, Any]:
    if not zjzh.strip():
        raise HTTPException(400, "缺少组合号")
    fp = DATA_DIR / "history" / f"{zjzh.strip()}.json"
    if not fp.exists():
        return {"zjzh": zjzh, "exists": False, "records": []}
    try:
        d = json.loads(fp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"zjzh": zjzh, "exists": False, "records": []}
    recs = d.get("records") or []
    return {"zjzh": zjzh, "exists": True, "name": d.get("name") or zjzh,
            "fetched_at": d.get("fetched_at"), "n": len(recs),
            "records": [{"trade_date": r.get("trade_date"),
                         "trade_time": (r.get("trade_time") or "")[-8:],
                         "stock_code": r.get("stock_code"), "stock_name": r.get("stock_name") or "",
                         "action": r.get("action"), "price": r.get("price"),
                         "weight_before": r.get("weight_before"),
                         "weight_after": r.get("weight_after")} for r in recs[:500]]}


# ── 关注管理 ─────────────────────────────────────────────────────────────

@router.get("/watchlist")
async def watchlist() -> Dict[str, Any]:
    from strategy import watchlist as W
    rows = W.list_watch("sim")
    return {"rows": [{"expert_id": r["expert_id"], "name": r.get("name") or "",
                      "created_at": r.get("created_at") or ""} for r in rows]}


class WatchBody(BaseModel):
    expert_id: str
    name: str = ""


@router.post("/watchlist")
async def watch_add(body: WatchBody) -> Dict[str, Any]:
    from strategy import watchlist as W
    eid = body.expert_id.strip()
    if not eid:
        raise HTTPException(400, "缺少高手ID")
    W.add(eid, body.name.strip())
    return {"ok": True, "expert_id": eid}


@router.delete("/watchlist/{expert_id}")
async def watch_remove(expert_id: str) -> Dict[str, Any]:
    from strategy import watchlist as W
    W.remove(expert_id)
    return {"ok": True, "expert_id": expert_id}


@router.get("/sources")
async def sources() -> Dict[str, Any]:
    return {"choices": SOURCE_CHOICES, "labels": SOURCE_LABELS}
