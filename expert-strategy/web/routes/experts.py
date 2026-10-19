"""高手评估页 API（原「社区高手」页重构：分渠道数据爬取 + 高手能力评估）。

GET  /api/expert-crawl/dates               可用交易日
GET  /api/expert-crawl/channels            分渠道数据状态（各源凭证/最新日/条数）
GET  /api/expert-crawl/status              任务状态（crawl/history/bulk_history/expert_eval/…）
GET  /api/expert-crawl/log                 任务日志尾部
POST /api/expert-crawl/run                 触发爬取（子进程，东财 Cookie 认证）
POST /api/expert-crawl/history             单组合调仓历史回补
POST /api/expert-crawl/bulk-history        批量回补（强制 curl 轻量后端）
GET  /api/expert-crawl/history-file        单组合已回补的历史记录
GET  /api/expert-crawl/watchlist           关注列表（本地，market_type=live 与东财同步）
POST /api/expert-crawl/watchlist           加关注（live=代你在东财账号关注）
DELETE /api/expert-crawl/watchlist/{id}    取消关注（live=同步取消东财关注）
POST /api/expert-crawl/watchlist/sync      同步东财账号已关注名单到本地
POST /api/expert-crawl/watchlist/fetch-detail  拉取关注组合最近调仓明细（rtV3→crawl_trade）
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


# ── 关注管理（与东财账号关注关系双向同步） ───────────────────────────────────
#
# 流程：爬榜单 → 在获取的高手里点「关注」→（live 组合代你在东财账号关注，rtV3
# 明细仅对关注者开放）→ 关注列表里「拉取调仓明细」入库 crawl_trade(live) →
# 能力评估(live 环境)即可用；也可「同步东财关注」把东财账号已有关注拉回来。


def _em_source(require_token: bool = True):
    """构造带登录态令牌的东财数据源（关注操作 / rtV3 明细共用）。"""
    from core.broker_account import active_tokens
    tp = active_tokens("eastmoney") or {}
    if require_token and not (tp.get("ctToken") and tp.get("utToken") and tp.get("userId")):
        raise HTTPException(400, "东财未登录——请先到右上角「🏦 账户中心」扫码登录")
    from core.http import HttpClient
    from sources.eastmoney import EastmoneySource
    cookie = ""
    try:
        from settings import DEFAULT_CONFIG, load_settings
        cookie = load_settings(DEFAULT_CONFIG)["eastmoney"].get("cookie") or ""
    except Exception:  # noqa: BLE001
        pass
    return EastmoneySource(
        HttpClient(min_interval=0.8),
        cookie=cookie,
        spzh_ct_token=tp.get("ctToken") or "",
        spzh_ut_token=tp.get("utToken") or "",
        spzh_user_id=tp.get("userId") or "",
        spzh_device_id=tp.get("deviceId") or "",
    )


def _combo_names(eids: List[str]) -> Dict[str, str]:
    """缺名组合的名称补全：config spzh_follow 静态名单 → rtV3 CombinationInfoHandler。"""
    out: Dict[str, str] = {}
    if not eids:
        return out
    try:
        from settings import DEFAULT_CONFIG, load_settings
        for x in load_settings(DEFAULT_CONFIG)["eastmoney"].get("spzh_follow") or []:
            if str(x.get("expert_id") or "") in eids and x.get("name"):
                out[str(x["expert_id"])] = str(x["name"])
    except Exception:  # noqa: BLE001
        pass
    missing = [e for e in eids if e not in out]
    if missing:
        try:
            src = _em_source()
            for eid in missing:
                info = src.combination_info(eid)
                if info.get("zuheName"):
                    out[eid] = str(info["zuheName"])
        except Exception:  # noqa: BLE001  未登录/接口隐藏都只影响名字展示
            pass
    return out


def _watch_rows(market_type: str = "live") -> List[Dict[str, Any]]:
    """关注列表行（本地 DB 为准，名称用 config/东财组合信息补全）。"""
    from strategy import watchlist as W
    rows = [{"expert_id": r["expert_id"], "name": r.get("name") or "",
             "note": r.get("note") or "", "created_at": r.get("created_at") or "",
             "market_type": market_type}
            for r in W.list_watch(market_type)]
    missing = [r["expert_id"] for r in rows if not r["name"]]
    if missing:
        names = _combo_names(missing)
        for r in rows:
            if not r["name"] and names.get(r["expert_id"]):
                r["name"] = names[r["expert_id"]]
    return rows


def _persist_live_trades(records) -> int:
    """rtV3 明细落库 crawl_trade（market_type=live，按 row_sig 幂等去重）。"""
    from core import db as DB
    n = 0
    for r in records:
        td = r.trade_date or ""
        if not td:
            continue
        sig = DB._sig(td, r.source, r.expert_id, r.stock_code, r.action, r.trade_time,
                      r.price, r.weight_before, r.weight_after)
        n += DB.qexec(
            "INSERT OR IGNORE INTO crawl_trade (trade_date, source, asset_type, channel, expert_id, "
            "expert_name, owner, stock_code, stock_name, market, action, price, volume, amount, "
            "weight_before, weight_after, weight_label, return_rate, day_return_rate, trade_time, "
            "raw_json, market_type, row_sig) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (td, r.source, r.asset_type, r.channel, r.expert_id, r.expert_name, r.owner,
             r.stock_code, r.stock_name, r.market, r.action, r.price, r.volume, r.amount,
             r.weight_before, r.weight_after, r.weight_label, r.return_rate, r.day_return_rate,
             r.trade_time, DB._j(r.raw), "live", sig))
    return n


@router.get("/watchlist")
async def watchlist() -> Dict[str, Any]:
    return {"rows": _watch_rows("live") + _watch_rows("sim")}


class WatchBody(BaseModel):
    expert_id: str
    name: str = ""
    market_type: str = "sim"   # live=同步关注东财组合（rtV3 明细仅对关注者开放）


@router.post("/watchlist")
async def watch_add(body: WatchBody) -> Dict[str, Any]:
    from strategy import watchlist as W
    eid = body.expert_id.strip()
    if not eid:
        raise HTTPException(400, "缺少高手ID")
    mt = "live" if body.market_type == "live" else "sim"
    em = {"ok": True, "msg": ""}
    if mt == "live":
        try:
            ok = _em_source().follow(eid)
            em = {"ok": ok, "msg": "已同步关注东财组合" if ok else "东财关注失败（可能达关注上限或登录态失效）"}
        except HTTPException:
            em = {"ok": False, "msg": "东财未登录，仅记录本地关注"}
        except Exception as exc:  # noqa: BLE001
            em = {"ok": False, "msg": f"东财关注失败: {exc}"}
    W.add(eid, body.name.strip(), market_type=mt,
          note="东财同步" if mt == "live" else "")
    return {"ok": True, "expert_id": eid, "market_type": mt, "em": em}


@router.delete("/watchlist/{expert_id}")
async def watch_remove(expert_id: str, market_type: str = Query("sim")) -> Dict[str, Any]:
    from strategy import watchlist as W
    eid = expert_id.strip()
    mt = "live" if market_type == "live" else "sim"
    em = {"ok": True, "msg": ""}
    if mt == "live":
        try:
            ok = _em_source().unfollow(eid)
            em = {"ok": ok, "msg": "已在东财账号取消关注" if ok else "东财取消关注失败（本地已移除）"}
        except Exception as exc:  # noqa: BLE001
            em = {"ok": False, "msg": f"东财取消关注失败: {exc}（本地已移除）"}
    W.remove(eid, market_type=mt)
    return {"ok": True, "expert_id": eid, "em": em}


@router.post("/watchlist/sync")
async def watch_sync() -> Dict[str, Any]:
    """以东财账号为准同步关注列表：拉 FollowCombinationQueryHandler 的关注名单，
    合并进本地 watchlist（market_type=live）。"""
    src = _em_source()
    ids = src.followed_ids()
    from strategy import watchlist as W
    existing = {r["expert_id"] for r in W.list_watch("live")}
    added = 0
    # 名字补全：新增的 + 已有但缺名的（config 静态名单 → 东财组合信息接口）
    need_name = [c for c in ids if c not in existing]
    from core.db import qexec
    for r in W.list_watch("live"):
        if not (r.get("name") or "").strip() and r["expert_id"] not in need_name:
            need_name.append(r["expert_id"])
    names = _combo_names(need_name)
    for cid in ids:
        if cid not in existing:
            W.add(cid, names.get(cid, ""), market_type="live", note="东财同步")
            added += 1
    for cid, nm in names.items():   # 缺名的老条目也回填
        qexec("UPDATE xcq_expert_watchlist SET name=? WHERE expert_id=? AND market_type='live'",
              (nm, cid))
    return {"ok": True, "em_total": len(ids), "added": added, "rows": _watch_rows("live")}


class FetchDetailBody(BaseModel):
    expert_id: str = ""   # 空 = 拉取全部关注组合


@router.post("/watchlist/fetch-detail")
async def watch_fetch_detail(body: FetchDetailBody) -> Dict[str, Any]:
    """拉取关注组合的最近调仓明细（rtV3，最近 40 条/组合），入库 crawl_trade(live)。"""
    wl = _watch_rows("live")
    if body.expert_id.strip():
        eids = [body.expert_id.strip()]
    else:
        eids = [r["expert_id"] for r in wl]
    if not eids:
        return {"ok": True, "msg": "关注列表为空", "n": 0, "rows": []}
    src = _em_source()
    from core.models import Expert
    experts = [Expert(source="eastmoney", expert_id=eid,
                      name=next((r["name"] for r in wl if r["expert_id"] == eid), eid))
               for eid in eids]
    records = src._spzh_trades(experts, target_date="")  # 不按日过滤 = 最近 40 条全量
    n = _persist_live_trades(records)
    rows = [{"expert_id": r.expert_id, "expert_name": r.expert_name,
             "trade_date": r.trade_date, "trade_time": r.trade_time,
             "stock_code": r.stock_code, "stock_name": r.stock_name,
             "action": r.action, "price": r.price,
             "weight_after": r.weight_after, "weight_label": r.weight_label or ""}
            for r in records]
    return {"ok": True, "n": n, "total": len(rows), "rows": rows}


@router.get("/sources")
async def sources() -> Dict[str, Any]:
    return {"choices": SOURCE_CHOICES, "labels": SOURCE_LABELS}


# ── 实盘高手榜（爬取结果展示：累计收益 + 最近操作 + 活跃度） ────────────────

@router.get("/live-experts")
async def live_experts(board: str = Query("total"), limit: int = Query(50, le=200)) -> Dict[str, Any]:
    """最新交易日的实盘高手榜（crawl_expert, market_type=live）。

    board=total 按累计收益排序（总榜条目），board=day 按当日收益排序（日榜条目）；
    每行带最近操作摘要（来自 crawl_trade 实盘流水——只有已关注组合才有 rtV3 明细，
    未关注组合「无明细」不代表不活跃，只是拿不到）。
    """
    from core.db import q, qone
    _db()
    latest = qone("SELECT MAX(trade_date) AS d FROM crawl_expert WHERE market_type='live'")
    latest = (latest or {}).get("d")
    if not latest:
        return {"ok": True, "latest_date": None, "rows": []}
    raw = q("SELECT expert_id, name, owner, followers, return_rate, extra_json "
            "FROM crawl_expert WHERE trade_date=? AND market_type='live'", (latest,))
    out = []
    for r in raw:
        extra = {}
        try:
            extra = json.loads(r.get("extra_json") or "{}")
        except (TypeError, ValueError):
            pass
        # ⚠️ 列里可能存的是 TEXT 形态数字（旧数据/手工回填），统一转 float 再排序
        def _num(v):
            try:
                return float(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None
        try:
            fol = int(float(r.get("followers"))) if r.get("followers") not in (None, "") else 0
        except (TypeError, ValueError):
            fol = 0
        out.append({
            "expert_id": r["expert_id"], "name": r.get("name") or "",
            "owner": r.get("owner") or "", "followers": fol,
            "return_rate": _num(r.get("return_rate")),
            "daily_gain": _num(extra.get("daily_gain")),
            "rank_label": extra.get("rate_title") or "",
        })
    if board == "day":
        rows = sorted([x for x in out if x["daily_gain"] is not None],
                      key=lambda x: x["daily_gain"], reverse=True)
    else:
        rows = sorted([x for x in out if x["return_rate"] is not None],
                      key=lambda x: x["return_rate"], reverse=True)
    rows = rows[:max(1, limit)]
    # 最近操作（crawl_trade 实盘流水）
    ids = [x["expert_id"] for x in rows]
    ph = ",".join("?" for _ in ids)
    stats: Dict[str, dict] = {}
    if ids:
        stats = {r["expert_id"]: r for r in q(
            f"SELECT expert_id, MAX(trade_date) AS last_date, COUNT(*) AS n FROM crawl_trade "
            f"WHERE market_type='live' AND expert_id IN ({ph}) GROUP BY expert_id", tuple(ids))}
    recent: Dict[str, list] = {}
    if ids:
        for r in q(f"SELECT expert_id, trade_date, trade_time, stock_code, stock_name, action, price "
                   f"FROM crawl_trade WHERE market_type='live' AND expert_id IN ({ph}) "
                   f"ORDER BY trade_date DESC, trade_time DESC", tuple(ids)):
            lst = recent.setdefault(r["expert_id"], [])
            if len(lst) < 3:
                lst.append(r)
    from datetime import date as _d
    today = _d.today().isoformat()
    from strategy import watchlist as W
    watched = {r["expert_id"] for r in W.list_watch("live")}
    for x in rows:
        st = stats.get(x["expert_id"])
        last = st.get("last_date") if st else None
        try:
            gap = ( _d.fromisoformat(today) - _d.fromisoformat(last)).days if last else None
        except ValueError:
            gap = None
        x["last_trade_date"] = last
        x["trade_days_ago"] = gap
        x["n_trades"] = (st.get("n") if st else 0) or 0
        x["recent"] = [{"trade_date": t["trade_date"], "trade_time": (t.get("trade_time") or "")[-8:-3],
                        "stock_code": t["stock_code"], "stock_name": t.get("stock_name") or "",
                        "action": t.get("action"), "price": t.get("price")}
                       for t in (recent.get(x["expert_id"]) or [])]
        x["watched"] = x["expert_id"] in watched
    return {"ok": True, "latest_date": latest, "rows": rows}


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
