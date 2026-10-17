"""高手跟随策略页 API（对齐源项目 routes_expert_follow.py 的 15 端点 + 本项目补充）。

GET  /api/expert-follow/overview               策略看板（账户前向表现/晋级状态）
POST /api/expert-follow/overview/update        后台更新前向净值（paper_sim）
GET  /api/expert-follow/recon                  前向 vs 回测对账汇总
GET  /api/expert-follow/strategies             策略文件列表
GET  /api/expert-follow/strategies/{sid}       单策略原文 + 解析 + 描述 + 指纹
POST /api/expert-follow/strategies/preview     参数预览（校验，不保存）
POST /api/expert-follow/strategies             另存为用户策略
GET  /api/expert-follow/backtests              回测配置列表
GET  /api/expert-follow/backtests/{bid}        单回测配置（含策略展开数）
POST /api/expert-follow/backtests              保存自定义回测
POST /api/expert-follow/run                    运行统一回测（子进程 + 状态文件）
GET  /api/expert-follow/run-status             回测运行状态
GET  /api/expert-follow/runs                   运行列表（archived=1 档案）
POST /api/expert-follow/runs/{run_id}/archive  归档/取消归档
POST /api/expert-follow/publish                发布方案到模拟盘
POST /api/expert-follow/promote/{action}       晋级 request/approve/revoke
GET  /api/expert-follow/data-health            数据体检
POST /api/expert-follow/data-health/refresh    后台重算体检
GET  /api/expert-follow/prices/stats           价格库状态
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from paths import LOG_DIR
from web import jobs

router = APIRouter(prefix="/api/expert-follow", tags=["expert-follow"])


def _sys() -> str:
    return sys.executable


# ── 策略看板 ─────────────────────────────────────────────────────────────

def _acct_perf(acct: dict) -> Dict[str, Any]:
    """账户前向表现：累计/回撤/最大回撤/超额/Alpha（与原 Streamlit 页同一套口径）。"""
    from core.db import q
    nav = q("SELECT trade_date, total_nav FROM xcq_paper_nav_daily WHERE account_id=? ORDER BY trade_date",
            (acct["id"],))
    initial = float(acct.get("initial_capital") or 1_000_000)
    pts = [(str(r["trade_date"]), float(r["total_nav"])) for r in nav if r.get("total_nav") is not None]
    out: Dict[str, Any] = {"n_days": len(pts), "first_date": pts[0][0] if pts else None,
                           "last_date": pts[-1][0] if pts else None, "nav": pts}
    if not pts:
        return {**out, "cum_return_pct": 0.0, "drawdown_pct": 0.0, "max_drawdown_pct": 0.0}
    vs = [v for _, v in pts]
    peak, mdd = initial, 0.0
    for v in vs:
        peak = max(peak, v)
        mdd = max(mdd, 1 - v / peak)
    out.update(cum_return_pct=round((vs[-1] / initial - 1) * 100, 2),
               drawdown_pct=round((1 - vs[-1] / peak) * 100, 2), max_drawdown_pct=round(mdd * 100, 2))
    try:
        from strategy import alpha as A
        ds = [d for d, _ in pts]
        b = A.bench_returns(ds[0], ds[-1])
        bd = [d for d in ds if d in b]
        if len(bd) >= 2:
            br = 1.0
            for d in bd[1:]:
                br *= (1 + b[d])
            out["bench_return_pct"] = round((br - 1) * 100, 2)
            out["excess_pct"] = round(out["cum_return_pct"] - out["bench_return_pct"], 2)
        if len(pts) >= 10:
            rets = [vs[i] / vs[i - 1] - 1 for i in range(1, len(vs))]
            al = A.regress(ds[1:], rets, b)
            if al:
                out.update(alpha_pct=round(al["alpha"], 2), alpha_t=round(al["alpha_t"], 2))
    except Exception:  # noqa: BLE001
        pass
    return out


@router.get("/overview")
async def overview() -> Dict[str, Any]:
    from core.db import q
    from strategy import config as C
    from strategy import promotion

    cfgs = {c["id"]: c for c in C.list_strategies() if not c.get("error")}
    bind = {c["bind_strategy_id"]: c for c in cfgs.values() if c.get("bind_strategy_id")}
    promos = {p["strategy_id"]: p for p in promotion.list_all()}
    rows = q("SELECT a.id, a.name, a.initial_capital, a.enabled, a.created_at, s.strategy_id, s.tier, s.spec_json "
             "FROM xcq_paper_accounts a JOIN xcq_strategies s ON s.strategy_id=a.strategy_id "
             "WHERE s.strategy_id LIKE 'expert_follow%' ORDER BY a.created_at")
    accts = []
    for r in rows:
        try:
            spec = json.loads(r.get("spec_json") or "{}")
        except ValueError:
            spec = {}
        cfg = bind.get(r["strategy_id"]) or cfgs.get(spec.get("config_id") or "")
        perf = _acct_perf(r)
        p = promos.get(r["strategy_id"])
        tier = r.get("tier") or "E0"
        stage = ("live" if p and p.get("state") == "approved" else
                 "requested" if p and p.get("state") == "requested" else
                 "halted" if p and p.get("state") == "halted" else
                 "ready" if tier in ("E2", "E3") else "observing")
        accts.append({
            "id": r["id"], "name": r["name"], "strategy_id": r["strategy_id"],
            "tier": r.get("tier") or "E0", "stage": stage,
            "initial_capital": r.get("initial_capital"),
            "config_id": (cfg or {}).get("id") or spec.get("config_id") or "—",
            "family": (cfg or {}).get("family") or "—",
            "promotion_state": (p or {}).get("state") or "—",
            "perf": {k: v for k, v in perf.items() if k != "nav"},
            "nav": [[d, v] for d, v in perf["nav"]],
        })
    return {"accounts": accts, "total": len(accts)}


@router.post("/overview/update")
async def overview_update() -> Dict[str, Any]:
    """后台更新全部账户前向净值（python -m strategy.paper_sim）。"""
    try:
        jobs.start_job("paper_update", ["-m", "strategy.paper_sim"])
    except jobs.JobBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "msg": "前向净值更新已启动（数据量大时耗时几分钟，完成前可轮询 /status）"}


@router.get("/recon")
async def recon() -> Dict[str, Any]:
    from strategy import recon as R
    try:
        rs = R.summaries()
    except Exception as exc:  # noqa: BLE001
        return {"rows": [], "error": str(exc)}
    return {"rows": [{"account": k, **v} for k, v in rs.items()]}


# ── 策略文件 ─────────────────────────────────────────────────────────────

@router.get("/strategies")
async def strategies() -> Dict[str, Any]:
    from strategy import config as C
    try:
        ss = C.list_strategies()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"读取策略文件失败: {exc}") from exc
    return {"strategies": [{"id": s["id"], "family": s.get("family"),
                            "builtin": bool(s.get("_builtin")),
                            "label": s.get("label") or s.get("error") or "",
                            "hash": s.get("hash") or "—",
                            "error": s.get("error")} for s in ss]}


@router.get("/strategies/{sid}")
async def strategy(sid: str) -> Dict[str, Any]:
    from strategy import config as C
    try:
        s = C.load_strategy(sid)
    except C.ConfigError as exc:
        raise HTTPException(404, str(exc)) from exc
    fp = C.strategy_path(sid)
    return {"id": sid, "path": str(fp) if fp else None,
            "raw": fp.read_text(encoding="utf-8") if fp else "",
            "spec": {k: v for k, v in s.items() if not k.startswith("_")},
            "describe": C.describe(s), "hash": C.strategy_hash(s)}


class StrategyBody(BaseModel):
    spec: Dict[str, Any]


@router.post("/strategies/preview")
async def strategy_preview(body: StrategyBody) -> Dict[str, Any]:
    from strategy import config as C
    try:
        ns = C.normalize_strategy(body.spec)
        return {"ok": True, "describe": C.describe(ns), "hash": C.strategy_hash(ns)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"校验失败: {exc}") from exc


@router.post("/strategies")
async def strategy_save(body: StrategyBody) -> Dict[str, Any]:
    from strategy import config as C
    try:
        ns = C.save_user_strategy(body.spec)
        return {"ok": True, "id": ns["id"], "msg": f"已保存 strategies/user/{ns['id']}.json5"}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"保存失败: {exc}") from exc


# ── 回测配置 ─────────────────────────────────────────────────────────────

@router.get("/backtests")
async def backtests() -> Dict[str, Any]:
    from strategy import config as C
    try:
        bts = C.list_backtests()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"读取回测配置失败: {exc}") from exc
    return {"backtests": [{"id": b["id"], "name": b.get("name") or b["id"],
                           "data_env": b.get("data_env"), "n_strategies": b.get("n_strategies"),
                           "builtin": bool(b.get("_builtin")), "error": b.get("error")} for b in bts]}


@router.get("/backtests/{bid}")
async def backtest(bid: str) -> Dict[str, Any]:
    from strategy import config as C
    try:
        b = C.load_backtest(bid)
    except C.ConfigError as exc:
        raise HTTPException(404, str(exc)) from exc
    rows = []
    for e in b.get("strategies") or []:
        try:
            n = len(C.expand(e, b["data_env"])) if e.get("enabled", True) else 0
        except Exception:  # noqa: BLE001
            n = -1
        rows.append({"ref": e["ref"], "enabled": bool(e.get("enabled", True)), "n_variants": n})
    return {"id": bid, "period": b["period"], "capital": b["capital"],
            "baseline": b["baseline"], "gate": b["gate"], "null": b["null"],
            "display": b["display"], "strategies": rows}


class BacktestBody(BaseModel):
    id: str
    data_env: str = "sim"
    strategies: List[str]


@router.post("/backtests")
async def backtest_save(body: BacktestBody) -> Dict[str, Any]:
    from strategy import config as C
    bid = body.id.strip()
    if not re.fullmatch(r"\w+", bid) or not body.strategies:
        raise HTTPException(400, "id 需为字母/数字/下划线，且至少选一个策略")
    try:
        b2 = C.save_user_backtest({"id": bid, "name": bid, "data_env": body.data_env,
                                   "strategies": [{"ref": r} for r in body.strategies]})
        return {"ok": True, "id": b2["id"], "msg": f"已保存 backtests/user/{b2['id']}.json5"}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"保存失败: {exc}") from exc


# ── 运行回测 ─────────────────────────────────────────────────────────────

@router.get("/run-status")
async def run_status(run_id: str = Query("")) -> Dict[str, Any]:
    if not run_id:
        raise HTTPException(400, "缺少 run_id")
    fp = LOG_DIR / f"ef_status_{run_id}.json"
    if not fp.exists():
        raise HTTPException(404, "没有该运行")
    st = json.loads(fp.read_text(encoding="utf-8") or "{}")
    return {"ok": True, **st}


class RunBtBody(BaseModel):
    backtest_id: str = "unified_sim"
    end: str = ""


@router.post("/run")
async def run_bt(body: RunBtBody) -> Dict[str, Any]:
    from strategy import config as C
    ok = [b["id"] for b in C.list_backtests() if not b.get("error")]
    if body.backtest_id not in ok:
        raise HTTPException(400, f"未知回测配置 {body.backtest_id}（可选 {ok}）")
    if (jobs.read_status("backtest") or {}).get("state") == "running":
        raise HTTPException(409, "已有回测任务在运行中")
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    status_fp = LOG_DIR / f"ef_status_{run_id}.json"
    status_fp.write_text(json.dumps({"run_id": run_id, "state": "starting", "progress": 0}),
                         encoding="utf-8")
    argv = ["-W", "ignore", "-m", "strategy.runner", "--backtest", body.backtest_id,
            "--status-file", str(status_fp), "--run-id", run_id]
    if body.end.strip():
        argv += ["--end", body.end.strip()]
    try:
        jobs.start_job("backtest", argv)
    except jobs.JobBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "run_id": run_id, "msg": "回测已启动"}


@router.get("/runs")
async def runs(archived: int = Query(0)) -> Dict[str, Any]:
    from strategy import store
    try:
        rs = store.list_runs(archived=bool(archived))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"读取运行记录失败: {exc}") from exc
    return {"runs": [{"run_id": r["run_id"], "backtest_id": r.get("backtest_id"),
                      "data_env": r.get("data_env"), "status": r.get("status"),
                      "started_at": r.get("started_at"), "finished_at": r.get("finished_at"),
                      "passed": r.get("passed_json") if isinstance(r.get("passed_json"), list)
                      else (r.get("passed") or []),
                      "archived": r.get("archived"),
                      "n_variants": r.get("n_variants"),
                      "families": r.get("families"),
                      "period_start": r.get("period_start"), "period_end": r.get("period_end"),
                      "health_ok": r.get("health_ok"), "error": r.get("error")} for r in rs]}


class ArchiveBody(BaseModel):
    archived: bool = True


@router.post("/runs/{run_id}/archive")
async def run_archive(run_id: str, body: ArchiveBody) -> Dict[str, Any]:
    from strategy import store
    try:
        store.set_archived(run_id, body.archived)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, str(exc)) from exc
    return {"ok": True, "run_id": run_id, "archived": body.archived}


@router.get("/runs/{run_id}")
async def run_detail(run_id: str) -> Dict[str, Any]:
    from strategy import live as L
    s = L.load_summary(run_id)
    if not s:
        raise HTTPException(404, "找不到该运行的 summary.json")
    if s.get("status") == "blocked":
        return {"run_id": run_id, "blocked": True, "reason": s.get("reason") or s.get("msg")}
    variants = s.get("variants") or {}
    vrows = []
    for k, v in variants.items():
        m = v.get("metrics") or {}
        g = v.get("gate") or {}
        vrows.append({"key": k, "ref": v.get("ref"), "engine": v.get("engine") or "",
                      "total_return_pct": m.get("total_return_pct"),
                      "annualized_pct": m.get("annualized_pct"),
                      "max_drawdown_pct": m.get("max_drawdown_pct"),
                      "sharpe": m.get("sharpe"), "alpha_pct": m.get("alpha_pct"),
                      "alpha_t": m.get("alpha_t"), "n_buys": m.get("n_buys"),
                      "passed": bool(g.get("passed")),
                      "fail_checks": [c for c, ok in (g.get("checks") or {}).items() if not ok]})
    vrows.sort(key=lambda r: (not r["passed"], -(r["total_return_pct"] or -1e9)))
    curves = {k: [[d, round(float(x), 4)] for d, x in (v.get("nav") or [])]
              for k, v in list(variants.items())[:8] if v.get("nav")}
    return {"run_id": run_id, "backtest_id": s.get("backtest_id"),
            "data_env": s.get("data_env"), "status": s.get("status"),
            "period": s.get("period"), "elapsed_s": s.get("elapsed_s"),
            "variants": vrows, "curves": curves}


# ── 发布 / 晋级 ──────────────────────────────────────────────────────────

class PublishBody(BaseModel):
    run_id: str
    key: str
    observe: bool = False
    capital: float = 1_000_000.0


@router.post("/publish")
async def publish(body: PublishBody) -> Dict[str, Any]:
    from strategy import live as L
    try:
        r = L.publish(body.run_id, body.key, observe=body.observe, capital=body.capital)
        return {"ok": True, **r}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class PromoteBody(BaseModel):
    strategy_id: str
    capital: float = 1_000_000.0
    max_dd_pct: float = 10.0


@router.post("/promote/{action}")
async def promote(action: str, body: PromoteBody) -> Dict[str, Any]:
    from strategy import promotion
    if action not in ("request", "approve", "revoke"):
        raise HTTPException(404, f"未知晋级动作 {action}")
    try:
        if action == "request":
            r = promotion.request(body.strategy_id, body.capital, body.max_dd_pct / 100.0)
        elif action == "approve":
            r = promotion.approve(body.strategy_id)
        else:
            r = promotion.revoke(body.strategy_id)
        return {"ok": True, "result": r}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# ── 数据体检 / 价格库 ────────────────────────────────────────────────────

@router.get("/data-health")
async def data_health() -> Dict[str, Any]:
    from strategy import data_health as DH
    try:
        h = DH.compute()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"读取体检结果失败: {exc}") from exc
    if not h:
        return {"generated_at": None, "envs": {}}
    return {"generated_at": h.get("generated_at"),
            "envs": {env: h.get(env) or {} for env in ("sim", "live")}}


@router.post("/data-health/refresh")
async def data_health_refresh() -> Dict[str, Any]:
    try:
        jobs.start_job("health", ["-m", "strategy.data_health"])
    except jobs.JobBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "msg": "体检已启动"}


@router.get("/prices/stats")
async def prices_stats() -> Dict[str, Any]:
    from strategy import prices
    try:
        return {"ok": True, "stats": prices.stats()}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, str(exc)) from exc
