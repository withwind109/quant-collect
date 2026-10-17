"""后台任务机制（对齐源项目 xcq-platform 的 routes_* 任务模式，去掉 FastAPI 中间层后由本层直管）。

设计（与源项目一致）：
  * 模块级 _STATE dict 记录每个任务的子进程句柄（进程重启后丢失，任务本身已 detached）
  * 子进程经 scripts/run_job.py 包装，状态写 logs/job_<name>.json，前端轮询 /status
  * start_new_session=True 让任务在服务重启后继续跑（uvicorn reload 不杀任务）
  * 剥掉宿主（WorkBuddy agent）注入的 PYTHONPATH shim —— broker IPC 会拖慢爬虫/回测的 IO
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

from paths import LOG_DIR, PROJECT_ROOT

#: 任务名 → 状态文件（logs/job_<name>.json）与日志（logs/job_<name>.log）
#: crawl / history / bulk_history / backtest / paper_update / db_sync / health
_SHIM_MARK = "WorkBuddy.app"


def status_file(name: str) -> Path:
    return LOG_DIR / f"job_{name}.json"


def log_file(name: str) -> Path:
    return LOG_DIR / f"job_{name}.log"


def read_status(name: str) -> Dict[str, Any]:
    fp = status_file(name)
    if not fp.exists():
        return {"state": "idle"}
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "unknown"}


def read_log_tail(name: str, chars: int = 4000) -> str:
    fp = log_file(name)
    if not fp.exists():
        return ""
    try:
        return fp.read_text(encoding="utf-8", errors="replace")[-chars:]
    except OSError:
        return ""


def _clean_env() -> Dict[str, str]:
    env = dict(os.environ)
    old_pp = env.get("PYTHONPATH", "")
    keep = [p for p in old_pp.split(os.pathsep) if p and _SHIM_MARK not in p]
    src = str(PROJECT_ROOT / "src")
    if src not in keep:
        keep.insert(0, src)
    env["PYTHONPATH"] = os.pathsep.join(keep)
    env["PYTHONUNBUFFERED"] = "1"
    return env


class JobBusy(RuntimeError):
    """同名任务已在进行中（HTTP 409）。"""


def start_job(name: str, argv: List[str]) -> Dict[str, Any]:
    """启动后台任务；返回状态文件内容。任务已存在且 running → JobBusy。

    argv 以解释器参数开头（如 ["-m", "cli", ...]）时自动补 sys.executable 前缀；
    也可直接传完整命令（[sys.executable, "-m", ...]）。
    """
    if argv and str(argv[0]).startswith("-"):
        argv = [sys.executable, *argv]
    st = read_status(name)
    if st.get("state") == "running":
        raise JobBusy(f"任务 {name} 已在进行中")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = open(log_file(name), "a", encoding="utf-8")
    subprocess.Popen(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "run_job.py"),
         str(status_file(name)), "--", *argv],
        cwd=str(PROJECT_ROOT), stdout=log, stderr=subprocess.STDOUT,
        env=_clean_env(), start_new_session=True,
    )
    return read_status(name)


def job_view(name: str, include_log: bool = True) -> Dict[str, Any]:
    """给前端的任务视图：状态 + 日志尾部。"""
    st = read_status(name)
    out: Dict[str, Any] = {"name": name, "state": st.get("state", "idle"),
                           "started_at": st.get("started_at"),
                           "finished_at": st.get("finished_at"),
                           "returncode": st.get("returncode"),
                           "error": st.get("error"), "cmd": st.get("cmd")}
    if include_log:
        out["log_tail"] = read_log_tail(name)
    return out
