"""行情价格库（本地 SQLite + akshare 兜底下载）—— 替代 xcq-platform 的 qlib bin 直读。

背景：xcq-platform 的策略引擎（pit_nav.PriceCtx / stock_graph.data.load_panel）直接读
qlib 的二进制特征文件，迁移到独立项目后不引入 qlib 重量级依赖，改为：
  * 本地 SQLite 单文件（data/prices.db，可随 git 提交，WAL 模式性能足够）；
  * 缺数据时用 akshare 从东方财富补齐（后复权价 + 原始价都存，复权因子加载时现算）；
  * 接口与 qlib 版对齐：load_panel() 返回 Panel(dates, codes, f{adjclose/close/factor})。

表结构：
  price_daily(code, date, open, high, low, close, hfq_close, volume, amount)
  index_daily(code, date, close)          —— 指数（如 000300.SH 沪深300）
  calendar(date)                          —— 交易日历
"""

from __future__ import annotations

import sqlite3
import threading
from collections import namedtuple
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np

from core.logger import logger
from paths import PRICE_DB_PATH

#: 与 qlib 版对齐的面板结构
Panel = namedtuple("Panel", "dates codes f")

_DEFAULT_FIELDS = ("adjclose", "close", "factor")

_DDL = [
    """CREATE TABLE IF NOT EXISTS price_daily (
        code TEXT NOT NULL, date TEXT NOT NULL,
        open REAL, high REAL, low REAL, close REAL, hfq_close REAL,
        volume REAL, amount REAL,
        PRIMARY KEY (code, date))""",
    """CREATE TABLE IF NOT EXISTS index_daily (
        code TEXT NOT NULL, date TEXT NOT NULL, close REAL,
        PRIMARY KEY (code, date))""",
    """CREATE TABLE IF NOT EXISTS calendar (
        date TEXT PRIMARY KEY)""",
    "CREATE INDEX IF NOT EXISTS idx_price_daily_code ON price_daily(code, date)",
]

_LOCAL = threading.local()


def price_db_path() -> Path:
    return PRICE_DB_PATH


def connect() -> sqlite3.Connection:
    conn = getattr(_LOCAL, "conn", None)
    if conn is None:
        PRICE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(PRICE_DB_PATH), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        _LOCAL.conn = conn
    return conn


def ensure_schema() -> None:
    conn = connect()
    with conn:
        for ddl in _DDL:
            conn.execute(ddl)


# ── 代码格式 ────────────────────────────────────────────────────────────────

def qlib_inst(code: str) -> str:
    """裸代码 → 交易所前缀（与 pit_nav.qlib_inst 同规则）。"""
    c = str(code)
    if c.startswith(("6", "5", "9")) and not c.startswith("92"):
        return "sh" + c
    if c.startswith(("4", "8", "92")):
        return "bj" + c
    return "sz" + c


def akshare_symbol(code: str) -> str:
    """裸代码 / 000300.SH → akshare 代码（sh600000 / sh000300）。"""
    c = str(code)
    if "." in c:  # 000300.SH → sh000300
        base, exch = c.split(".", 1)
        return exch.lower() + base
    return qlib_inst(c)


def _norm_code(code: str) -> str:
    """统一存裸代码（去 sh/sz/bj 前缀与 .SH 后缀）。"""
    c = str(code).strip()
    if "." in c:
        c = c.split(".", 1)[0]
    if c[:2].lower() in ("sh", "sz", "bj") and len(c) > 2:
        c = c[2:]
    return c


# ── 交易日历 ────────────────────────────────────────────────────────────────

def trading_calendar(start: str = "2015-01-01", end: Optional[str] = None) -> List[str]:
    """交易日历（SQLite calendar 表；空则用 akshare 初始化）。"""
    ensure_schema()
    conn = connect()
    n = conn.execute("SELECT COUNT(*) c FROM calendar").fetchone()["c"]
    if n == 0:
        _init_calendar()
    sql = "SELECT date FROM calendar WHERE date >= ?"
    params: list = [start]
    if end:
        sql += " AND date <= ?"
        params.append(end)
    sql += " ORDER BY date"
    return [r["date"] for r in conn.execute(sql, params).fetchall()]


def _init_calendar() -> None:
    import akshare as ak

    df = ak.tool_trade_date_hist_sina()
    dates = [str(d)[:10] for d in df["trade_date"].tolist()]
    conn = connect()
    with conn:
        conn.executemany("INSERT OR REPLACE INTO calendar(date) VALUES (?)", [(d,) for d in dates])
    logger.info(f"[prices] 交易日历初始化: {len(dates)} 天 ({dates[0]} ~ {dates[-1]})")


