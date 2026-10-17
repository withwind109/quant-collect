"""关注高手列表（对应 xcq-platform 的 xcq_expert_watchlist 表 + scripts/experts/watchlist.py）。

v1 镜像策略（universe.source=watchlist）与 v2（include_watchlist）从这里取关注名单；
社区高手页面提供关注/取消关注操作。实盘高手（market_type='live'）与模拟盘分开存。
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List

from core.db import q, qexec


def list_watch(market_type: str = "sim") -> List[dict]:
    if market_type == "live":
        rows = q("SELECT expert_id, name, note, created_at FROM xcq_expert_watchlist "
                 "WHERE source='eastmoney' AND market_type='live' ORDER BY created_at")
    else:
        rows = q("SELECT expert_id, name, note, created_at FROM xcq_expert_watchlist "
                 "WHERE COALESCE(market_type,'sim') <> 'live' ORDER BY created_at")
    return rows


def is_watched(expert_id: str, market_type: str = "sim") -> bool:
    rows = q("SELECT expert_id FROM xcq_expert_watchlist WHERE expert_id=? AND COALESCE(market_type,'sim')=?",
             (str(expert_id), "live" if market_type == "live" else "sim"))
    return bool(rows)


def add(expert_id: str, name: str = "", market_type: str = "sim", note: str = "") -> bool:
    mt = "live" if market_type == "live" else "sim"
    n = qexec(
        "INSERT OR REPLACE INTO xcq_expert_watchlist(expert_id, source, market_type, name, note, created_at) "
        "VALUES (?, 'eastmoney', ?, ?, ?, ?)",
        (str(expert_id), mt, name or "", note or "", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    return n > 0


def remove(expert_id: str, market_type: str = "sim") -> bool:
    mt = "live" if market_type == "live" else "sim"
    n = qexec("DELETE FROM xcq_expert_watchlist WHERE expert_id=? AND COALESCE(market_type,'sim')=?",
              (str(expert_id), mt))
    return n > 0


def toggle(expert_id: str, name: str = "", market_type: str = "sim") -> str:
    """关注/取消关注，返回操作后的状态（'added' / 'removed'）。"""
    if is_watched(expert_id, market_type):
        remove(expert_id, market_type)
        return "removed"
    add(expert_id, name, market_type)
    return "added"


def watch_pairs(env: str = "sim") -> List[tuple]:
    """[(expert_id, name)] —— engine.watch_for 的同构数据（engine 直读表，这里供 UI 用）。"""
    return [(r["expert_id"], r.get("name") or r["expert_id"]) for r in list_watch("live" if env == "live" else "sim")]


def counts() -> Dict[str, int]:
    rows = q("SELECT COALESCE(market_type,'sim') mt, COUNT(*) c FROM xcq_expert_watchlist GROUP BY mt")
    out = {"sim": 0, "live": 0}
    for r in rows:
        out[r["mt"] if r["mt"] in out else "sim"] = r["c"]
    return out
