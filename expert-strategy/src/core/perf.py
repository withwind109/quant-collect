"""看板进程性能测量 (2026-09-26 用户决策: 先测量, 不急着优化)。

回答一个问题: 模拟盘页面慢的时候, 是接口本身慢, 还是同一进程里的后台重任务 (盘中轮询/执行、每日决策、推荐生成、
跟随高手上下文构建 …) 在抢 CPU。记录三类数据 (都在 logs/perf/, 按天一个 JSONL 文件):
  slow_requests_<日期>.jsonl   超过 SLOW_MS 的请求: 路径、耗时、当时正在跑的后台任务、进程 CPU%
  jobs_<日期>.jsonl            后台任务: 名称、耗时 (超过 JOB_LOG_MS 才记, 避免每分钟的空转刷屏)
  samples_<日期>.jsonl         每分钟进程快照: CPU%、线程数、内存、正在跑的任务
另在内存里按路由聚合 次数 / p50 / p95 / 最大, 由 GET /api/system/perf 查看。
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Deque, Dict, List

from paths import PROJECT_ROOT

LOG_DIR = PROJECT_ROOT / "logs" / "perf"
SLOW_MS = 500
JOB_LOG_MS = 200

_LOCK = threading.Lock()
_ACTIVE: Dict[int, Dict[str, Any]] = {}          # 线程 id → {name, start}
_ROUTES: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=500))
_COUNTS: Dict[str, int] = defaultdict(int)
_RECENT_SLOW: Deque[Dict[str, Any]] = deque(maxlen=200)
_RECENT_JOBS: Deque[Dict[str, Any]] = deque(maxlen=300)
_PROC = None


def _proc():
    global _PROC
    if _PROC is None:
        try:
            import psutil
            _PROC = psutil.Process(os.getpid())
            _PROC.cpu_percent(None)
        except Exception:  # noqa: BLE001
            _PROC = False
    return _PROC


def _write(kind: str, row: Dict[str, Any]) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / f"{kind}_{datetime.now():%Y%m%d}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def active_jobs() -> List[Dict[str, Any]]:
    now = time.time()
    with _LOCK:
        return [{"job": v["name"], "running_s": round(now - v["start"], 1)} for v in _ACTIVE.values()]


@contextmanager
def job(name: str):
    """后台任务计时: with perf.job("paper.intraday_cycle"): ..."""
    tid = threading.get_ident()
    t0 = time.time()
    with _LOCK:
        prev = _ACTIVE.get(tid)
        _ACTIVE[tid] = {"name": name, "start": t0}
    ok = True
    try:
        yield
    except Exception:
        ok = False
        raise
    finally:
        ms = (time.time() - t0) * 1000
        with _LOCK:
            if prev:
                _ACTIVE[tid] = prev
            else:
                _ACTIVE.pop(tid, None)
        if ms >= JOB_LOG_MS:
            row = {"ts": datetime.now().isoformat(timespec="seconds"), "job": name, "ms": round(ms), "ok": ok}
            _RECENT_JOBS.append(row)
            _write("jobs", row)


def timed(name: str):
    """装饰器版: @perf.timed("recs.daily_run")"""
    def deco(fn):
        import functools

        @functools.wraps(fn)
        def wrap(*a, **kw):
            with job(name):
                return fn(*a, **kw)
        return wrap
    return deco


def record_request(path: str, method: str, ms: float, status: int) -> None:
    key = f"{method} {path}"
    with _LOCK:
        _ROUTES[key].append(ms)
        _COUNTS[key] += 1
    if ms >= SLOW_MS:
        p = _proc()
        row = {"ts": datetime.now().isoformat(timespec="seconds"), "req": key, "ms": round(ms), "status": status,
               "active_jobs": active_jobs(), "cpu_pct": p.cpu_percent(None) if p else None,
               "threads": threading.active_count()}
        _RECENT_SLOW.append(row)
        _write("slow_requests", row)


def sample() -> None:
    """每分钟由轮询线程调用一次: 进程快照。"""
    p = _proc()
    row = {"ts": datetime.now().isoformat(timespec="seconds"), "cpu_pct": p.cpu_percent(None) if p else None,
           "rss_mb": round(p.memory_info().rss / 1e6) if p else None, "threads": threading.active_count(),
           "active_jobs": active_jobs()}
    _write("samples", row)


def summary() -> Dict[str, Any]:
    with _LOCK:
        routes = {k: list(v) for k, v in _ROUTES.items()}
        counts = dict(_COUNTS)
    rows = []
    for k, xs in routes.items():
        s = sorted(xs)
        rows.append({"route": k, "count": counts.get(k, 0), "p50_ms": round(s[len(s) // 2]),
                     "p95_ms": round(s[min(len(s) - 1, int(len(s) * 0.95))]), "max_ms": round(s[-1])})
    rows.sort(key=lambda r: -r["p95_ms"])
    return {"routes": rows[:60], "recent_slow": list(_RECENT_SLOW)[-50:], "recent_jobs": list(_RECENT_JOBS)[-80:],
            "active_jobs": active_jobs(), "slow_ms": SLOW_MS, "log_dir": str(LOG_DIR)}
