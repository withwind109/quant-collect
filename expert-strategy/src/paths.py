"""项目路径常量（所有模块统一从这里取路径，避免各处硬编码）。

目录约定（相对项目根 expert-platform/）：
  data/                  运行数据（git 只提交 .gitkeep 与小型库）
  data/crawl/            每日爬取产出 JSON（data/<日期>/*.json）
  data/history/          单组合调仓历史 JSON
  data/history_bulk/     批量高手组合历史 JSON（跟随策略回测的数据基础）
  data/follow_backtest/  回测产出（净值/明细/汇总）
  data/experts.db        爬取结果 SQLite（可随 git 提交）
  data/prices.db         行情价格 SQLite（可随 git 提交，akshare 兜底下载）
  data/health.json       数据体检缓存
  logs/                  运行日志
  models/expert_follow/  跟随策略运行记录（summary.json 等）
  configs/expert_follow/ 策略文件（json5）与回测配置
"""

from __future__ import annotations

from pathlib import Path

#: 项目根（expert-platform/）——本文件位于 src/paths.py
PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: 源码目录（src/，UI 子进程与 sys.path 注入用）
SRC_DIR = PROJECT_ROOT / "src"

#: 爬虫配置（config.yaml）与 .env
CONFIG_YAML = PROJECT_ROOT / "config.yaml"
ENV_FILE = PROJECT_ROOT / ".env"

#: 运行数据根目录
DATA_DIR = PROJECT_ROOT / "data"
CRAWL_DATA_DIR = DATA_DIR / "crawl"           # 每日爬取 JSON：data/crawl/<YYYY-MM-DD>/
HISTORY_DIR = DATA_DIR / "history"            # 单组合历史
HISTORY_BULK_DIR = DATA_DIR / "history_bulk"  # 批量组合历史（回测数据基础）
FOLLOW_BT_DIR = DATA_DIR / "follow_backtest"  # 回测产出
FOLLOW_V2_DIR = DATA_DIR / "follow_v2"        # v2 每日跟随集合（盘中跟单用）
MODELS_DIR = DATA_DIR / "models"              # 运行记录（expert_follow/）
EVAL_DIR = DATA_DIR / "eval"                  # 高手能力评估产出（latest.json + 历次 run）

#: 数据库
DB_PATH = DATA_DIR / "experts.db"             # 爬取结果 + 策略/账户/运行记录
PRICE_DB_PATH = DATA_DIR / "prices.db"        # 行情价格库（可随 git 提交）
HEALTH_CACHE = DATA_DIR / "health.json"       # 数据体检缓存

#: 日志
LOG_DIR = PROJECT_ROOT / "logs"

#: 跟随策略配置（json5）
FOLLOW_CFG_DIR = PROJECT_ROOT / "configs" / "expert_follow"
FOLLOW_STRAT_DIR = FOLLOW_CFG_DIR / "strategies"
FOLLOW_USER_DIR = FOLLOW_STRAT_DIR / "user"
FOLLOW_BT_DIR_CFG = FOLLOW_CFG_DIR / "backtests"
FOLLOW_BT_USER_DIR = FOLLOW_BT_DIR_CFG / "user"


BROKER_DIR = DATA_DIR / "broker_accounts"      # 账户中心 storage_state（敏感，gitignore）


def ensure_dirs() -> None:
    """确保运行目录存在（导入即用，幂等）。"""
    for d in (DATA_DIR, CRAWL_DATA_DIR, HISTORY_DIR, HISTORY_BULK_DIR,
              FOLLOW_BT_DIR, FOLLOW_V2_DIR, MODELS_DIR / "expert_follow",
              EVAL_DIR, BROKER_DIR,
              FOLLOW_STRAT_DIR, FOLLOW_USER_DIR, FOLLOW_BT_DIR_CFG,
              FOLLOW_BT_USER_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
