#!/usr/bin/env python3
"""后台任务包装：运行一条命令，并把状态写入 JSON 文件（供 Web 页面轮询）。

用法:
  python scripts/run_job.py <status_json> -- <command> [args...]

状态文件内容: {"state": "running|done|error", "started_at", "finished_at", "returncode", "cmd"}
日志实时写到与状态文件同名的 .log（status=job_crawl.json → logs/job_crawl.log），
页面 /api/expert-crawl/log 轮询该文件尾部。

与原 xcq-platform 的 dashboard 子任务模式一致（subprocess + status 文件）。
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
    log_fp = status_fp.with_suffix(".log")
    status = {"state": "running", "started_at": datetime.now().isoformat(timespec="seconds"), "cmd": cmd}
    status_fp.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    tail: list[str] = []
    try:
        # 实时写日志（页面轮询）+ 留尾部用于失败摘要
        with open(log_fp, "a", encoding="utf-8") as log:
            log.write(f"\n===== {datetime.now().isoformat(timespec='seconds')} $ {' '.join(cmd)} =====\n")
            log.flush()
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, errors="replace", bufsize=1)
            assert proc.stdout is not None
            for line in proc.stdout:
                log.write(line)
                log.flush()
                tail.append(line)
                if len(tail) > 400:
                    tail.pop(0)
            proc.wait()
        status.update(state="done" if proc.returncode == 0 else "error",
                      returncode=proc.returncode,
                      finished_at=datetime.now().isoformat(timespec="seconds"))
        if proc.returncode != 0:
            status["error"] = "".join(tail)[-2000:]
    except Exception as exc:  # noqa: BLE001
        status.update(state="error", error=str(exc)[:2000],
                      finished_at=datetime.now().isoformat(timespec="seconds"))
    status_fp.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
