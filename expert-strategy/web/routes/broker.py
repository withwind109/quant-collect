"""routes/broker.py — 账户中心 API（东财登录态管理，/api/broker/*）。

参考 xcq-platform scripts/dashboard/routes_account.py（2026-10-19 移植精简）。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body

from core.broker_account import (account_logs, account_manager, check_login,
                                 get_account, heartbeat_once, list_accounts)

router = APIRouter(prefix="/api/broker", tags=["broker-accounts"])


@router.get("/check")
def broker_check():
    """前端「开始爬取」前快速探测东财登录态。"""
    try:
        info = check_login("eastmoney")
        return {"ok": True, **info}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "logged_in": False, "error": str(exc)}


@router.get("/accounts")
def broker_accounts():
    try:
        return {"ok": True, "accounts": list_accounts()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


@router.post("/login/start")
def broker_login_start(body: Dict[str, Any] = Body(...)):
    alias = str(body.get("alias") or "")
    res = account_manager.start_login("eastmoney", alias)
    return {"ok": bool(res.get("ok")), **res}


@router.get("/login/{sid}/snapshot")
def broker_login_snapshot(sid: str):
    sess = account_manager.get_session(sid)
    if not sess:
        return {"ok": False, "error": "会话不存在或已过期"}
    snap = sess.snapshot()
    return {"ok": True, **snap}


@router.get("/login/{sid}/status")
def broker_login_status(sid: str):
    sess = account_manager.get_session(sid)
    if not sess:
        return {"ok": False, "error": "会话不存在或已过期"}
    return {"ok": True, **sess.status()}


@router.post("/login/{sid}/cancel")
def broker_login_cancel(sid: str):
    sess = account_manager.get_session(sid)
    if not sess:
        return {"ok": False, "error": "会话不存在或已过期"}
    sess.cancel()
    return {"ok": True}


@router.post("/accounts/{aid}/heartbeat")
def broker_account_heartbeat(aid: str):
    acc = get_account(aid)
    if not acc:
        return {"ok": False, "error": "账户不存在"}
    alive = heartbeat_once(acc)
    return {"ok": True, "alive": alive}


@router.post("/accounts/{aid}/toggle")
def broker_account_toggle(aid: str, body: Dict[str, Any] = Body(...)):
    from core import db as DB
    from paths import DB_PATH
    enabled = 1 if body.get("enabled") else 0
    n = DB.qexec("UPDATE broker_accounts SET keepalive_enabled=? WHERE id=?",
                 (enabled, aid), db_path=DB_PATH)
    return {"ok": n > 0}


@router.delete("/accounts/{aid}")
def broker_account_delete(aid: str):
    from pathlib import Path
    from core import db as DB
    from paths import DB_PATH
    acc = get_account(aid)
    if not acc:
        return {"ok": False, "error": "账户不存在"}
    ss = acc.get("storage_state_path") or ""
    DB.qexec("DELETE FROM broker_accounts WHERE id=?", (aid,), db_path=DB_PATH)
    DB.qexec("DELETE FROM broker_account_log WHERE account_id=?", (aid,), db_path=DB_PATH)
    if ss and Path(ss).exists():
        try:
            Path(ss).unlink()
            Path(ss).parent.rmdir()
        except Exception:
            pass
    return {"ok": True}


@router.get("/accounts/{aid}/logs")
def broker_account_logs(aid: str, limit: int = 50):
    return {"ok": True, "logs": account_logs(aid, limit)}
