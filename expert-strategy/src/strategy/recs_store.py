"""推荐体系的 DB 访问 (SQLite 版) —— 迁移自 scripts/recs/store.py (2026-10-16)。

源文件用 SQLAlchemy + MySQL (scripts/core/db.get_engine); 按迁移约定改成本地 SQLite:
  * fetch / run / run_many 保持源签名 (SQL 用 ":name" 占位符 + dict 参数), 内部把 :name 转成
    core.db 的位置参数 (?), 库文件取 paths.DB_PATH; date/datetime 统一转 ISO 字符串;
  * ensure_schema: 先 core.db.ensure_strategy_schema 建基表 (xcq_strategies /
    xcq_paper_accounts / xcq_paper_nav_daily / promotions / recs_ledger 等), 再用
    core.db.ensure_table 建本模块附加表, 最后 ALTER TABLE 给基表补源表多出的列 (幂等,
    对应源文件 "老表补列" 段);
  * tx(): 显式事务 (BEGIN/COMMIT/ROLLBACK), 对应源 ledger.write_batch 的
    "删旧+写新 同一事务" (get_engine().begin()); 事务内异常向上抛 (不静默吞)。

MySQL 专有语法改写 (调用方 SQL 里): ON DUPLICATE KEY UPDATE -> INSERT OR REPLACE /
ON CONFLICT DO UPDATE, AUTO_INCREMENT -> INTEGER PRIMARY KEY AUTOINCREMENT,
NOW()/CURDATE() -> datetime.now().isoformat()/date.today().isoformat(),
IF(...) -> CASE WHEN ..., information_schema 补列 -> PRAGMA table_info。

差异点 (遗留问题): 源 store.py 特意避开 scripts.core.db.q()/qexec() 的 "出错静默返回 []"
(台账是证据来源, 写失败必须显式抛出); 迁移约定用 core.db q/qexec, 因此 fetch/run 的失败
会记 error 日志并返回 [] / 0, 不再抛异常 —— 仅 tx() 事务内仍会抛。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

from core import db as _db
from paths import DB_PATH

# ── 附加表 (源 scripts/recs/ddl.py 的 SQLite 化; 只含被迁移代码用到的表) ──────────
_EXTRA_TABLES: Dict[str, str] = {
    # 推荐台账: 盘前锁定, 之后不可修改。valid_forward=1 才计入前向证据
    "xcq_recommendations": """
        CREATE TABLE IF NOT EXISTS xcq_recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_date TEXT NOT NULL,
            pred_date TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            strategy_version INTEGER NOT NULL DEFAULT 1,
            code TEXT NOT NULL,
            name TEXT DEFAULT NULL,
            action TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            reason_text TEXT DEFAULT NULL,
            score REAL DEFAULT NULL,
            rank_no INTEGER DEFAULT NULL,
            target_weight REAL DEFAULT NULL,
            stop_pct REAL DEFAULT NULL,
            take_profit_pct REAL DEFAULT NULL,
            pool_ref TEXT DEFAULT NULL,
            meta_json TEXT DEFAULT NULL,
            generated_at TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            valid_forward INTEGER NOT NULL DEFAULT 0,
            backfilled INTEGER NOT NULL DEFAULT 0,
            UNIQUE (pred_date, strategy_id, code, action)
        )""",
    # 生成记录: 区分 "跑了但 0 条推荐" 与 "当天没跑/失败"
    "xcq_rec_runs": """
        CREATE TABLE IF NOT EXISTS xcq_rec_runs (
            pred_date TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            status TEXT NOT NULL,
            n_recs INTEGER NOT NULL DEFAULT 0,
            valid_forward INTEGER NOT NULL DEFAULT 0,
            batch_hash TEXT DEFAULT NULL,
            note TEXT DEFAULT NULL,
            generated_at TEXT NOT NULL,
            PRIMARY KEY (pred_date, strategy_id)
        )""",
    # 等级/事件流水
    "xcq_strategy_tier_log": """
        CREATE TABLE IF NOT EXISTS xcq_strategy_tier_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id TEXT NOT NULL,
            from_tier TEXT DEFAULT NULL,
            to_tier TEXT NOT NULL,
            reason TEXT DEFAULT NULL,
            actor TEXT NOT NULL DEFAULT 'auto',
            at TEXT NOT NULL
        )""",
    # 通知发件箱 (recs_misc.notify.enqueue 用; webhook 发送 flush 未迁移)
    "xcq_notify_outbox": """
        CREATE TABLE IF NOT EXISTS xcq_notify_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL,
            title TEXT DEFAULT NULL,
            content_md TEXT NOT NULL,
            payload_json TEXT DEFAULT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            tries INTEGER NOT NULL DEFAULT 0,
            last_error TEXT DEFAULT NULL,
            created_at TEXT NOT NULL,
            last_try_at TEXT DEFAULT NULL,
            sent_at TEXT DEFAULT NULL
        )""",
    # 管线配置 (stops.py / accounts.py 的 pipeline 分支读它; 本项目无管线配置, 建空表保持查询链)
    "xcq_pipeline_configs": """
        CREATE TABLE IF NOT EXISTS xcq_pipeline_configs (
            pipeline_name TEXT PRIMARY KEY,
            exit_strategy TEXT DEFAULT NULL,
            extra_params TEXT DEFAULT NULL
        )""",
    # 回测运行 (alpha.runs_stats 读它; 回测运行属并行任务 backtest_v2, 建空表保持查询链)
    "xcq_backtest_runs": """
        CREATE TABLE IF NOT EXISTS xcq_backtest_runs (
            id INTEGER PRIMARY KEY,
            equity_curve TEXT DEFAULT NULL,
            initial_capital REAL DEFAULT NULL
        )""",
}

# ── 基表补列 (源 store.ensure_schema 的 "老表补列" 段; SQLite 用 PRAGMA table_info) ──
_ADD_COLUMNS: Dict[str, List[str]] = {
    "xcq_strategies": [
        "name TEXT", "spec_hash TEXT", "tier_updated_at TEXT", "backtestable INTEGER DEFAULT 0",
        "feed_enabled INTEGER DEFAULT 1", "eval_horizon INTEGER DEFAULT 5", "lifecycle_ref TEXT",
        "evidence_since TEXT", "live_approved_at TEXT", "live_cap_pct REAL", "description TEXT",
    ],
    "xcq_paper_accounts": [
        "signal_source TEXT", "stop_loss_pct REAL", "bound_at TEXT", "bound_note TEXT",
        "exit_policy_version INTEGER DEFAULT 1",
    ],
}

_SCHEMA_READY = False


def ensure_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    _db.ensure_strategy_schema(DB_PATH)
    for name, ddl in _EXTRA_TABLES.items():
        _db.ensure_table(DB_PATH, name, ddl)
    conn = _db.connect(DB_PATH)
    try:
        for table, cols in _ADD_COLUMNS.items():
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for col in cols:
                cname = col.split()[0]
                if cname not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
    finally:
        conn.close()
    _SCHEMA_READY = True


# ── :name 占位符 -> ? 位置参数 (跳过单引号字符串字面量) ─────────────────────────
def _adapt(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


def _to_positional(sql: str, params: Optional[Dict[str, Any]]) -> tuple[str, tuple]:
    if not params:
        return sql, ()
    out: List[str] = []
    vals: List[Any] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":                       # 字符串字面量原样拷贝 (含 '' 转义)
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append(sql[i:j + 1])
            i = j + 1
            continue
        if ch == ":" and i + 1 < n and (sql[i + 1].isalpha() or sql[i + 1] == "_"):
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            name = sql[i + 1:j]
            if name in params:
                out.append("?")
                vals.append(_adapt(params[name]))
                i = j
                continue
        out.append(ch)
        i += 1
    return "".join(out), tuple(vals)


def fetch(sql: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """SELECT -> list[dict]。对应源 store.fetch (SQLAlchemy text + 命名参数)。"""
    s, vals = _to_positional(sql, params)
    return _db.q(s, vals, DB_PATH)


def run(sql: str, params: Optional[Dict[str, Any]] = None) -> int:
    """写操作 -> 受影响行数。对应源 store.run。"""
    s, vals = _to_positional(sql, params)
    return _db.qexec(s, vals, DB_PATH)


def run_many(sql: str, rows: Iterable[Dict[str, Any]]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    conn = _db.connect(DB_PATH)
    try:
        with conn:
            n = 0
            for r in rows:
                ss, vals = _to_positional(sql, r)
                cur = conn.execute(ss, vals)
                n += cur.rowcount if cur.rowcount is not None else 0
            return n
    finally:
        conn.close()


@contextmanager
def tx():
    """显式事务 (BEGIN ... COMMIT / ROLLBACK); 对应源 get_engine().begin()。"""
    conn = _db.connect(DB_PATH)
    try:
        conn.execute("BEGIN")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
