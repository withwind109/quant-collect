"""社区高手页面（对应原 xcq-platform #/experts / expert-crawl）。

功能（对齐原 expert-crawl 页 + routes_experts_crawl.py 的 API 面）：
  * 数据概览：可用交易日、DB 行数、覆盖度
  * 手动触发爬取（东财 Cookie 认证，不再扫码登录；数据源可多选，未来可扩展渠道）
  * 按日期浏览：高手买入主视图 / 高手榜 / 成交明细 / 个股聚合 / 热度榜 / 信号 / 覆盖度
  * 关注高手管理（v1 镜像等策略的跟随名单来源）
  * 单高手 / 批量调仓历史回补（跟随策略回测的数据基础）

数据读取：本地 SQLite 优先（core.db.read_day），无该日入库时回退 data/crawl/<日期>/*.json。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import streamlit as st

from ui import components as X
from paths import CRAWL_DATA_DIR, DATA_DIR, HISTORY_BULK_DIR


def _ensure_db():
    from core import db
    from paths import DB_PATH
    db.ensure_schema(DB_PATH)
    db.ensure_strategy_schema(DB_PATH)
    return db


def _dates(db) -> List[str]:
    from paths import DB_PATH
    dates = set(db.list_dates(DB_PATH))
    if CRAWL_DATA_DIR.is_dir():
        for p in CRAWL_DATA_DIR.iterdir():
            if p.is_dir() and (p / "expert_buys.json").exists():
                dates.add(p.name)
    return sorted(dates, reverse=True)


def _read_day(db, date: str) -> Optional[Dict[str, Any]]:
    from paths import DB_PATH
    data = db.read_day(date, DB_PATH)
    if data is not None:
        return data
    day_dir = CRAWL_DATA_DIR / date
    if not (day_dir / "expert_buys.json").exists():
        return None

    def _load(name: str):
        fp = day_dir / name
        if not fp.exists():
            return []
        try:
            return json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    return {
        "experts": _load("experts.json"), "expert_buys": _load("expert_buys.json"),
        "buys": _load("trades.json"), "buy_aggregated": _load("buy_aggregated.json"),
        "stock_rank": _load("stock_rank.json"), "signals": _load("signals.json"),
        "meta": _load("meta.json") or {},
    }


def _flatten_expert_buys(expert_buys: List[dict]) -> List[dict]:
    """高手 → stocks[] 扁平成一高手一股一行（主视图）。"""
    rows = []
    for e in expert_buys or []:
        for s in e.get("stocks") or []:
            rows.append({
                "高手": e.get("expert_name") or e.get("expert_id"),
                "高手ID": e.get("expert_id"),
                "owner": e.get("owner") or "",
                "来源": X.src_label(e.get("source")),
                "类型": "实盘" if e.get("market_type") == "live" else "模拟盘",
                "收益率%": e.get("return_rate"),
                "今日%": e.get("day_return_rate"),
                "粉丝": e.get("followers"),
                "股票": s.get("stock_code"),
                "股票名": s.get("stock_name") or "",
                "方向": s.get("action") or "",
                "价格": s.get("price"),
                "持仓前%": s.get("weight_before"),
                "持仓后%": s.get("weight_after"),
                "时间": (s.get("trade_time") or "")[-8:],
            })
    return rows


def _crawl_panel(db) -> None:
    st.subheader("① 触发爬取")
    st.caption("东财使用 Cookie 认证（.env 的 EASTMONEY_COOKIE，不再扫码登录）；数据源可多选，未来新增渠道只需在 sources/ 下加一个源。")
    col1, col2, col3, col4 = st.columns([2, 2, 1, 1])
    with col1:
        sources = st.multiselect("数据源", ["eastmoney", "tonghuashun", "xueqiu", "alipay"],
                                 default=["eastmoney", "tonghuashun"],
                                 format_func=X.src_label, key="crawl_sources")
    with col2:
        date_mode = st.radio("交易日", ["自动推断", "指定日期"], horizontal=True, key="crawl_date_mode")
        date_str = "" if date_mode == "自动推断" else str(st.date_input("日期", key="crawl_date").strftime("%Y-%m-%d"))
    with col3:
        top = st.number_input("每榜数量", min_value=5, max_value=500, value=50, step=10, key="crawl_top")
    with col4:
        st.write("")
        st.write("")
        if st.button("▶ 开始爬取", type="primary", width="stretch", key="crawl_start"):
            if not sources:
                st.error("至少选一个数据源")
            else:
                argv = [sys_executable(), "-m", "cli", "crawl", "--sources", ",".join(sources), "--top", str(top)]
                if date_str:
                    argv += ["--date", date_str]
                fp = X.start_job("crawl", argv)
                st.session_state["crawl_status_fp"] = str(fp)
                st.rerun()
    fp = Path(st.session_state.get("crawl_status_fp") or (X.LOG_DIR / "job_crawl.json"))
    X.job_panel(fp, "爬取任务")
    st.caption(f"产出目录：{CRAWL_DATA_DIR}/<日期>/（JSON + SQLite 双写）")


def _history_panel() -> None:
    st.subheader("② 高手调仓历史回补")
    st.caption("跟随策略回测的数据基础（data/history_bulk/<组合号>.json）。单组合回补立即执行；批量回补在后台跑。")
    tab1, tab2 = st.tabs(["单组合回补", "批量回补"])
    with tab1:
        c1, c2, c3 = st.columns([2, 2, 1])
        with c1:
            zjzh = st.text_input("模拟盘组合号（1000… 长号）", key="hist_zjzh")
        with c2:
            since = st.text_input("起始日 YYYY-MM-DD（空=全部）", key="hist_since")
        with c3:
            st.write("")
            st.write("")
            if st.button("回补", key="hist_run", width="stretch"):
                if not zjzh.strip():
                    st.error("请输入组合号")
                else:
                    argv = [sys_executable(), "-m", "cli", "history", "--zjzh", zjzh.strip()]
                    if since.strip():
                        argv += ["--since", since.strip()]
                    fp = X.start_job("history", argv)
                    st.session_state["hist_status_fp"] = str(fp)
                    st.rerun()
        X.job_panel(Path(st.session_state.get("hist_status_fp") or (X.LOG_DIR / "job_history.json")), "回补任务")
        _show_single_history(zjzh.strip() if zjzh else "")
    with tab2:
        c1, c2, c3 = st.columns([2, 1, 1])
        with c1:
            st.caption(f"名单 = 每日爬取产出里出现过的东财组合（当前 {len(list(HISTORY_BULK_DIR.glob('*.json'))) if HISTORY_BULK_DIR.is_dir() else 0} 个已有文件）")
        with c2:
            limit = st.number_input("只跑前 N 个（0=全部）", min_value=0, value=20, step=10, key="bulk_limit")
        with c3:
            st.write("")
            st.write("")
            if st.button("批量回补", key="bulk_run", width="stretch"):
                argv = [sys_executable(), "-m", "cli", "bulk-history", "--limit", str(limit)]
                fp = X.start_job("bulk_history", argv)
                st.session_state["bulk_status_fp"] = str(fp)
                st.rerun()
        X.job_panel(Path(st.session_state.get("bulk_status_fp") or (X.LOG_DIR / "job_bulk_history.json")), "批量回补任务")


def _show_single_history(zjzh: str) -> None:
    if not zjzh:
        return
    fp = DATA_DIR / "history" / f"{zjzh}.json"
    if not fp.exists():
        st.caption(f"（{fp.name} 尚未回补）")
        return
    try:
        d = json.loads(fp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    recs = d.get("records") or []
    st.caption(f"{d.get('name') or zjzh} · {len(recs)} 条 · 回补于 {d.get('fetched_at')}")
    rows = [{
        "日期": r.get("trade_date"), "时间": (r.get("trade_time") or "")[-8:],
        "股票": r.get("stock_code"), "股票名": r.get("stock_name") or "",
        "方向": r.get("action"), "价格": r.get("price"),
        "持仓前%": r.get("weight_before"), "持仓后%": r.get("weight_after"),
    } for r in recs[:200]]
    X.show_df(rows, height=280)


def _watch_panel() -> None:
    st.subheader("③ 关注高手")
    st.caption("关注名单是 v1 镜像策略（universe.source=watchlist）与 v2（include_watchlist）的跟随来源。")
    from strategy import watchlist as W
    c1, c2, c3 = st.columns([2, 2, 1])
    with c1:
        eid = st.text_input("高手ID（组合号）", key="wl_id")
    with c2:
        nm = st.text_input("备注名（可选）", key="wl_name")
    with c3:
        st.write("")
        st.write("")
        if st.button("＋ 关注", key="wl_add", width="stretch"):
            if eid.strip():
                W.add(eid.strip(), nm.strip())
                st.rerun()
    rows = [{"高手ID": r["expert_id"], "名称": r.get("name") or "", "加入时间": r.get("created_at") or "",
             "操作": r["expert_id"]} for r in W.list_watch("sim")]
    if rows:
        df_rows = rows
        sel = st.selectbox("取消关注", [r["高手ID"] for r in df_rows], key="wl_del_sel")
        if st.button("－ 取消关注", key="wl_del"):
            W.remove(sel)
            st.rerun()
        X.show_df([{k: v for k, v in r.items() if k != "操作"} for r in df_rows], height=200)
    else:
        st.info("关注列表为空（模拟盘）")


def _data_panel(db) -> None:
    st.subheader("④ 按日期浏览")
    dates = _dates(db)
    if not dates:
        st.info("还没有数据 —— 先在上面触发一次爬取，或用 CLI：python -m cli crawl")
        return
    c1, c2 = st.columns([2, 3])
    with c1:
        date = st.selectbox("交易日", dates, key="view_date")
    with c2:
        st.caption(f"共 {len(dates)} 个交易日有数据；最新 {dates[0]}")
    data = _read_day(db, date)
    if not data:
        st.warning(f"{date} 没有可展示的数据")
        return
    meta = data.get("meta") or {}
    cov = meta.get("coverage") or []
    n_eb = len(data.get("expert_buys") or [])
    n_buys = len(data.get("buys") or [])
    n_live = sum(1 for e in data.get("expert_buys") or [] if e.get("market_type") == "live")
    X.kv_cards([
        ("高手买入", n_eb), ("成交明细", n_buys), ("实盘高手", n_live),
        ("覆盖度", f"{len(cov)} 项"), ("日期模式", meta.get("date_mode") or "—"),
        ("爬取时间", (meta.get("crawled_at") or "—")[:19]),
    ])
    if cov:
        with st.expander("覆盖度明细"):
            X.show_df(cov, height=200)

    tabs = st.tabs(["高手买入（主视图）", "高手榜", "成交明细", "个股聚合", "热度榜", "信号"])
    with tabs[0]:
        rows = _flatten_expert_buys(data.get("expert_buys") or [])
        srcs = sorted({r["来源"] for r in rows})
        pick = st.multiselect("来源过滤", srcs, default=srcs, key="eb_src")
        rows = [r for r in rows if r["来源"] in pick] if pick else rows
        only_live = st.checkbox("只看实盘高手", key="eb_live")
        if only_live:
            rows = [r for r in rows if r["类型"] == "实盘"]
        st.caption(f"{len(rows)} 行（高手 × 股票）")
        X.show_df(rows[:1000], height=460)
    with tabs[1]:
        rows = [{"高手": e.get("name") or e.get("expert_id"), "高手ID": e.get("expert_id"),
                 "来源": X.src_label(e.get("source")), "类型": "实盘" if e.get("market_type") == "live" else "模拟盘",
                 "榜单": e.get("rank_label") or "", "owner": e.get("owner") or "",
                 "收益率%": e.get("return_rate"), "总收益%": e.get("total_return"),
                 "粉丝": e.get("followers")} for e in data.get("experts") or []]
        X.show_df(rows, height=460)
    with tabs[2]:
        rows = [{"高手": t.get("expert_name") or t.get("expert_id"), "来源": X.src_label(t.get("source")),
                 "股票": t.get("stock_code"), "股票名": t.get("stock_name") or "",
                 "方向": t.get("action"), "价格": t.get("price"), "成交量": t.get("volume"),
                 "成交额": t.get("amount"), "时间": (t.get("trade_time") or "")[-8:]}
                for t in data.get("buys") or []]
        X.show_df(rows[:1000], height=460)
    with tabs[3]:
        rows = [{"股票": a.get("stock_code"), "股票名": a.get("stock_name") or "",
                 "买入高手数": a.get("buy_experts"), "买入笔数": a.get("buy_records"),
                 "均价": a.get("avg_price"), "净买入额": a.get("net_amount"),
                 "备注": a.get("note") or ""} for a in data.get("buy_aggregated") or []]
        X.show_df(rows, height=460)
    with tabs[4]:
        rows = [{"股票": a.get("stock_code"), "股票名": a.get("stock_name") or "",
                 "买入高手数": a.get("buy_experts"), "买入笔数": a.get("buy_records"),
                 "均价": a.get("avg_price"), "净买入额": a.get("net_amount")}
                for a in data.get("stock_rank") or []]
        X.show_df(rows, height=460)
    with tabs[5]:
        rows = [{"类型": s.get("kind") or "", "高手": s.get("expert_name") or s.get("expert_id") or "",
                 "方向": s.get("action") or "", "股票": s.get("stock_code") or "",
                 "股票名": s.get("stock_name") or "", "时间": (s.get("trade_time") or "")[-8:],
                 "备注": s.get("note") or ""} for s in data.get("signals") or []]
        X.show_df(rows, height=460)


def sys_executable() -> str:
    import sys
    return sys.executable


def render() -> None:
    X.page_header("社区高手", "东财高手操作流水 · 多源抓取（Cookie 认证）· 本地 SQLite 存储")
    db = _ensure_db()
    _crawl_panel(db)
    st.divider()
    _history_panel()
    st.divider()
    _watch_panel()
    st.divider()
    _data_panel(db)
