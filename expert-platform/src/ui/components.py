"""Streamlit 共享组件（参考 multi-factor-stock 的 ui/components.py 风格）。

主题：浅色（.streamlit/config.toml base="light"），中文标签，结构化优先。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

#: src/ 与项目根（ui/components.py → 上两级是 src/，三级是项目根）
SRC_DIR = Path(__file__).resolve().parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from paths import LOG_DIR  # noqa: E402

#: 数据源中文标签（与原页面 expert-crawl.js 的 _EC_SRC 一致）
SOURCE_LABELS = {
    "eastmoney": "东财",
    "xueqiu": "雪球",
    "tonghuashun": "同花顺",
    "alipay": "支付宝",
}


def src_label(src: Optional[str]) -> str:
    return SOURCE_LABELS.get(str(src or ""), str(src or "—"))


def page_header(title: str, subtitle: str = "") -> None:
    st.title(title)
    if subtitle:
        st.caption(subtitle)
    st.divider()


def safe_pct(v: Any, digits: int = 2, fallback: str = "—") -> str:
    try:
        if v is None or (isinstance(v, float) and v != v):
            return fallback
        return f"{float(v):.{digits}f}%"
    except (TypeError, ValueError):
        return fallback


def safe_float(v: Any, fmt: str = ".4f", fallback: str = "—") -> str:
    try:
        if v is None or (isinstance(v, float) and v != v):
            return fallback
        return f"{float(v):{fmt}}"
    except (TypeError, ValueError):
        return fallback


def show_df(rows: List[dict], columns: Optional[Dict[str, str]] = None,
            fmt: Optional[Dict[str, str]] = None, height: int = 420) -> None:
    """统一表格展示：列重命名 + 数字格式化 + 隐藏索引。"""
    if not rows:
        st.info("暂无数据")
        return
    df = pd.DataFrame(rows)
    if columns:
        df = df.rename(columns=columns)
        df = df[[c for c in columns.values() if c in df.columns]]
    if fmt:
        df = df.style.format(fmt, na_rep="—")
    st.dataframe(df, width="stretch", hide_index=True, height=height)


def metric_row(items: List[tuple], cols: Optional[int] = None) -> None:
    """一行指标卡：[(label, value, delta?)]。"""
    n = cols or len(items)
    columns = st.columns(n)
    for col, item in zip(columns, items):
        label, value = item[0], item[1]
        delta = item[2] if len(item) > 2 else None
        with col:
            st.metric(label, value, delta=delta)


# ── 后台任务（子进程 + 状态文件轮询） ────────────────────────────────────────

def start_job(status_name: str, argv: List[str]) -> Path:
    """启动后台子进程（独立会话，Streamlit 重跑后仍存活），返回状态文件路径。"""
    status_fp = LOG_DIR / f"job_{status_name}.json"
    if status_fp.exists():
        try:
            old = json.loads(status_fp.read_text(encoding="utf-8"))
            if old.get("state") == "running":
                raise RuntimeError(f"任务 {status_name} 已在进行中")
        except ValueError:
            pass
    env = dict(os.environ)
    #: 剥掉宿主注入的 shim（broker IPC 会拖慢爬虫/回测的 IO），只保留 src
    old_pp = env.get("PYTHONPATH", "")
    keep = [p for p in old_pp.split(os.pathsep) if p and "WorkBuddy.app" not in p]
    env["PYTHONPATH"] = os.pathsep.join([str(SRC_DIR), *keep])
    env["PYTHONUNBUFFERED"] = "1"
    log = open(LOG_DIR / f"job_{status_name}.log", "a", encoding="utf-8")
    subprocess.Popen(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "run_job.py"), str(status_fp), "--", *argv],
        cwd=str(PROJECT_ROOT), stdout=log, stderr=subprocess.STDOUT,
        env=env, start_new_session=True,
    )
    return status_fp


def job_status(status_fp: Path) -> Dict[str, Any]:
    if not status_fp.exists():
        return {"state": "idle"}
    try:
        return json.loads(status_fp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "unknown"}


def job_panel(status_fp: Path, title: str = "任务状态") -> None:
    """展示任务状态；运行中时用 meta refresh 每 4 秒自动轮询。"""
    st_ = job_status(status_fp)
    state = st_.get("state", "idle")
    label = {"running": "🟡 运行中…", "done": "🟢 已完成", "error": "🔴 失败",
             "idle": "⚪ 未启动"}.get(state, state)
    st.caption(f"{title}: {label}"
               + (f"（{st_.get('started_at', '')} 起）" if st_.get("started_at") else ""))
    if state == "running":
        st.markdown('<meta http-equiv="refresh" content="4">', unsafe_allow_html=True)
    if st_.get("error"):
        st.error(st_["error"])
    if st_.get("returncode") not in (None, 0):
        st.warning(f"退出码 {st_['returncode']}")
    log_fp = Path(str(status_fp).replace("job_", "job_").replace(".json", ".log"))
    if log_fp.exists():
        with st.expander("运行日志（尾部）", expanded=(state == "error")):
            tail = log_fp.read_text(encoding="utf-8", errors="replace")[-3000:]
            st.code(tail or "（空）", language="text")


def status_file_for(name: str) -> Path:
    return LOG_DIR / f"job_{name}.json"


def fmt_ts(ts: Any) -> str:
    if not ts:
        return "—"
    return str(ts)[:19].replace("T", " ")


def kv_cards(pairs: List[tuple], ncols: int = 4) -> None:
    """键值对卡片（比 metric 更轻）。"""
    rows = [pairs[i:i + ncols] for i in range(0, len(pairs), ncols)]
    for row in rows:
        cols = st.columns(ncols)
        for col, (k, v) in zip(cols, row):
            with col:
                with st.container(border=True):
                    st.caption(str(k))
                    st.markdown(f"**{v}**")


def money(v: Any, fallback: str = "—") -> str:
    try:
        if v is None:
            return fallback
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return fallback
