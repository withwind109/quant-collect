#!/usr/bin/env python3
"""后台任务包装：运行一条命令，并把状态写入 JSON 文件（供 Streamlit 页面轮询）。

用法:
  python scripts/run_job.py <status_json> -- <command> [args...]

状态文件内容: {"state": "running|done|error", "started_at", "finished_at", "returncode", "cmd"}
与原 xcq-platform 的 dashboard 子任务模式一致（subprocess.Popen + status 文件），
但去掉了 FastAPI 中间层 —— Streamlit 页面直接以子进程方式驱动爬虫/回测。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[2] != "--":
        print("用法: run_job.py <status_json> -- <command> [args...]")
        return 2
    status_fp = Path(sys.argv[1])
    cmd = sys.argv[3:]
    status_fp.parent.mkdir(parents=True, exist_ok=True)
    status = {"state": "running", "started_at": datetime.now().isoformat(timespec="seconds"), "cmd": cmd}
    status_fp.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        status.update(state="done" if proc.returncode == 0 else "error",
                      returncode=proc.returncode,
                      finished_at=datetime.now().isoformat(timespec="seconds"))
        tail = (proc.stdout or "")[-4000:]
        if proc.returncode != 0:
            status["error"] = ((proc.stderr or "") or tail)[-2000:]
    except Exception as exc:  # noqa: BLE001
        status.update(state="error", error=str(exc)[:2000],
                      finished_at=datetime.now().isoformat(timespec="seconds"))
    status_fp.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
