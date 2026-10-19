"""高手评估页 API（仅东财实盘链路——模拟盘爬取通道已于 2026-10-19 整体删除）。

GET  /api/expert-crawl/dates               可用交易日
GET  /api/expert-crawl/status              任务状态（crawl/expert_eval/backtest/…）
GET  /api/expert-crawl/log                 任务日志尾部
GET  /api/expert-crawl/live-experts        实盘高手榜（本地 crawl_expert live 前50）
POST /api/expert-crawl/live-experts/refresh 手动刷新榜单（直拉东财 rtV1 单榜落库）
GET  /api/expert-crawl/watchlist           关注列表（本地，market_type=live 与东财同步）
POST /api/expert-crawl/watchlist           加关注（live=代你在东财账号关注）
DELETE /api/expert-crawl/watchlist/{id}    取消关注（live=同步取消东财关注）
POST /api/expert-crawl/watchlist/sync      同步东财账号已关注名单到本地
POST /api/expert-crawl/watchlist/fetch-detail  拉取关注组合最近调仓明细（rtV3→crawl_trade）
GET  /api/expert-crawl/sources             数据源选项
GET  /api/expert-crawl/evaluation          最新一次能力评估结果（rows+meta+bench）
POST /api/expert-crawl/evaluation/run      触发能力评估（后台子进程，仅实盘）
GET  /api/expert-crawl/evaluation/detail   单高手明细（净值曲线+成交+指标，现算）
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from paths import CRAWL_DATA_DIR, DB_PATH
from web import jobs

router = APIRouter(prefix="/api/expert-crawl", tags=["expert-crawl"])

SOURCE_CHOICES = ["eastmoney"]
SOURCE_LABELS = {"eastmoney": "东财"}

#: 全部后台任务名（状态文件 logs/job_<name>.json）；策略页的回测/净值更新/体检也走这套
_JOB_NAMES = ("crawl", "expert_eval", "backtest", "paper_update", "health")


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


@router.get("/status")
async def status() -> Dict[str, Any]:
    """全部后台任务状态（crawl/expert_eval/backtest/paper_update/health）。"""
    return {name: jobs.job_view(name, include_log=False) for name in _JOB_NAMES}


@router.get("/log")
async def log(job: str = Query("crawl", pattern="^(crawl|expert_eval|backtest|paper_update|health)$")) -> Dict[str, Any]:
    return {"job": job, "log_tail": jobs.read_log_tail(job)}


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


def _persist_live_trades(records, fetched_at: str = "", trigger: str = "manual") -> int:
    """rtV3 明细落库 crawl_trade（market_type=live，按 row_sig 幂等去重）。

    同时写一行 xcq_fetch_log（每次拉取动作一条，用于数据新鲜度展示——
    去重导致 0 新增时 crawl_trade 里看不出"上次拉取时间"）。
    """
    from core import db as DB
    import uuid
    fetched_at = fetched_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n = 0
    per: dict = {}
    per_new: dict = {}
    for r in records:
        td = r.trade_date or ""
        if not td:
            continue
        per[r.expert_id] = per.get(r.expert_id, 0) + 1
        sig = DB._sig(td, r.source, r.expert_id, r.stock_code, r.action, r.trade_time,
                      r.price, r.weight_before, r.weight_after)
        row_n = DB.qexec(
            "INSERT OR IGNORE INTO crawl_trade (trade_date, source, asset_type, channel, expert_id, "
            "expert_name, owner, stock_code, stock_name, market, action, price, volume, amount, "
            "weight_before, weight_after, weight_label, return_rate, day_return_rate, trade_time, "
            "raw_json, market_type, row_sig, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (td, r.source, r.asset_type, r.channel, r.expert_id, r.expert_name, r.owner,
             r.stock_code, r.stock_name, r.market, r.action, r.price, r.volume, r.amount,
             r.weight_before, r.weight_after, r.weight_label, r.return_rate, r.day_return_rate,
             r.trade_time, DB._j(r.raw), "live", sig, fetched_at))
        n += row_n
        per_new[r.expert_id] = per_new.get(r.expert_id, 0) + row_n
    for eid, cnt in per.items():   # [2026-10-19 修] n_total/n_new 原来用的是全部组合的合计，应按组合分别记
        DB.qexec("INSERT OR REPLACE INTO xcq_fetch_log (id, expert_id, market_type, fetched_at, "
                 "n_total, n_new, trigger) VALUES (?,?,?,?,?,?,?)",
                 (uuid.uuid4().hex, eid, "live", fetched_at, cnt, per_new.get(eid, 0), trigger))
    return n


def fetch_live_trades(expert_ids: List[str] | None = None, trigger: str = "manual") -> Dict[str, Any]:
    """拉取关注组合的 rtV3 调仓明细并累计入库（fetch-detail 路由 / 定时调度器共用）。

    expert_ids 为空 = 拉全部 live 关注；返回 {ok, n, total, rows, msg}。
    未登录或关注列表为空时 ok=False 且给出原因（调度器静默跳过，手动触发时提示用户）。
    """
    wl = _watch_rows("live")
    eids = [e.strip() for e in (expert_ids or []) if e.strip()] or [r["expert_id"] for r in wl]
    if not eids:
        return {"ok": False, "msg": "关注列表为空", "n": 0, "total": 0, "rows": []}
    src = _em_source()
    from core.models import Expert
    experts = [Expert(source="eastmoney", expert_id=eid,
                      name=next((r["name"] for r in wl if r["expert_id"] == eid), eid))
               for eid in eids]
    records = src._spzh_trades(experts, target_date="")  # 不按日过滤 = 最近 40 条全量
    n = _persist_live_trades(records, trigger=trigger)
    rows = [{"expert_id": r.expert_id, "expert_name": r.expert_name,
             "trade_date": r.trade_date, "trade_time": r.trade_time,
             "stock_code": r.stock_code, "stock_name": r.stock_name,
             "action": r.action, "price": r.price,
             "weight_after": r.weight_after, "weight_label": r.weight_label or ""}
            for r in records]
    return {"ok": True, "n": n, "total": len(rows), "rows": rows}


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
    eids = [body.expert_id.strip()] if body.expert_id.strip() else None
    try:
        d = fetch_live_trades(eids, trigger="manual")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"rtV3 拉取失败: {exc}") from exc
    return d

@router.get("/watchlist/freshness")
async def watch_freshness() -> Dict[str, Any]:
    """关注组合的调仓数据新鲜度（评估列表展示 + 过旧提醒）。

    数据口径：crawl_trade 的最近调仓日期/条数 + xcq_fetch_log 的最近拉取时间。
    stale 规则：从未拉取，或最近拉取距今 >7 天（评估样本可能已断更）。
    """
    from core.db import q, qone
    _db()
    out: List[Dict[str, Any]] = []
    for r in _watch_rows("live"):
        eid = r["expert_id"]
        st = qone("SELECT MAX(trade_date) AS last_date, COUNT(*) AS n FROM crawl_trade "
                  "WHERE market_type='live' AND expert_id=?", (eid,)) or {}
        fl = qone("SELECT fetched_at FROM xcq_fetch_log WHERE market_type='live' AND expert_id=? "
                  "ORDER BY fetched_at DESC LIMIT 1", (eid,))
        fetched_at = (fl or {}).get("fetched_at")
        last_date = st.get("last_date")
        hours_ago = days_since_trade = None
        now = datetime.now()
        if fetched_at:
            try:
                hours_ago = round((now - datetime.strptime(fetched_at, "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600, 1)
            except ValueError:
                pass
        if last_date:
            try:
                from datetime import date as _d
                days_since_trade = (_d.today() - _d.fromisoformat(last_date)).days
            except ValueError:
                pass
        stale = fetched_at is None or (hours_ago is not None and hours_ago > 24 * 7)
        out.append({"expert_id": eid, "name": r.get("name") or "",
                    "n_trades": st.get("n") or 0, "last_trade_date": last_date,
                    "days_since_trade": days_since_trade,
                    "fetched_at": fetched_at, "hours_since_fetch": hours_ago, "stale": stale})
    return {"ok": True, "rows": out}


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
        return {"ok": True, "latest_date": None, "fetched_at": None, "rows": []}
    fetched_at = (qone("SELECT MAX(fetched_at) AS t FROM crawl_expert "
                       "WHERE trade_date=? AND market_type='live'", (latest,)) or {}).get("t")
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
    return {"ok": True, "latest_date": latest, "fetched_at": fetched_at, "rows": rows}


class BoardRefreshBody(BaseModel):
    board: str = "total"   # total=总榜(10004,累计收益) / day=日榜(10005,当日收益)
    limit: int = 100


@router.post("/live-experts/refresh")
async def live_experts_refresh(body: BoardRefreshBody) -> Dict[str, Any]:
    """手动拉取最新东财 rtV1 榜单（单榜）并落库（今日 upsert），供「加载最新」按钮调用。

    与整页爬取（/run，含流水+rtV3）不同：本端点只刷新高手榜名册，轻量快速，
    前端按榜分两步调用以展示进度。
    """
    board_type = {"total": "10004", "day": "10005"}.get(body.board)
    if not board_type:
        raise HTTPException(400, "board 仅支持 total/day")
    limit = max(20, min(int(body.limit or 100), 200))
    from core.http import HttpClient
    from sources.eastmoney import EastmoneySource
    src = EastmoneySource(HttpClient(min_interval=0.3), rank_types=[board_type])
    experts = src.hot_experts(limit=limit)
    if not experts:
        raise HTTPException(502, "东财榜单拉取为空（接口可能变更或被限流）")

    from datetime import date as _d
    from core.db import qone, qexec
    _db()
    today = _d.today().isoformat()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    updated = inserted = 0
    for e in experts:
        extra_new = dict(e.extra or {})
        row = qone("SELECT extra_json, return_rate FROM crawl_expert "
                   "WHERE trade_date=? AND expert_id=? AND market_type='live'",
                   (today, e.expert_id))
        if row:
            try:
                extra = json.loads(row.get("extra_json") or "{}")
            except (TypeError, ValueError):
                extra = {}
            # [2026-10-19 修] 只覆盖有值的键：总榜刷新时 daily_gain=None 会把日榜已写入的当日收益抹掉
            extra.update({k: v for k, v in extra_new.items() if v is not None})
            # 总榜才覆盖累计收益；日榜只补当日收益，不动已有累计口径
            sets = ["name=?", "owner=?", "followers=?", "extra_json=?", "fetched_at=?"]
            args: list = [e.name, e.owner, e.followers or 0,
                          json.dumps(extra, ensure_ascii=False), now]
            if e.return_rate is not None:
                sets.append("return_rate=?")
                args.append(e.return_rate)
            args += [today, e.expert_id]
            qexec(f"UPDATE crawl_expert SET {', '.join(sets)} "
                  "WHERE trade_date=? AND expert_id=? AND market_type='live'", tuple(args))
            updated += 1
        else:
            qexec("""INSERT INTO crawl_expert
                 (trade_date, source, asset_type, channel, expert_id, name, owner, rank_label,
                  followers, return_rate, total_return, extra_json, market_type, fetched_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (today, e.source, e.asset_type, e.channel, e.expert_id, e.name, e.owner,
               e.rank_label, e.followers or 0, e.return_rate, None,
               json.dumps(extra_new, ensure_ascii=False), e.market_type, now))
            inserted += 1
    return {"ok": True, "board": body.board, "fetched": len(experts),
            "updated": updated, "inserted": inserted, "fetched_at": now}


# ── 高手能力评估 ─────────────────────────────────────────────────────────────

class EvalBody(BaseModel):
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
    """触发能力评估——仅实盘（模拟盘通道已删除，env 参数一并移除）。"""
    argv = ["-m", "strategy.expert_eval",
            "--min-sells", str(max(1, body.min_sells)), "--min-days", str(max(1, body.min_days))]
    if body.start.strip():
        argv += ["--start", body.start.strip()]
    if body.end.strip():
        argv += ["--end", body.end.strip()]
    try:
        jobs.start_job("expert_eval", argv)
    except jobs.JobBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "msg": "评估已启动", "env": "live"}


@router.get("/evaluation/detail")
async def evaluation_detail(expert_id: str = Query(""), env: str = Query("live")) -> Dict[str, Any]:
    if not expert_id.strip():
        raise HTTPException(400, "缺少高手ID")
    from strategy import expert_eval as EE
    d = EE.expert_detail(expert_id.strip(), env="live")
    if not d.get("ok"):
        raise HTTPException(404, d.get("msg") or "无法评估该高手")
    return d
