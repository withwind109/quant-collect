"""跟随高手 v3 的两张表: 回测运行记录 + 实盘晋级状态 —— 迁移自 scripts/expert_v3/store.py (2026-10-16)。

存储迁移 MySQL -> 本地 SQLite:
  * 回测运行表: 与 core.db 预建的 expert_follow_runs 结构不同, 按迁移约定用 core.db.ensure_table
    自建 xcq_expert_v3_runs (列同源表); save_run 的 ON DUPLICATE KEY UPDATE 按规则改写为
    INSERT OR REPLACE (差异: 未提供的列会被重置为默认值, 而 MySQL 只更新给出的列);
    created_at 源由 MySQL CURRENT_TIMESTAMP 自动填, 这里 save_run 未显式给时补 datetime.now();
  * 实盘晋级状态: 改用 core.db 已建的 promotions 表 (strategy_id PK / state / capital / max_dd_pct /
    note / requested_at / decided_at / history_json), 不再建 xcq_live_promotions;
    源表多出的 account_id / peak_value / value_now / approved_at / halted_at / halt_reason 等
    由 strategy.promotion 存进 history_json (见 promotion.py docstring);
  * ensure_schema 同时保证 recs_store 的基表补列已就绪 (promotion.request 要按
    xcq_paper_accounts.exit_policy_version 查并行账户), 并建空的 xcq_expert_watchlist
    (关注列表数据未迁移, 保持 v3_screen_rank.run 的查询链);
  * :name 占位符由 strategy.recs_store 转 ? (对应源 S.fetch/S.run)。
"""
from __future__ import annotations

from datetime import datetime

from core import db as _db
from paths import DB_PATH
from strategy import recs_store as S

_RUNS_DDL = """
    CREATE TABLE IF NOT EXISTS xcq_expert_v3_runs (
        run_id TEXT PRIMARY KEY,
        spec_hash TEXT, spec_json TEXT, data_end TEXT, test_start TEXT,
        status TEXT, passed_json TEXT, brief_json TEXT, error TEXT,
        created_at TEXT DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%S','NOW','localtime'))
    )"""

_WATCH_DDL = """
    CREATE TABLE IF NOT EXISTS xcq_expert_watchlist (
        expert_id TEXT PRIMARY KEY,
        name TEXT,
        source TEXT,
        market_type TEXT
    )"""

_DONE = False


def ensure_schema() -> None:
    global _DONE
    if _DONE:
        return
    S.ensure_schema()                                  # 基表 + 补列 (promotions 也在内)
    _db.ensure_table(DB_PATH, "xcq_expert_v3_runs", _RUNS_DDL)
    _db.ensure_table(DB_PATH, "xcq_expert_watchlist", _WATCH_DDL)
    _DONE = True


def save_run(run_id: str, **kw) -> None:
    ensure_schema()
    kw = dict(kw)
    kw.setdefault("created_at", datetime.now().isoformat())   # 源: MySQL CURRENT_TIMESTAMP
    cols = ["run_id"] + list(kw)
    ph = ",".join(f":{c}" for c in cols)
    S.run(f"INSERT OR REPLACE INTO xcq_expert_v3_runs ({','.join(cols)}) VALUES ({ph})",
          {"run_id": run_id, **kw})


def list_runs(limit: int = 30):
    ensure_schema()
    return S.fetch("SELECT run_id, spec_hash, spec_json, data_end, test_start, status, passed_json, error, created_at "
                   "FROM xcq_expert_v3_runs ORDER BY created_at DESC LIMIT :n", {"n": limit})
