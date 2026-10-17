"""社区高手 & 高手跟随策略平台 — Streamlit 入口（架构参考 multi-factor-stock）。

启动方式:
    streamlit run src/ui/app.py
    ./run_streamlit.sh [端口]        # 默认 8501

页面:
    社区高手      —— 东财高手流水抓取（Cookie 认证）/ 关注管理 / 历史回补 / 数据浏览
    高手跟随策略  —— 策略文件 / 统一回测 / 运行记录 / 模拟盘前向验证 / 晋级管理

没有 FastAPI 中间层：页面直接 import 业务模块（core.* / strategy.*），
后台任务（爬取/回测）以子进程方式驱动（scripts/run_job.py + 状态文件轮询）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

#: src/ 入 sys.path（app.py → parents[1] = src/）
SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from paths import ensure_dirs  # noqa: E402

ensure_dirs()


def _build_page_registry() -> dict:
    return {
        "experts": st.Page("routes/experts.py", title="社区高手", icon="🧑‍💼", url_path="experts", default=True),
        "strategy": st.Page("routes/strategy.py", title="高手跟随策略", icon="🧭", url_path="strategy"),
    }


if st.runtime.scriptrunner.get_script_run_ctx(suppress_warning=True) is not None:
    st.set_page_config(page_title="社区高手 & 跟随策略平台", page_icon="🧭", layout="wide")
    st.markdown(
        """
        <style>
        [data-testid="stAppDeployButton"] { display: none !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    pages = _build_page_registry()
    navigation = st.navigation(
        {
            "高手数据": [pages["experts"]],
            "跟随策略": [pages["strategy"]],
        },
        position="hidden",
    )

    with st.sidebar:
        st.title("高手跟随平台")
        st.caption("社区高手爬取 · 跟随策略回测 · 模拟盘前向验证")
        st.divider()
        if st.button("🧑‍💼 社区高手", width="stretch", key="btn_experts"):
            st.switch_page(pages["experts"])
        if st.button("🧭 高手跟随策略", width="stretch", key="btn_strategy"):
            st.switch_page(pages["strategy"])
        st.divider()
        st.caption("数据：本地 SQLite（data/experts.db · data/prices.db）")
        st.caption("启动：./run_streamlit.sh [端口]")

    navigation.run()
