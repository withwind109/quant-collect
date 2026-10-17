"""爬取结果入库（本地 SQLite）——按交易日落库，幂等可重跑，支持历史多日补齐。

从 xcq-platform 的 MySQL db_writer 迁移（2026-10-16）：
  * 数据库由远端 MySQL 换成本地 SQLite 单文件（默认 data/experts.db），
    可随 git 提交、零依赖（Python 标准库 sqlite3）、WAL 模式下读写性能足够；
  * 7 张爬取表与产出 JSON 一一对应（去掉 xcq_ 前缀）：
      experts.json       -> crawl_expert      (高手榜)
      trades.json        -> crawl_trade       (成交明细，含买卖)
      expert_buys.json   -> crawl_expert_buy  (高手×个股 主视图，已扁平)
      buy_aggregated.json-> crawl_aggregate   (个股买入聚合)
      stock_rank.json    -> crawl_stock_rank  (东财全量热度榜)
      signals.json       -> crawl_signal      (无标的的公开动态)
      meta.json          -> crawl_meta        (每日覆盖度/投票，一日期一行)
  * 写入语义与 MySQL 版一致：源级表按 (trade_date, source) 先 DELETE 后 INSERT
    （当日快照替换，重爬幂等）；派生表按 trade_date 先 DELETE 后 INSERT。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger


# ── 表定义（列名与 MySQL 版一致，JSON 列用 TEXT 存 json 字符串） ────────────
TABLES: Dict[str, List[str]] = {
    "expert": ["trade_date", "source", "asset_type", "channel", "expert_id", "name", "owner",
               "rank_label", "followers", "return_rate", "total_return", "extra_json", "market_type"],
    "trade": ["trade_date", "source", "asset_type", "channel", "expert_id", "expert_name", "owner",
              "stock_code", "stock_name", "market", "action", "price", "volume", "amount",
              "weight_before", "weight_after", "weight_label", "return_rate", "day_return_rate",
              "trade_time", "raw_json", "market_type", "row_sig"],
    "aggregate": ["trade_date", "stock_code", "asset_type", "stock_name", "buy_experts", "buy_records",
                  "sources_json", "avg_price", "amount", "latest_time", "net_amount", "note"],
    "expert_buy": ["trade_date", "source", "asset_type", "channel", "expert_id", "expert_name", "owner",
                   "rank_label", "return_rate", "day_return_rate", "followers", "buy_count", "stock_code",
                   "stock_name", "action", "price", "volume", "amount", "weight_before", "weight_after",
                   "trade_time", "market_type", "row_sig"],
    "stock_rank": ["trade_date", "stock_code", "asset_type", "stock_name", "buy_experts", "buy_records",
                   "sources_json", "avg_price", "amount", "latest_time", "net_amount", "note"],
    "signal": ["trade_date", "source", "kind", "expert_id", "expert_name", "action", "stock_code",
               "stock_name", "trade_time", "note", "row_sig"],
    "meta": ["trade_date", "date_mode", "date_votes_json", "coverage_json", "crawled_at"],
}

#: 唯一键（trade_date 之后的自然键）；meta 一日期一行
UNIQUE_KEYS: Dict[str, List[str]] = {
    "expert": ["trade_date", "source", "expert_id"],
    "trade": ["trade_date", "row_sig"],
    "aggregate": ["trade_date", "stock_code"],
    "expert_buy": ["trade_date", "row_sig"],
    "stock_rank": ["trade_date", "stock_code"],
    "signal": ["trade_date", "row_sig"],
    "meta": ["trade_date"],
}

#: 源级表（按 source 维度替换）与派生表（按日期整体替换）
SOURCE_TABLES = ("expert", "trade", "expert_buy", "signal")
DERIVED_TABLES = ("aggregate", "stock_rank", "meta")

_LOCAL = threading.local()


def _table_name(suffix: str) -> str:
    return f"crawl_{suffix}"


def _build_ddl() -> List[str]:
    out = []
    for suffix, cols in TABLES.items():
        tname = _table_name(suffix)
        col_defs = [f'"{c}" TEXT' for c in cols]
        uk = ", ".join(f'"{c}"' for c in UNIQUE_KEYS[suffix])
        out.append(
            f'CREATE TABLE IF NOT EXISTS "{tname}" ('
            + ", ".join(col_defs)
            + f', UNIQUE ({uk}))'
        )
    return out


# ── 连接管理（线程本地连接 + WAL） ─────────────────────────────────────────
def connect(db_path: Path | str) -> sqlite3.Connection:
    """打开（并初始化）SQLite 连接。WAL + busy_timeout 提升并发读写性能。"""
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30, isolation_level=None)  # autocommit; 事务显式开启
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def ensure_schema(db_path: Path | str) -> bool:
    try:
        conn = connect(db_path)
        try:
            with conn:
                for ddl in _build_ddl():
                    conn.execute(ddl)
        finally:
            conn.close()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[db] SQLite 建表失败 {db_path}: {exc}")
        return False


@contextmanager
def _conn(db_path: Path | str):
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


# ── 行构造（与 MySQL 版 _rows_* 对齐） ─────────────────────────────────────
def _mt(r: dict) -> str:
    mt = r.get("market_type")
    return mt if mt in ("live", "sim") else ""


def _sig(*parts) -> str:
    return hashlib.md5("␟".join("" if p is None else str(p) for p in parts).encode()).hexdigest()


def _j(obj) -> Optional[str]:
    return None if obj is None else json.dumps(obj, ensure_ascii=False, default=str)


def _f(v):
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def _i(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _rows_expert(records: List[dict], trade_date: str) -> List[dict]:
    out = []
    for r in records:
        out.append({
            "trade_date": trade_date, "source": r.get("source"), "asset_type": r.get("asset_type"),
            "channel": r.get("channel"), "expert_id": r.get("expert_id"), "name": r.get("name"),
            "owner": r.get("owner"), "rank_label": r.get("rank_label"), "followers": _i(r.get("followers")),
            "return_rate": _f(r.get("return_rate")), "total_return": _f(r.get("total_return")),
            "extra_json": _j(r.get("extra")), "market_type": _mt(r),
        })
    return out


def _rows_trade(records: List[dict], trade_date: str) -> List[dict]:
    out = []
    for r in records:
        out.append({
            "trade_date": trade_date, "source": r.get("source"), "asset_type": r.get("asset_type"),
            "channel": r.get("channel"), "expert_id": r.get("expert_id"), "expert_name": r.get("expert_name"),
            "owner": r.get("owner"), "stock_code": r.get("stock_code"), "stock_name": r.get("stock_name"),
            "market": r.get("market"), "action": r.get("action"), "price": _f(r.get("price")),
            "volume": _f(r.get("volume")), "amount": _f(r.get("amount")),
            "weight_before": _f(r.get("weight_before")), "weight_after": _f(r.get("weight_after")),
            "weight_label": (str(r.get("weight_label") or "").strip() or None),
            "return_rate": _f(r.get("return_rate")), "day_return_rate": _f(r.get("day_return_rate")),
            "trade_time": r.get("trade_time"), "raw_json": _j(r.get("raw")), "market_type": _mt(r),
            "row_sig": _sig(trade_date, r.get("source"), r.get("expert_id"), r.get("stock_code"),
                            r.get("action"), r.get("trade_time"), r.get("price"),
                            r.get("weight_before"), r.get("weight_after")),
        })
    return out


def _rows_aggregate(records: List[dict], trade_date: str) -> List[dict]:
    out = []
    for r in records:
        out.append({
            "trade_date": trade_date, "stock_code": r.get("stock_code"), "asset_type": r.get("asset_type"),
            "stock_name": r.get("stock_name"), "buy_experts": _i(r.get("buy_experts")),
            "buy_records": _i(r.get("buy_records")), "sources_json": _j(r.get("sources")),
            "avg_price": _f(r.get("avg_price")), "amount": _f(r.get("amount")),
            "latest_time": r.get("latest_time"), "net_amount": _f(r.get("net_amount")), "note": r.get("note"),
        })
    return out


def _rows_expert_buy(records: List[dict], trade_date: str) -> List[dict]:
    """expert_buys.json 是「高手 → stocks[]」结构，落库前扁平成一高手一股一行。"""
    out = []
    for e in records:
        for s in e.get("stocks") or []:
            out.append({
                "trade_date": trade_date, "source": e.get("source"), "asset_type": e.get("asset_type"),
                "channel": e.get("channel"), "expert_id": e.get("expert_id"),
                "expert_name": e.get("expert_name"), "owner": e.get("owner"),
                "rank_label": e.get("rank_label"), "return_rate": _f(e.get("return_rate")),
                "day_return_rate": _f(e.get("day_return_rate")), "followers": _i(e.get("followers")),
                "buy_count": _i(e.get("buy_count")), "stock_code": s.get("stock_code"),
                "stock_name": s.get("stock_name"), "action": s.get("action"), "price": _f(s.get("price")),
                "volume": _f(s.get("volume")), "amount": _f(s.get("amount")),
                "weight_before": _f(s.get("weight_before")), "weight_after": _f(s.get("weight_after")),
                "trade_time": s.get("trade_time"), "market_type": _mt(e),
                "row_sig": _sig(trade_date, e.get("source"), e.get("expert_id"), s.get("stock_code"),
                                s.get("action"), s.get("trade_time"), s.get("price"),
                                s.get("weight_before"), s.get("weight_after")),
            })
    return out


def _rows_signal(records: List[dict], trade_date: str) -> List[dict]:
    out = []
    for r in records:
        out.append({
            "trade_date": trade_date, "source": r.get("source"), "kind": r.get("kind"),
            "expert_id": r.get("expert_id"), "expert_name": r.get("expert_name"),
            "action": r.get("action"), "stock_code": r.get("stock_code"), "stock_name": r.get("stock_name"),
            "trade_time": r.get("trade_time"), "note": r.get("note"),
            "row_sig": _sig(trade_date, r.get("source"), r.get("expert_id"), r.get("stock_code"),
                            r.get("action"), r.get("trade_time")),
        })
    return out


def _row_meta(meta: dict, trade_date: str, crawled_at: Optional[str]) -> dict:
    return {
        "trade_date": trade_date, "date_mode": meta.get("date_mode"),
        "date_votes_json": _j(meta.get("date_votes")), "coverage_json": _j(meta.get("coverage")),
        "crawled_at": crawled_at or meta.get("crawled_at") or "",
    }


# ── 写入 ───────────────────────────────────────────────────────────────────
def _delete_and_insert(conn: sqlite3.Connection, tname: str, rows: List[dict],
                       where: str, params: tuple) -> int:
    if not rows:
        return 0
    cols = list(TABLES[tname.removeprefix("crawl_")])
    ph = ",".join("?" for _ in cols)
    collist = ",".join(f'"{c}"' for c in cols)
    conn.execute(f'DELETE FROM "{tname}" WHERE {where}', params)
    conn.executemany(
        f'INSERT OR REPLACE INTO "{tname}" ({collist}) VALUES ({ph})',
        [tuple(r.get(c) for c in cols) for r in rows],
    )
    return len(rows)


def write_day(trade_date: str, day_dir: Path, db_path: Path | str,
              crawled_at: Optional[str] = None) -> Dict[str, int]:
    """把某一交易日的爬取产出（day_dir 下的 JSON）写入 SQLite（替换式，幂等）。"""
    def _load(name: str):
        fp = day_dir / name
        if not fp.exists():
            return []
        try:
            return json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning(f"[db] 读取 {fp.name} 失败: {exc}")
            return []

    experts = _load("experts.json")
    trades = _load("trades.json")
    buys = _load("buys.json")
    agg = _load("buy_aggregated.json")
    stock_rank = _load("stock_rank.json")
    signals = _load("signals.json")
    expert_buys = _load("expert_buys.json")
    meta = _load("meta.json") or {}

    # 源级表按 (trade_date, source) 替换；先按出现的 source 删
    sources = sorted({str(r.get("source")) for r in trades if r.get("source")}
                     | {str(r.get("source")) for r in buys if r.get("source")})
    res: Dict[str, int] = {}
    with _conn(db_path) as conn, conn:
        for tname, rows in (
            ("crawl_expert", _rows_expert(experts, trade_date)),
            ("crawl_trade", _rows_trade(trades, trade_date)),
            ("crawl_expert_buy", _rows_expert_buy(expert_buys, trade_date)),
            ("crawl_signal", _rows_signal(signals, trade_date)),
        ):
            if tname == "crawl_expert":
                n = _delete_and_insert(conn, tname, rows, "trade_date=?", (trade_date,))
            else:
                # 源级: 按本次抓到的 source 集合删（未抓的源不动，支持 --sources 部分爬取）
                n = 0
                if rows:
                    for src in sources or [""]:
                        n += _delete_and_insert(
                            conn, tname, [r for r in rows if (r.get("source") or "") == src],
                            "trade_date=? AND source=?", (trade_date, src))
            res[tname.removeprefix("crawl_")] = n
        # 派生表: 按 trade_date 整体替换
        for tname, rows in (
            ("crawl_aggregate", _rows_aggregate(agg, trade_date)),
            ("crawl_stock_rank", _rows_aggregate(stock_rank, trade_date)),
            ("crawl_meta", [_row_meta(meta, trade_date, crawled_at)]),
        ):
            res[tname.removeprefix("crawl_")] = _delete_and_insert(
                conn, tname, rows, "trade_date=?", (trade_date,))
    logger.info(f"[db] {trade_date} 写入 SQLite: " + ", ".join(f"{k}={v}" for k, v in res.items()))
    return res


def backfill_all(data_dir: Path, db_path: Path | str) -> Dict[str, Any]:
    """扫描 data/<YYYY-MM-DD>/ 下所有日期目录，逐日写入（幂等）。"""
    synced, skipped, totals = 0, 0, {}
    for day in sorted(p for p in data_dir.iterdir() if p.is_dir() and (p / "expert_buys.json").exists()):
        try:
            res = write_day(day.name, day, db_path)
            synced += 1
            for k, v in res.items():
                totals[k] = totals.get(k, 0) + v
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[db] 补齐 {day.name} 失败: {exc}")
            skipped += 1
    return {"synced": synced, "skipped": skipped, "totals": totals}


def list_dates(db_path: Path | str) -> List[str]:
    """已有数据的交易日（倒序）。"""
    try:
        with _conn(db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT trade_date FROM crawl_meta ORDER BY trade_date DESC").fetchall()
        return [r[0] for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[db] 读取日期列表失败: {exc}")
        return []


# ── 读取（按「入库前的 JSON 形状」还原，前端无感） ──────────────────────────
def _jloads(v):
    if v in (None, ""):
        return None
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return None


def read_day(trade_date: str, db_path: Path | str) -> Optional[dict]:
    """读取某日的展示数据；该日无 meta 行返回 None。"""
    try:
        with _conn(db_path) as conn:
            meta = conn.execute(
                "SELECT * FROM crawl_meta WHERE trade_date=?", (trade_date,)).fetchone()
            if meta is None:
                return None

            def _q(sql: str) -> List[dict]:
                return [dict(r) for r in conn.execute(sql, (trade_date,)).fetchall()]

            experts = _q("SELECT * FROM crawl_expert WHERE trade_date=?")
            trades = _q("SELECT * FROM crawl_trade WHERE trade_date=?")
            agg = _q("SELECT * FROM crawl_aggregate WHERE trade_date=?")
            stock_rank = _q("SELECT * FROM crawl_stock_rank WHERE trade_date=?")
            signals = _q("SELECT * FROM crawl_signal WHERE trade_date=?")
            eb_rows = _q("SELECT * FROM crawl_expert_buy WHERE trade_date=?")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[db] 读取 {trade_date} 失败: {exc}")
        return None

    for r in experts:
        r["extra"] = _jloads(r.pop("extra_json", None))
    for r in trades:
        r["raw"] = _jloads(r.pop("raw_json", None))
    for r in agg + stock_rank:
        r["sources"] = _jloads(r.pop("sources_json", None))

    # expert_buy 行 → 按 (source, expert_id) 分组还原成「高手 → stocks[]」
    grouped: Dict[tuple, dict] = {}
    for r in eb_rows:
        key = (r.get("source"), r.get("expert_id"))
        g = grouped.get(key)
        if g is None:
            g = {"source": r.get("source"), "asset_type": r.get("asset_type"), "channel": r.get("channel"),
                 "expert_id": r.get("expert_id"), "expert_name": r.get("expert_name"),
                 "owner": r.get("owner"), "rank_label": r.get("rank_label"),
                 "market_type": r.get("market_type"), "return_rate": r.get("return_rate"),
                 "day_return_rate": r.get("day_return_rate"), "followers": r.get("followers"),
                 "buy_count": r.get("buy_count"), "stocks": []}
            grouped[key] = g
        g["stocks"].append({k: r.get(k) for k in
                            ("stock_code", "stock_name", "action", "price", "volume", "amount",
                             "weight_before", "weight_after", "trade_time")})

    meta_out = {
        "trade_date": trade_date,
        "date_mode": meta["date_mode"],
        "date_votes": _jloads(meta["date_votes_json"]) or [],
        "coverage": _jloads(meta["coverage_json"]) or [],
        "crawled_at": meta["crawled_at"] or "",
    }
    return {
        "experts": experts, "expert_buys": list(grouped.values()), "buys": trades,
        "buy_aggregated": agg, "stock_rank": stock_rank, "signals": signals, "meta": meta_out,
    }


# ══════════════════════════════════════════════════════════════════════════
#  策略层（跟随高手）表结构 —— 对应 xcq-platform MySQL 的 xcq_* 表
# ══════════════════════════════════════════════════════════════════════════

STRATEGY_TABLES: Dict[str, str] = {
    # 策略注册（对应 xcq_strategies）
    "xcq_strategies": """
        CREATE TABLE IF NOT EXISTS xcq_strategies (
            strategy_id TEXT PRIMARY KEY,
            kind TEXT,
            tier TEXT DEFAULT 'E0',
            status TEXT DEFAULT 'active',
            version INTEGER DEFAULT 1,
            live_approved INTEGER DEFAULT 0,
            spec_json TEXT,
            created_at TEXT
        )""",
    # 模拟账户（对应 xcq_paper_accounts）
    "xcq_paper_accounts": """
        CREATE TABLE IF NOT EXISTS xcq_paper_accounts (
            id TEXT PRIMARY KEY,
            strategy_id TEXT,
            name TEXT,
            initial_capital REAL DEFAULT 1000000,
            cash REAL DEFAULT 1000000,
            enabled INTEGER DEFAULT 1,
            max_positions INTEGER DEFAULT 30,
            created_at TEXT
        )""",
    # 模拟账户每日净值（对应 xcq_paper_nav_daily）
    "xcq_paper_nav_daily": """
        CREATE TABLE IF NOT EXISTS xcq_paper_nav_daily (
            account_id TEXT,
            trade_date TEXT,
            total_nav REAL,
            cum_return_pct REAL,
            cash REAL,
            positions_json TEXT,
            note TEXT,
            PRIMARY KEY (account_id, trade_date)
        )""",
    # 统一回测运行记录（对应 expert_follow 的 MySQL runs 表）
    "expert_follow_runs": """
        CREATE TABLE IF NOT EXISTS expert_follow_runs (
            run_id TEXT PRIMARY KEY,
            backtest_id TEXT,
            data_env TEXT,
            status TEXT DEFAULT 'running',
            started_at TEXT,
            finished_at TEXT,
            spec_json TEXT,
            summary_json TEXT,
            passed_json TEXT,
            archived INTEGER DEFAULT 0
        )""",
    # 正式交易晋级申请（对应 promotion 的 MySQL 表）
    "promotions": """
        CREATE TABLE IF NOT EXISTS promotions (
            strategy_id TEXT PRIMARY KEY,
            state TEXT DEFAULT 'none',
            capital REAL,
            max_dd_pct REAL,
            note TEXT,
            requested_at TEXT,
            decided_at TEXT,
            history_json TEXT
        )""",
    # 推荐台账（recs ledger，Rec 记录）
    "recs_ledger": """
        CREATE TABLE IF NOT EXISTS recs_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rec_date TEXT,
            strategy_id TEXT,
            code TEXT,
            action TEXT,
            target_weight REAL,
            price REAL,
            status TEXT DEFAULT 'pending',
            meta_json TEXT,
            created_at TEXT
        )""",
    # 关注高手列表（对应 xcq_expert_watchlist）
    "xcq_expert_watchlist": """
        CREATE TABLE IF NOT EXISTS xcq_expert_watchlist (
            expert_id TEXT,
            source TEXT DEFAULT 'eastmoney',
            market_type TEXT DEFAULT 'sim',
            name TEXT,
            note TEXT,
            created_at TEXT,
            PRIMARY KEY (expert_id, source, market_type)
        )""",
}

#: 实盘流水视图（pit_nav live 环境从 MySQL xcq_expert_trades 读，这里用爬取表的视图替代）
EXPERT_TRADES_VIEW = """
    CREATE VIEW IF NOT EXISTS expert_trades AS
    SELECT expert_id, expert_name, trade_date, trade_time, stock_code, stock_name,
           action, price, weight_before, weight_after, weight_label, volume, market_type
    FROM crawl_trade
    WHERE market_type = 'live'
