"""配置加载：config.yaml 为基准，.env 环境变量覆盖敏感项。"""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.yaml"

#: 无 config.yaml 时使用的内置默认值（与仓库中的 config.yaml 保持一致）。
#: 2026-10-19 起仅保留东财实盘配置——模拟盘渠道（东财 spo_* / 雪球 / 同花顺）已删除。
FALLBACK_SETTINGS: dict = {
    "http": {"min_interval": 0.8, "timeout": 20, "proxy": ""},
    "eastmoney": {
        "cookie": "",
        "rank_type": "10005",
    },
    # 无头浏览器抓取后端（Playwright/Chromium）。enabled=true 时优先用浏览器拟人化抓取，
    # 失败自动回退 curl_cffi。headless=false 可调试（需桌面环境）。
    "browser": {
        "enabled": False,
        "headless": True,
        "stealth": True,
        "min_interval": 1.2,
        "timeout": 30000,
    },
    "output": {"dir": "data", "formats": ["csv", "json"]},
    # 本地 SQLite 入库（替代原 xcq MySQL）
    "sqlite": {"enabled": True, "path": "", "fail_hard": False},
}


def load_settings(config_path: Path | None = None) -> dict:
    load_dotenv(ROOT / ".env")

    path = config_path or DEFAULT_CONFIG
    if path.exists():
        settings = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    else:
        settings = {k: {kk: vv for kk, vv in v.items()} for k, v in FALLBACK_SETTINGS.items()}

    for section, defaults in FALLBACK_SETTINGS.items():
        current = settings.setdefault(section, {})
        for key, value in defaults.items():
            current.setdefault(key, value)

    # 敏感凭据走环境变量，不落配置文件
    if os.getenv("EASTMONEY_COOKIE"):
        settings["eastmoney"]["cookie"] = os.getenv("EASTMONEY_COOKIE")
    if os.getenv("HTTP_PROXY"):
        settings["http"]["proxy"] = os.getenv("HTTP_PROXY")

    # SQLite 入库路径：环境变量优先，默认 <项目根>/data/experts.db
    sq = settings.setdefault("sqlite", {})
    if os.getenv("EXPERTS_DB"):
        sq["path"] = os.getenv("EXPERTS_DB")
    if os.getenv("EXPERTS_DB_ENABLED"):
        sq["enabled"] = os.getenv("EXPERTS_DB_ENABLED").lower() in ("1", "true", "yes", "on")
    return settings


def sqlite_path(settings: dict | None = None) -> Path:
    """SQLite 主库路径（默认 data/experts.db）。"""
    settings = settings or load_settings()
    p = (settings.get("sqlite") or {}).get("path") or ""
    return Path(p) if p else (ROOT / "data" / "experts.db")
