"""统一日志出口（loguru），替代 scripts.core.logger。"""
from __future__ import annotations

import sys

from loguru import logger

_CONFIGURED = False


def setup_logging(level: str = "INFO") -> None:
    """配置 loguru 输出到 stderr（幂等）。"""
    global _CONFIGURED
    if _CONFIGURED:
        return
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <7}</level> | <level>{message}</level>",
        colorize=True,
    )
    _CONFIGURED = True


setup_logging()

__all__ = ["logger", "setup_logging"]
