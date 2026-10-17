#!/usr/bin/env python3
"""expert-platform CLI 入口（架构参考 multi-factor-stock 的 src/main.py）。

用法:
    python main.py crawl --sources eastmoney,tonghuashun [--date auto] [--top 50]
    python main.py check                       # 凭据与接口连通性自检
    python main.py history --zjzh 1000xxxx     # 单组合调仓历史回补
    python main.py bulk-history [--limit 20]   # 批量回补（跟随策略数据基础）
    python main.py db-sync --all               # 爬取 JSON 补齐进 SQLite
    python main.py strategy list               # 策略文件 / 回测配置
    python main.py strategy run [--backtest unified_sim]
    python main.py strategy health             # 数据体检
    python main.py ui [--port 8501]            # 启动 Streamlit
    python main.py prices --index 000300.SH    # 初始化行情价格库（回测前必须）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: 宿主（WorkBuddy agent）注入的 PYTHONPATH shim 会把每次 open()/mkdir 包一层 broker IPC，
#: 爬虫/回测这类重 IO 场景必须剥掉（与 multi-factor-stock 的 env_guard 同款防护）。
#: sitecustomize 已在解释器启动时加载，只能 re-exec 干净环境。
_SHIM_MARK = "WorkBuddy.app"
if _SHIM_MARK in os.environ.get("PYTHONPATH", "") and not os.environ.get("_EP_CLEAN_ENV"):
    os.environ.pop("PYTHONPATH", None)
    os.environ["_EP_CLEAN_ENV"] = "1"
    os.execv(sys.executable, [sys.executable, os.path.abspath(__file__), *sys.argv[1:]])

#: src/ 入 sys.path（main.py 与 src/ 同级）
SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cli import cli  # noqa: E402


if __name__ == "__main__":
    cli()
