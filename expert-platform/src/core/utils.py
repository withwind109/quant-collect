"""通用工具：类型转换、时间处理、证券代码规范化、实盘/模拟盘推断。"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

#: 数值字符串里需要剥离的非数字符号（百分号 / 千分位逗号 / 空白）
_NUM_CLEAN_RE = re.compile(r"[%,\s]")

# ── 实盘/模拟盘打标（2026-09-23 方案 P1）────────────────────────
MARKET_LIVE = "live"   # 实盘（真金白银）
MARKET_SIM = "sim"     # 模拟盘（模拟组合/模拟炒股）

#: market_type -> 中文标签（前端徽章/推送前缀共用）
MARKET_LABELS = {MARKET_LIVE: "实盘", MARKET_SIM: "模拟"}


def infer_market_type(source: str, channel: str = "", expert_id: str = "") -> str:
    """推断记录的实盘/模拟盘归属。

    规则（与 docs/社区高手实盘模拟盘分离与指定高手跟踪_方案_20260923.md 一致）：
      * alipay（蚂蚁财富实盘基金）           -> live
      * eastmoney：channel 含「实盘」或组合号
        以 9 开头（rtV1 实盘榜短号，如 900018468；
        模拟盘为 100000000… 长号）           -> live
      * eastmoney 其余（spo_* 模拟流水）、
        tonghuashun（模拟炒股）、xueqiu（组合模拟）-> sim
    未知 source 一律 sim（保守默认，宁可不标实盘）。
    """
    src = (source or "").lower()
    if src == "alipay":
        return MARKET_LIVE
    if src == "eastmoney":
        if "实盘" in (channel or ""):
            return MARKET_LIVE
        eid = str(expert_id or "")
        if eid.isdigit() and not eid.startswith("1000") and eid.startswith("9"):
            return MARKET_LIVE
        return MARKET_SIM
    return MARKET_SIM


def to_int(value: Any, default: int = 0) -> int:
    """把接口原始值转成 int，失败返回 default。"""
    try:
        return int(float(_NUM_CLEAN_RE.sub("", str(value))))
    except (TypeError, ValueError):
        return default


def to_float(value: Any, default: float | None = None) -> float | None:
    """把接口原始值转成 float，失败返回 default。

    用于 price / 收益率等可空字段；需要「必为数值」的场景用 `to_float(x) or 0.0`。
    会先剥离百分号、千分位逗号与空白，使 `1.77%` / `1,234.5` 之类也能正常解析。
    """
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(_NUM_CLEAN_RE.sub("", str(value)))
    except (TypeError, ValueError):
        return default


def ts_to_str(ts_ms: Any, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """毫秒时间戳转本地时间字符串。"""
    if not ts_ms:
        return ""
    try:
        return datetime.fromtimestamp(float(ts_ms) / 1000).strftime(fmt)
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def normalize_symbol(raw: str) -> tuple[str, str]:
    """把 SH600519 / 600519.SH / 600519 统一成 (代码, 市场)。"""
    if not raw:
        return "", ""
    s = str(raw).upper().strip()
    if "." in s:  # 600519.SH
        code, _, market = s.partition(".")
        return code, market
    if s[:2] in {"SH", "SZ", "BJ", "HK"}:  # SH600519
        return s[2:], s[:2]
    # 纯数字：按代码段推断市场
    if s.startswith(("60", "68", "51", "58", "11", "9")):
        return s, "SH"
    if s.startswith(("00", "30", "12", "15", "16", "18")):
        return s, "SZ"
    if s.startswith(("4", "8", "92")):
        return s, "BJ"
    return s, ""
