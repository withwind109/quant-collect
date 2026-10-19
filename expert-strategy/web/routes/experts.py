"""高手评估页 API（原「社区高手」页重构：分渠道数据爬取 + 高手能力评估）。

GET  /api/expert-crawl/dates               可用交易日
GET  /api/expert-crawl/channels            分渠道数据状态（各源凭证/最新日/条数）
GET  /api/expert-crawl/status              任务状态（crawl/history/bulk_history/expert_eval/…）
GET  /api/expert-crawl/log                 任务日志尾部
POST /api/expert-crawl/run                 触发爬取（子进程，东财 Cookie 认证）
POST /api/expert-crawl/history             单组合调仓历史回补
POST /api/expert-crawl/bulk-history        批量回补（强制 curl 轻量后端）
GET  /api/expert-crawl/history-file        单组合已回补的历史记录
GET  /api/expert-crawl/watchlist           关注列表
POST /api/expert-crawl/watchlist           加关注
DELETE /api/expert-crawl/watchlist/{expert_id}  取消关注
GET  /api/expert-crawl/sources             数据源选项（前端 chips）
GET  /api/expert-crawl/evaluation          最新一次能力评估结果（rows+meta+bench）
POST /api/expert-crawl/evaluation/run      触发能力评估（后台子进程）
GET  /api/expert-crawl/evaluation/detail   单高手明细（净值曲线+成交+指标，现算）
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from paths import CRAWL_DATA_DIR, DATA_DIR, DB_PATH
from web import jobs

router = APIRouter(prefix="/api/expert-crawl", tags=["expert-crawl"])

SOURCE_CHOICES = ["eastmoney", "tonghuashun", "xueqiu", "alipay"]
SOURCE_LABELS = {"eastmoney": "东财", "xueqiu": "雪球",
                 "tonghuashun": "同花顺", "alipay": "支付宝"}

#: 全部后台任务名（状态文件 logs/job_<name>.json）；策略页的回测/净值更新/体检也走这套
_JOB_NAMES = ("crawl", "history", "bulk_history", "expert_eval", "backtest", "paper_update", "health")


# ── 数据读取 ─────────────────────────────────────────────────────────────────

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


# ── 端点 ─────────────────────────────────────────────────────────────────────

@router.get("/dates")
async def dates() -> Dict[str, Any]:
    ds = _dates()
    return {"dates": ds, "latest": ds[0] if ds else None}


@router.get("/channels")
async def channels() -> Dict[str, Any]:
    """分渠道数据状态：凭证 / 最新交易日 / 高手数 / 成交数 / 有数据天数。"""
    db = _db()

    def _group(sql: str) -> Dict[str, dict]:
        return {(r.get("source") or ""): r for r in db.q(sql)}

    trade_st = _group("""SELECT source, COUNT(*) AS n, COUNT(DISTINCT trade_date) AS days,
                                MAX(trade_date) AS last_date FROM crawl_trade GROUP BY source""")
    expert_st = _group("SELECT source, COUNT(*) AS n FROM crawl_expert GROUP BY source")
    buy_st = _group("SELECT source, COUNT(*) AS n FROM crawl_expert_buy GROUP BY source")

    cred: Dict[str, str] = {}
    try:
        from settings import DEFAULT_CONFIG, load_settings
        s = load_settings(DEFAULT_CONFIG)
        cred = {"eastmoney": "cookie" if s["eastmoney"].get("cookie") else "none",
                "xueqiu": "cookie" if s["xueqiu"].get("cookie") else "none",
                "tonghuashun": "cookie" if s["tonghuashun"].get("cookie") else "none",
                "alipay": "endpoint" if s["alipay"].get("rank_url") else "none"}
    except Exception:  # noqa: BLE001  设置读取失败不阻塞页面
        pass

    out = []
    for src in SOURCE_CHOICES:
        t = trade_st.get(src) or {}
        out.append({
            "source": src, "label": SOURCE_LABELS[src],
            "credential": cred.get(src, "none"),
            "last_date": t.get("last_date"),
            "days": t.get("days") or 0,
            "trades": t.get("n") or 0,
            "experts": (expert_st.get(src) or {}).get("n") or 0,
            "buys": (buy_st.get(src) or {}).get("n") or 0,
            "has_data": bool(t),
        })
    ds = _dates()
    return {"channels": out, "choices": SOURCE_CHOICES, "labels": SOURCE_LABELS,
            "latest_date": ds[0] if ds else None, "dates_total": len(ds)}


@router.get("/status")
async def status() -> Dict[str, Any]:
    """全部后台任务状态（crawl/history/bulk_history/expert_eval/backtest/paper_update/health）。"""
    return {name: jobs.job_view(name, include_log=False) for name in _JOB_NAMES}


@router.get("/log")
async def log(job: str = Query("crawl", pattern="^(crawl|history|bulk_history|expert_eval|backtest|paper_update|health)$")) -> Dict[str, Any]:
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


# ── 关注管理（评估结果一键关注） ─────────────────────────────────────────────

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


# ── 高手能力评估 ─────────────────────────────────────────────────────────────

class EvalBody(BaseModel):
    env: str = "sim"
    start: str = ""
    end: str = ""
    min_sells: int = 3
    min_days: int = 20


@router.get("/evaluation")
async def evaluation() -> Dict[str, Any]:
    """最新一次评估结果（rows 按综合评分/夏普排序，前端自行筛选）。"""
    from strategy.expert_eval import EVAL_LATEST
    if not EVAL_LATEST.exists():
        return {"ok": False, "msg": "还没有评估结果——先点「开始评估」", "rows": [], "meta": {}}
    try:
        d = json.loads(EVAL_LATEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": False, "msg": "评估结果文件损坏，请重新评估", "rows": [], "meta": {}}
    d["ok"] = True
    return d


@router.post("/evaluation/run")
async def evaluation_run(body: EvalBody) -> Dict[str, Any]:
    env = body.env if body.env in ("sim", "live") else "sim"
    argv = ["-m", "strategy.expert_eval", "--env", env,
            "--min-sells", str(max(1, body.min_sells)), "--min-days", str(max(1, body.min_days))]
    if body.start.strip():
        argv += ["--start", body.start.strip()]
    if body.end.strip():
        argv += ["--end", body.end.strip()]
    try:
        jobs.start_job("expert_eval", argv)
    except jobs.JobBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "msg": "评估已启动", "env": env}


@router.get("/evaluation/detail")
async def evaluation_detail(expert_id: str = Query(""), env: str = Query("sim")) -> Dict[str, Any]:
    if not expert_id.strip():
        raise HTTPException(400, "缺少高手ID")
    from strategy import expert_eval as EE
    d = EE.expert_detail(expert_id.strip(), env=env if env in ("sim", "live") else "sim")
    if not d.get("ok"):
        raise HTTPException(404, d.get("msg") or "无法评估该高手")
    return d