# ── 写入 ────────────────────────────────────────────────────────────────────

def upsert_daily(code: str, rows: Sequence[dict]) -> int:
    """rows: [{date, open, high, low, close, hfq_close, volume, amount}]"""
    if not rows:
        return 0
    conn = connect()
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO price_daily"
            "(code, date, open, high, low, close, hfq_close, volume, amount)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            [(code, r["date"], r.get("open"), r.get("high"), r.get("low"),
              r.get("close"), r.get("hfq_close"), r.get("volume"), r.get("amount")) for r in rows],
        )
    return len(rows)


def upsert_index(code: str, rows: Sequence[dict]) -> int:
    if not rows:
        return 0
    conn = connect()
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO index_daily(code, date, close) VALUES (?,?,?)",
            [(code, r["date"], r.get("close")) for r in rows],
        )
    return len(rows)


def download_stock(code: str, start: str = "20150101", end: Optional[str] = None) -> int:
    """akshare 拉单只股票日线（原始价 + 后复权价）。返回行数。

    akshare 走 push2his.eastmoney.com，被代理/网络阻断时自动切腾讯行情兜底
    （web.ifzq.gtimg.cn fqkline，字段序 [日期,开,收,高,低,量(手)]）。
    """
    n = _download_via_akshare(code, start, end)
    if n:
        return n
    return _download_via_tencent(code, start, end)


def _download_via_akshare(code: str, start: str = "20150101", end: Optional[str] = None) -> int:
    import akshare as ak
    from datetime import datetime

    bare = _norm_code(code)
    end = end or datetime.now().strftime("%Y%m%d")
    out: List[dict] = []
    for adjust, key in (("", "close"), ("hfq", "hfq_close")):
        try:
            df = ak.stock_zh_a_hist(symbol=bare, period="daily", start_date=start,
                                    end_date=end, adjust=adjust)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[prices] 下载 {bare} ({adjust or 'raw'}) 失败: {exc}")
            return 0
        if df is None or df.empty:
            continue
        for _, r in df.iterrows():
            d = str(r["日期"])[:10]
            rec = next((x for x in out if x["date"] == d), None)
            if rec is None:
                rec = {"date": d}
                out.append(rec)
            rec[key] = float(r["收盘"]) if r["收盘"] is not None else None
            if adjust == "":
                rec["open"] = float(r["开盘"]) if r["开盘"] is not None else None
                rec["high"] = float(r["最高"]) if r["最高"] is not None else None
                rec["low"] = float(r["最低"]) if r["最低"] is not None else None
                rec["volume"] = float(r["成交量"]) if r["成交量"] is not None else None
                rec["amount"] = float(r["成交额"]) if r["成交额"] is not None else None
    n = upsert_daily(bare, out)
    if n:
        logger.info(f"[prices] {bare} 下载 {n} 天 (akshare)")
    return n


#: 腾讯行情日线端点（东财 push2his 被代理阻断时的兜底通道，2026-10-16 实测可达）
QTIMG_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


def _qt_prefix(bare: str) -> str:
    if bare.startswith("6"):
        return "sh"
    if bare.startswith(("4", "8", "92")):
        return "bj"
    return "sz"