"""


def ensure_strategy_schema(db_path: Path | str) -> bool:
    """建策略层表 + expert_trades 视图（幂等）。"""
    try:
        conn = connect(db_path)
        try:
            with conn:
                for ddl in STRATEGY_TABLES.values():
                    conn.execute(ddl)
                conn.execute(EXPERT_TRADES_VIEW)
        finally:
            conn.close()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[db] 策略层建表失败 {db_path}: {exc}")
        return False


def ensure_table(db_path: Path | str, name: str, ddl: str) -> bool:
    """其它模块自定义建表（幂等）。"""
    try:
        conn = connect(db_path)
        try:
            with conn:
                conn.execute(ddl)
        finally:
            conn.close()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[db] 建表 {name} 失败: {exc}")
        return False


# ── MySQL 兼容查询助手（源代码迁移自 scripts.core.db 的 q/qexec） ──────────
def _to_sqlite_sql(sql: str) -> str:
    """MySQL 占位符 %s → sqlite ?；其余 SQL 语法两库通用（本项目的用法）。"""
    return sql.replace("%s", "?")


def q(sql: str, params: Optional[tuple | list] = None, db_path: Path | str | None = None) -> List[dict]:
    """查询，返回 list[dict]（对齐 MySQL 版 q 的语义）。

    db_path 为空时用 paths.DB_PATH；SQL 用 MySQL 风格 %s 占位符（内部转换）。
    """
    import paths as _P

    path = Path(db_path) if db_path else _P.DB_PATH
    try:
        with _conn(path) as conn:
            rows = conn.execute(_to_sqlite_sql(sql), tuple(params or ())).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[db] 查询失败: {exc}\n  SQL: {sql[:200]}")
        return []


def qone(sql: str, params: Optional[tuple | list] = None, db_path: Path | str | None = None) -> Optional[dict]:
    rows = q(sql, params, db_path)
    return rows[0] if rows else None


def qexec(sql: str, params: Optional[tuple | list] = None, db_path: Path | str | None = None) -> int:
    """执行写操作（INSERT/UPDATE/DELETE），返回受影响行数。"""
    import paths as _P

    path = Path(db_path) if db_path else _P.DB_PATH
    try:
        conn = connect(path)
        try:
            with conn:
                cur = conn.execute(_to_sqlite_sql(sql), tuple(params or ()))
            return cur.rowcount if cur.rowcount is not None else 0
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[db] 执行失败: {exc}\n  SQL: {sql[:200]}")
        return 0


def qinsert(table: str, row: dict, db_path: Path | str | None = None) -> int:
    """按字典插入一行（列名取自字典键）。"""
    cols = list(row.keys())
    collist = ", ".join(f'"{c}"' for c in cols)
    ph = ", ".join("?" for _ in cols)
    return qexec(f'INSERT OR REPLACE INTO "{table}" ({collist}) VALUES ({ph})',
                 tuple(row.get(c) for c in cols), db_path)
