"""交易日历（本地价格库 calendar 表，替代 xcq-platform 的 MySQL xcq_trading_calendar）。

API 与 xcq-platform 版对齐：is_trading_day / prev_trading_day / next_trading_day /
get_trading_days / is_holiday / in_trading_session / trading_session_state。
期货/期权交割日回填不迁移（跟随策略不用）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from strategy import prices as _P

_AM_OPEN, _AM_CLOSE = (9, 15), (11, 30)
_PM_OPEN, _PM_CLOSE = (13, 0), (15, 0)

_CAL_CACHE: dict = {}


def _cal(start: str = "2015-01-01") -> List[str]:
    if start not in _CAL_CACHE:
        _CAL_CACHE[start] = _P.trading_calendar(start=start)
    return _CAL_CACHE[start]


def is_trading_day(date_str: str) -> bool:
    days = _cal()
    if date_str in days:
        return True
    return datetime.strptime(date_str, "%Y-%m-%d").weekday() < 5


def prev_trading_day(date_str: str) -> Optional[str]:
    days = _cal()
    prev = [d for d in days if d < date_str]
    if prev:
        return prev[-1]
    d = datetime.strptime(date_str, "%Y-%m-%d")
    for _ in range(10):
        d -= timedelta(days=1)
        if d.weekday() < 5:
            return d.strftime("%Y-%m-%d")
    return None


def next_trading_day(date_str: str) -> Optional[str]:
    days = _cal()
    nxt = [d for d in days if d > date_str]
    if nxt:
        return nxt[0]
    d = datetime.strptime(date_str, "%Y-%m-%d")
    for _ in range(10):
        d += timedelta(days=1)
        if d.weekday() < 5:
            return d.strftime("%Y-%m-%d")
    return None


def get_trading_days(start_date: str, end_date: str) -> List[str]:
    return [d for d in _cal(start=start_date) if d <= end_date]


def is_holiday(date_str: str) -> Tuple[bool, str]:
    return False, ""


def _hm(t) -> tuple[int, int]:
    return (t.hour, t.minute)


def in_trading_session(now=None) -> bool:
    """当前是否处于 A 股交易时段（含集合竞价，不含午休）。只看时钟，不查日历。"""
    now = now or datetime.now()
    hm = _hm(now)
    return (_AM_OPEN <= hm <= _AM_CLOSE) or (_PM_OPEN <= hm <= _PM_CLOSE)


def trading_session_state(now=None) -> dict:
    """交易时段状态（trading / holiday / lunch / before / after）。"""
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    hm = _hm(now)
    if not is_trading_day(today):
        return {"open": False, "reason": "holiday", "date": today,
                "time": now.strftime("%H:%M:%S"), "holiday_name": ""}
    if _AM_OPEN <= hm <= _AM_CLOSE or _PM_OPEN <= hm <= _PM_CLOSE:
        reason = "trading"
    elif hm < _AM_OPEN:
        reason = "before"
    elif _AM_CLOSE < hm < _PM_OPEN:
        reason = "lunch"
    else:
        reason = "after"
    return {"open": reason == "trading", "reason": reason, "date": today,
            "time": now.strftime("%H:%M:%S"), "holiday_name": ""}
