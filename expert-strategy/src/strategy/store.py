"""跟随高手统一回测的运行记录 (2026-09-25)。一次运行 = 一个回测配置 + 当时各策略文件的指纹快照。

详情 (各方案指标/净值/对照/闸门/分组) 存 models/expert_follow/<run_id>/summary.json; 表里只放列表与筛选用字段。
archived=1 的运行从"回测实验室"下拉里隐藏, 仍可在"实验档案"查看 (用户决策: 归档可查, 不删除)。

迁移自 scripts/expert_follow/store.py (2026-10-16): 存储由 MySQL xcq_expert_follow_runs
(SQLAlchemy :name 占位符) 换成本地 SQLite expert_follow_runs 表 (core.db q/qexec/qinsert,
%s 占位符); 目标表实存列固定为 run_id/backtest_id/data_env/status/started_at/finished_at/
spec_json/summary_json/passed_json/archived (由 core.db.ensure_strategy_schema 建表),
源表的附列 (families/period_start/period_end/n_variants/config_hash/health_ok/error)
打包进 spec_json (读时展开还原); ON DUPLICATE KEY UPDATE 语义 → 先 UPDATE 后 INSERT
(只更新本次传入的列, 与 MySQL 版一致); datetime 参数转 isoformat (SQLite TEXT 列)。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List

from core.db import ensure_strategy_schema, q, qexec, qinsert
from paths import DB_PATH

_TABLE = "expert_follow_runs"
#: SQLite 表实存列 (core/db.py STRATEGY_TABLES["expert_follow_runs"])
_COLS = ("run_id", "backtest_id", "data_env", "status", "started_at", "finished_at",
         "spec_json", "summary_json", "passed_json", "archived")


def _fmt(v: Any) -> Any:
    """datetime → isoformat 字符串 (SQLite TEXT 列不能直接绑 datetime)。"""
    return v.isoformat(timespec="seconds") if isinstance(v, datetime) else v


def ensure_schema() -> None:
    # [2026-10-16] 表 DDL 已随 core.db.ensure_strategy_schema 落库 (幂等)
    ensure_strategy_schema(DB_PATH)


def save_run(run_id: str, **kw) -> None:
    """运行记录 upsert (对齐 MySQL 版 ON DUPLICATE KEY UPDATE: 只更新本次传入的列)。"""
    ensure_schema()
    cols = {k: _fmt(v) for k, v in kw.items() if k in _COLS}
    extra = {k: v for k, v in kw.items() if k not in _COLS}
    if extra:                                          # 源表附列打包进 spec_json (与旧值合并)
        prev = q(f"SELECT spec_json FROM {_TABLE} WHERE run_id=%s", (run_id,))
        merged: Dict[str, Any] = {}
        if prev and prev[0].get("spec_json"):
            try:
                merged = json.loads(prev[0]["spec_json"])
            except ValueError:
                merged = {}
        merged.update(extra)
        cols["spec_json"] = json.dumps(merged, ensure_ascii=False, default=str)
    have = q(f"SELECT run_id FROM {_TABLE} WHERE run_id=%s", (run_id,))
    if have:
        if cols:
            sets = ", ".join(f'"{c}"=%s' for c in cols)
            qexec(f'UPDATE "{_TABLE}" SET {sets} WHERE run_id=%s',
                  tuple(cols[c] for c in cols) + (run_id,))
        return
    qinsert(_TABLE, {"run_id": run_id, **cols})


def list_runs(archived: bool = False, limit: int = 50) -> List[Dict[str, Any]]:
    ensure_schema()
    rows = q(f"SELECT run_id, backtest_id, data_env, status, started_at, finished_at, spec_json, "
             f"passed_json, archived FROM {_TABLE} WHERE archived=%s ORDER BY started_at DESC LIMIT %s",
             (1 if archived else 0, limit))
    out: List[Dict[str, Any]] = []
    for r in rows:
        extra: Dict[str, Any] = {}
        if r.get("spec_json"):
            try:
                extra = json.loads(r["spec_json"])
            except ValueError:
                extra = {}
        out.append({**r, **extra, "created_at": r.get("started_at")})
    return out


def set_archived(run_id: str, archived: bool) -> None:
    ensure_schema()
    qexec(f"UPDATE {_TABLE} SET archived=%s WHERE run_id=%s", (1 if archived else 0, run_id))
