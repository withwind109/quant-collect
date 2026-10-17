"""策略注册表 + 等级 (E0~E3)。策略 spec 变化 = 新版本, 等级归零 (证据属于具体版本)。

迁移自 scripts/recs/registry.py (2026-10-16)。存储 MySQL -> 本地 SQLite
(strategy.recs_store, 表 xcq_strategies / xcq_strategy_tier_log); 导入改绝对路径。
MySQL 专有语法改写: CURDATE() -> date().isoformat() 参数; 其余 SQL 原样
(命名占位符 :name 由 recs_store 转 ?)。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from strategy import recs_store as store

TIERS = ["E0", "E1", "E2", "E3"]
TIER_LABEL = {"E0": "未验证", "E1": "回测通过", "E2": "初步可信", "E3": "显著可信"}


def tier_rank(t: Optional[str]) -> int:
    try:
        return TIERS.index(str(t or "E0").upper())
    except ValueError:
        return 0


def spec_hash(spec: Dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(spec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def get_strategy(strategy_id: str) -> Optional[Dict[str, Any]]:
    rows = store.fetch("SELECT * FROM xcq_strategies WHERE strategy_id=:s", {"s": strategy_id})
    return rows[0] if rows else None


def list_strategies(status: Optional[str] = None) -> List[Dict[str, Any]]:
    if status:
        return store.fetch("SELECT * FROM xcq_strategies WHERE status=:s ORDER BY strategy_id", {"s": status})
    return store.fetch("SELECT * FROM xcq_strategies ORDER BY strategy_id")


def upsert_strategy(strategy_id: str, name: str, kind: str, spec: Dict[str, Any], *,
                    backtestable: bool = False, feed_enabled: bool = True, eval_horizon: int = 5,
                    description: str = "", lifecycle_ref: Optional[str] = None) -> Dict[str, Any]:
    """登记/更新策略。spec 变化则 version+1、等级归零并记入 tier_log。"""
    store.ensure_schema()
    h = spec_hash(spec)
    cur = get_strategy(strategy_id)
    spec_json = json.dumps(spec, ensure_ascii=False, sort_keys=True)
    if cur is None:
        store.run(
            "INSERT INTO xcq_strategies (strategy_id,name,kind,spec_json,spec_hash,version,tier,tier_updated_at,"
            "status,backtestable,feed_enabled,eval_horizon,description,lifecycle_ref) "
            "VALUES (:id,:name,:kind,:spec,:h,1,'E0',:now,'active',:bt,:fe,:eh,:d,:lr)",
            {"now": now(), "id": strategy_id, "name": name, "kind": kind, "spec": spec_json, "h": h,
             "bt": int(backtestable), "fe": int(feed_enabled), "eh": eval_horizon,
             "d": description[:500], "lr": lifecycle_ref})
        store.run("INSERT INTO xcq_strategy_tier_log (strategy_id,from_tier,to_tier,reason,actor,`at`) "
                  "VALUES (:id,NULL,'E0','创建策略','auto',:at)", {"id": strategy_id, "at": now()})
    elif cur.get("spec_hash") != h:
        store.run(
            "UPDATE xcq_strategies SET name=:name,kind=:kind,spec_json=:spec,spec_hash=:h,version=version+1,"
            "tier='E0',tier_updated_at=:now,evidence_since=:es,live_approved=0,backtestable=:bt,eval_horizon=:eh,"
            "description=:d,lifecycle_ref=:lr WHERE strategy_id=:id",
            {"now": now(), "id": strategy_id, "name": name, "kind": kind, "spec": spec_json, "h": h,
             "bt": int(backtestable), "eh": eval_horizon, "es": now().date().isoformat(),
             "d": description[:500], "lr": lifecycle_ref})
        store.run("INSERT INTO xcq_strategy_tier_log (strategy_id,from_tier,to_tier,reason,actor,`at`) "
                  "VALUES (:id,:f,'E0','策略定义变更, 版本+1, 证据归零','auto',:at)",
                  {"id": strategy_id, "f": cur.get("tier"), "at": now()})
    else:
        store.run("UPDATE xcq_strategies SET name=:name,description=:d WHERE strategy_id=:id",
                  {"id": strategy_id, "name": name, "d": description[:500]})
    return get_strategy(strategy_id)  # type: ignore[return-value]


def set_tier(strategy_id: str, to_tier: str, reason: str, actor: str = "auto") -> bool:
    """改等级并留痕。返回是否发生变化。"""
    cur = get_strategy(strategy_id)
    if cur is None or cur.get("tier") == to_tier:
        return False
    store.run("UPDATE xcq_strategies SET tier=:t,tier_updated_at=:now WHERE strategy_id=:id",
              {"id": strategy_id, "t": to_tier, "now": now()})
    if actor == "auto" and not strategy_id.startswith("sim:"):      # 仿真回放的合成策略不能进真实通知队列(会被发到企微群)
        try:            # 等级变更通知(P4-2): 幂等键=策略+方向+日期, 同一天同一变化只推一次
            from strategy.recs_misc import notify
            up = tier_rank(to_tier) > tier_rank(cur.get("tier"))
            notify.enqueue(f"tier:{strategy_id}:{cur.get('tier')}>{to_tier}:{now().strftime('%Y-%m-%d')}", "tier_change",
                           f"策略等级变化 {strategy_id}",
                           f"### {'⬆️ 策略升级' if up else '⬇️ 策略降级'}\n> **{cur.get('name')}**  {cur.get('tier')} → **{to_tier}**\n> {reason}"
                           + ("\n> 已自动撤销该策略的实盘批准，如仍要用于实盘请重新批准。" if (not up and int(cur.get('live_approved') or 0)) else ""),
                           {"strategy_id": strategy_id, "from": cur.get("tier"), "to": to_tier})
        except Exception:   # noqa: BLE001 通知失败不影响等级变更
            pass
    if tier_rank(to_tier) < 2 and int(cur.get("live_approved") or 0):
        # 降到 E2 以下: 实盘批准自动失效 (autotrade 提交前也会复核), 恢复资格后需要重新显式批准
        store.run("UPDATE xcq_strategies SET live_approved=0 WHERE strategy_id=:id", {"id": strategy_id})
    store.run("INSERT INTO xcq_strategy_tier_log (strategy_id,from_tier,to_tier,reason,actor,`at`) "
              "VALUES (:id,:f,:t,:r,:a,:at)",
              {"id": strategy_id, "f": cur.get("tier"), "t": to_tier, "r": reason[:300], "a": actor, "at": now()})
    return True


def log_event(strategy_id: str, reason: str, actor: str = "user") -> None:
    """记一条不改等级的运维事件(暂停/恢复/批准实盘/撤销批准), 等级变更记录里可见。"""
    cur = get_strategy(strategy_id)
    if cur is None:
        return
    store.run("INSERT INTO xcq_strategy_tier_log (strategy_id,from_tier,to_tier,reason,actor,`at`) "
              "VALUES (:id,:t,:t,:r,:a,:at)", {"id": strategy_id, "t": cur.get("tier"), "r": reason[:300], "a": actor, "at": now()})


def set_status(strategy_id: str, status: str, note: str = "", actor: str = "user") -> Dict[str, Any]:
    if status not in ("active", "paused"):
        raise ValueError("status 只能是 active / paused")
    cur = get_strategy(strategy_id)
    if cur is None:
        raise ValueError(f"策略不存在: {strategy_id}")
    if cur["status"] == status:
        return cur
    store.run("UPDATE xcq_strategies SET status=:s WHERE strategy_id=:i", {"s": status, "i": strategy_id})
    if status == "paused":         # 暂停即撤销实盘批准: 恢复后需要重新显式批准
        store.run("UPDATE xcq_strategies SET live_approved=0 WHERE strategy_id=:i", {"i": strategy_id})
    log_event(strategy_id, ("暂停" if status == "paused" else "恢复") + (f": {note}" if note else ""), actor)
    return get_strategy(strategy_id)  # type: ignore[return-value]


def set_live_approval(strategy_id: str, approved: bool, cap_pct: Optional[float] = None, note: str = "",
                      actor: str = "user") -> Dict[str, Any]:
    """批准/撤销用于实盘。E2 只是"有资格", 必须由用户显式批准; 批准要求当前等级>=E2 且策略在用。"""
    cur = get_strategy(strategy_id)
    if cur is None:
        raise ValueError(f"策略不存在: {strategy_id}")
    if approved:
        if tier_rank(cur.get("tier")) < 2:
            raise ValueError(f"当前等级 {cur.get('tier')} 未达 E2, 不能批准用于实盘")
        if cur["status"] != "active":
            raise ValueError("策略已暂停, 不能批准")
        if cap_pct is not None and not (0 < float(cap_pct) <= 100):
            raise ValueError("额度上限需在 (0,100]")
    store.run("UPDATE xcq_strategies SET live_approved=:a, live_approved_at=:t, live_cap_pct=:c WHERE strategy_id=:i",
              {"a": int(approved), "t": now() if approved else None, "c": cap_pct if approved else None, "i": strategy_id})
    log_event(strategy_id, ("批准用于实盘" + (f"(额度上限{cap_pct}%)" if cap_pct else "") if approved else "撤销实盘批准")
              + (f": {note}" if note else ""), actor)
    return get_strategy(strategy_id)  # type: ignore[return-value]


def tier_of(strategy_id: str) -> str:
    """未登记的策略一律视为 E0 (autotrade 等下游据此拦截)。"""
    s = get_strategy(strategy_id)
    return str(s.get("tier") or "E0") if s else "E0"


def now() -> datetime:
    return datetime.now()
