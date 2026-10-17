"""FastAPI 应用工厂（对齐源项目 xcq-platform/scripts/dashboard/app.py 的结构）。

差异：业务模块直连（无 handlers 兼容层）、SQLite（无 MySQL）、on_event → lifespan。
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def _ensure_schemas() -> None:
    """启动时幂等建表（爬取 7 表 + 策略层表 + 价格库）。"""
    try:
        from core import db
        from paths import DB_PATH, PRICE_DB_PATH
        db.ensure_schema(DB_PATH)
        db.ensure_strategy_schema(DB_PATH)
    except Exception:  # noqa: BLE001  启动不因建表失败而中断（页面会显示具体错误）
        pass
    try:
        from strategy import prices
        prices.ensure_schema(PRICE_DB_PATH)
        prices.ensure_calendar(PRICE_DB_PATH)
    except Exception:  # noqa: BLE001
        pass


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _ensure_schemas()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="expert-platform", version="1.0.0",
                  docs_url=None, redoc_url=None, lifespan=_lifespan)

    # ── 路由注册（先于静态兜底） ───────────────────────────────────────────
    from web.routes import experts as experts_router
    from web.routes import strategy as strategy_router
    app.include_router(experts_router.router)
    app.include_router(strategy_router.router)

    # ── 手写 CORS + 慢请求日志（源项目同款中间件，精简版） ─────────────────
    @app.middleware("http")
    async def _cors_and_perf(request: Request, call_next):
        t0 = time.time()
        resp = await call_next(request)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        ms = (time.time() - t0) * 1000
        if request.url.path.startswith("/api/") and ms > 1500:
            from core.logger import logger
            logger.warning(f"[slow-api] {request.method} {request.url.path} {ms:.0f}ms")
        return resp

    # ── 静态资源 ──────────────────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse((_STATIC_DIR / "index.html").read_text(encoding="utf-8"))

    @app.get("/echarts.min.js")
    async def echarts() -> FileResponse:
        return FileResponse(_STATIC_DIR / "echarts.min.js",
                            media_type="application/javascript")

    app.mount("/css", StaticFiles(directory=str(_STATIC_DIR / "css")), name="css")
    app.mount("/js", StaticFiles(directory=str(_STATIC_DIR / "js")), name="js")

    # ── 服务信息（页面头部展示） ──────────────────────────────────────────
    @app.get("/api/system/info")
    async def system_info() -> Dict[str, Any]:
        from core.db import qone
        from paths import CRAWL_DATA_DIR, DB_PATH, HISTORY_BULK_DIR
        info: Dict[str, Any] = {"db": str(DB_PATH), "crawl_dir": str(CRAWL_DATA_DIR)}
        for t, key in (("crawl_expert", "experts"), ("crawl_trade", "trades"),
                       ("crawl_expert_buy", "expert_buys"), ("crawl_aggregate", "stocks"),
                       ("xcq_paper_accounts", "accounts")):
            try:
                info[key] = qone(f"SELECT COUNT(*) AS n FROM {t}")["n"]
            except Exception:  # noqa: BLE001
                info[key] = 0
        info["bulk_files"] = (len(list(HISTORY_BULK_DIR.glob("*.json")))
                              if HISTORY_BULK_DIR.is_dir() else 0)
        return info

    return app


app = create_app()