def _download_via_tencent(code: str, start: str = "20150101", end: Optional[str] = None) -> int:
    """腾讯行情兜底：fqkline 拉原始价 + 后复权价。失败返回 0（不抛异常）。"""
    from datetime import datetime

    from core.http import HttpClient

    bare = _norm_code(code)
    end = end or datetime.now().strftime("%Y%m%d")
    sym = f"{_qt_prefix(bare)}{bare}"
    s = f"{start[:4]}-{start[4:6]}-{start[6:]}"
    e = f"{end[:4]}-{end[4:6]}-{end[6:]}"
    http = HttpClient(min_interval=0.3)
    try:
        out: List[dict] = []
        for fq, key in (("", "close"), ("hfq", "hfq_close")):
            resp = http.get_json(
                QTIMG_KLINE_URL,
                # ⚠️ count 上限 2000（传 3000 服务端返 "param error"）；2000 根日线约覆盖 8 年，
                # 回测窗口（2024-07 起）足够；更早的历史需翻页，当前无此场景。
                params={"param": f"{sym},day,{s},{e},2000,{fq}"},
            )
            node = ((resp or {}).get("data") or {}).get(sym) or {}
            rows = node.get(f"{fq}day" if fq else "day") or []
            if not rows:
                continue
            for r in rows:
                # 腾讯字段序: [日期, 开, 收, 高, 低, 成交量(手)]
                d, o, c, h, l, v = r[0], r[1], r[2], r[3], r[4], r[5]
                rec = next((x for x in out if x["date"] == d), None)
                if rec is None:
                    rec = {"date": d}
                    out.append(rec)
                rec[key] = float(c) if c not in (None, "") else None
                if not fq:
                    rec["open"] = float(o) if o not in (None, "") else None
                    rec["high"] = float(h) if h not in (None, "") else None
                    rec["low"] = float(l) if l not in (None, "") else None
                    rec["volume"] = float(v) * 100 if v not in (None, "") else None  # 手 -> 股
                    rec["amount"] = None
        n = upsert_daily(bare, out)
        if n:
            logger.info(f"[prices] {bare} 下载 {n} 天 (腾讯兜底)")
        return n
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[prices] 腾讯兜底下载 {bare} 失败: {exc}")
        return 0
    finally:
        http.close()


def download_index(code: str = "000300.SH", start: str = "20150101", end: Optional[str] = None) -> int:
    """akshare 拉指数日线（stock_zh_index_daily）。"""
    import akshare as ak
    from datetime import datetime

    end = end or datetime.now().strftime("%Y%m%d")
    sym = akshare_symbol(code)
    try:
        df = ak.stock_zh_index_daily(symbol=sym)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[prices] 下载指数 {sym} 失败: {exc}")
        return 0
    if df is None or df.empty:
        return 0
    rows = [{"date": str(r["date"])[:10], "close": float(r["close"])}
            for _, r in df.iterrows() if start <= str(r["date"])[:10].replace("-", "") <= end]
    n = upsert_index(_norm_code(code) if "." not in code else code, rows)
    if n:
        logger.info(f"[prices] 指数 {code} 下载 {n} 天")
    return n


def ensure_prices(codes: Sequence[str], start: str, end: str,
                  max_download: int = 400) -> Dict[str, int]:
    """确保 codes 在 [start, end] 有本地行情；缺的用 akshare 补（限量防限流）。

    end 按今日截断再判覆盖：调用方常传 9999-12-31 之类的大 end 预热面板，
    不截断会让 `mx < end` 恒真、已缓存的股票每次都被判缺失而重下。
    """
    from datetime import date as _date

    ensure_schema()
    ensure_calendar()
    eff_end = min(end, _date.today().isoformat())   # 行情不可能有未来的
    conn = connect()
    missing: List[str] = []
    for c in dict.fromkeys(_norm_code(x) for x in codes):
        row = conn.execute(
            "SELECT MIN(date) mn, MAX(date) mx, COUNT(*) c FROM price_daily WHERE code=?", (c,)
        ).fetchone()
        if row["c"] == 0 or (row["mn"] or "9") > start or (row["mx"] or "0") < eff_end:
            missing.append(c)
    done: Dict[str, int] = {}
    for i, c in enumerate(missing[:max_download]):
        done[c] = download_stock(c, start="".join(start.split("-")),
                                 end="".join(eff_end.split("-")))
        if len(missing) > max_download and i == max_download - 1:
            logger.warning(f"[prices] 待下载 {len(missing)} 只超过单次上限 {max_download}, 其余下次继续")
    return done


def ensure_calendar() -> None:
    ensure_schema()
    conn = connect()
    n = conn.execute("SELECT COUNT(*) c FROM calendar").fetchone()["c"]
    if n == 0:
        _init_calendar()


# ── 读取 ────────────────────────────────────────────────────────────────────

