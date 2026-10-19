"""后台定时任务：关注组合 rtV3 调仓明细自动拉取。

* 服务启动后延迟 20s 拉一次（等建表/登录态就绪）
* 之后每 8 小时拉一次，滚动累积 crawl_trade(live) 评估样本
* 未登录 / 关注列表为空 → 本轮跳过并记日志，等下一轮（扫码后自动生效）
* 守护线程：随服务退出，不阻塞 lifespan
"""
from __future__ import annotations

import threading
import time

from loguru import logger

INTERVAL_HOURS = 8
STARTUP_DELAY_SEC = 20


def _run_once(trigger: str) -> None:
    try:
        from web.routes.experts import fetch_live_trades
        d = fetch_live_trades(trigger=trigger)
        if d.get("ok"):
            logger.info(f"[scheduler] rtV3 自动拉取（{trigger}）：{d.get('total')} 条，新入库 {d.get('n')} 条")
        else:
            logger.info(f"[scheduler] rtV3 自动拉取跳过（{trigger}）：{d.get('msg')}")
    except Exception as exc:  # noqa: BLE001  定时任务失败不影响服务
        logger.warning(f"[scheduler] rtV3 自动拉取失败（{trigger}）：{exc}")


def start() -> None:
    def _loop() -> None:
        time.sleep(STARTUP_DELAY_SEC)
        _run_once("startup")
        while True:
            time.sleep(INTERVAL_HOURS * 3600)
            _run_once("scheduled")

    threading.Thread(target=_loop, name="rtv3-autofetch", daemon=True).start()
    logger.info(f"[scheduler] rtV3 自动拉取已启动：启动后 {STARTUP_DELAY_SEC}s 首拉，之后每 {INTERVAL_HOURS}h 一次")