def load_panel(start: str, end: str, codes: Optional[Sequence[str]] = None,
               fields: Sequence[str] = _DEFAULT_FIELDS) -> Panel:
    """加载 [start, end] 价格面板（与 qlib 版 load_panel 对齐）。

    返回 Panel(dates=list[str], codes=list[str], f={"adjclose"/"close"/"factor": ndarray[T, N]})
    adjclose = 后复权收盘; close = 原始收盘; factor = hfq_close / close。
    """
    ensure_schema()
    conn = connect()
    if codes is None:
        codes = [r["code"] for r in conn.execute("SELECT DISTINCT code FROM price_daily").fetchall()]
    codes = [_norm_code(c) for c in codes]
    if not codes:
        return Panel(dates=[], codes=[], f={k: np.zeros((0, 0)) for k in fields})

    ph = ",".join("?" for _ in codes)
    rows = conn.execute(
        f"SELECT code, date, close, hfq_close FROM price_daily "
        f"WHERE code IN ({ph}) AND date >= ? AND date <= ? ORDER BY date",
        (*codes, start, end),
    ).fetchall()

    import pandas as pd

    df = pd.DataFrame([dict(r) for r in rows])
    dates = sorted(df["date"].unique().tolist()) if not df.empty else []
    if not dates:
        return Panel(dates=[], codes=[], f={k: np.zeros((0, len(codes))) for k in fields})
    T = len(dates)
    didx = {d: i for i, d in enumerate(dates)}
    cidx = {c: j for j, c in enumerate(codes)}

    close_a = np.full((T, len(codes)), np.nan)
    hfq_a = np.full((T, len(codes)), np.nan)
    if not df.empty:
        for code, g in df.groupby("code"):
            j = cidx.get(code)
            if j is None:
                continue
            for d, c, h in zip(g["date"], g["close"], g["hfq_close"]):
                i = didx[d]
                close_a[i, j] = c if c is not None else np.nan
                hfq_a[i, j] = h if h is not None else np.nan
    with np.errstate(divide="ignore", invalid="ignore"):
        fac_a = np.where(close_a > 0, hfq_a / close_a, np.nan)
    out = {"adjclose": hfq_a, "close": close_a, "factor": fac_a}
    return Panel(dates=dates, codes=codes, f={k: out[k] for k in fields})


def index_closes(index_code: str = "000300.SH", start: str = "", end: str = "") -> Dict[str, float]:
    """指数收盘价 {date: close}（本地没有则下载）。"""
    ensure_schema()
    conn = connect()
    sql = "SELECT date, close FROM index_daily WHERE code=?"
    params: list = [index_code]
    if start:
        sql += " AND date >= ?"
        params.append(start)
    if end:
        sql += " AND date <= ?"
        params.append(end)
    sql += " ORDER BY date"
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        download_index(index_code)
        rows = conn.execute(sql, params).fetchall()
    return {r["date"]: float(r["close"]) for r in rows if r["close"] is not None}


def close_prices(code: str, start: str, end: str) -> Dict[str, float]:
    """单只股票原始收盘价 {date: close}。"""
    ensure_schema()
    conn = connect()
    rows = conn.execute(
        "SELECT date, close FROM price_daily WHERE code=? AND date>=? AND date<=? ORDER BY date",
        (_norm_code(code), start, end),
    ).fetchall()
    return {r["date"]: float(r["close"]) for r in rows if r["close"] is not None}


def list_cached_codes() -> List[str]:
    ensure_schema()
    conn = connect()
    return [r["code"] for r in conn.execute("SELECT DISTINCT code FROM price_daily ORDER BY code").fetchall()]


def stats() -> dict:
    ensure_schema()
    conn = connect()
    n_codes = conn.execute("SELECT COUNT(DISTINCT code) c FROM price_daily").fetchone()["c"]
    n_days = conn.execute("SELECT COUNT(DISTINCT date) c FROM price_daily").fetchone()["c"]
    rng = conn.execute("SELECT MIN(date) mn, MAX(date) mx FROM price_daily").fetchone()
    n_cal = conn.execute("SELECT COUNT(*) c FROM calendar").fetchone()["c"]
    return {"codes": n_codes, "days": n_days, "range": [rng["mn"], rng["mx"]], "calendar_days": n_cal}


if __name__ == "__main__":  # python -m src.strategy.prices
    import argparse

    ap = argparse.ArgumentParser(description="行情价格库管理")
    ap.add_argument("--init", action="store_true", help="初始化日历")
    ap.add_argument("--index", default="000300.SH", help="下载指数 (default 000300.SH)")
    ap.add_argument("--codes", nargs="*", default=[], help="下载指定股票代码")
    ap.add_argument("--stats", action="store_true", help="查看库统计")
    args = ap.parse_args()
    if args.init or args.codes or args.index:
        ensure_calendar()
    if args.index and not args.codes:
        download_index(args.index)
    for c in args.codes:
        download_stock(c)
    if args.stats or not (args.init or args.codes or args.index):
        print(stats())
