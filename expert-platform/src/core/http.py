"""HTTP 基础层：浏览器 TLS 指纹伪装 + 限速 + 重试。

雪球与东方财富的接口均部署在 WAF 之后，普通 requests 会被拦截，
因此统一使用 curl_cffi 的浏览器指纹模拟（impersonate）。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Mapping, Protocol, runtime_checkable

from curl_cffi import requests as creq
from loguru import logger
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

#: 模拟的浏览器 TLS 指纹
DEFAULT_IMPERSONATE = "chrome124"


@runtime_checkable
class Fetcher(Protocol):
    """抓取后端统一接口：HttpClient（curl_cffi）与 BrowserClient（Playwright）均实现。"""

    def get_text(self, url: str, **kwargs: Any) -> str: ...
    def get_json(self, url: str, **kwargs: Any) -> Any: ...
    def post_json(self, url: str, payload: Any, headers: Mapping[str, str] | None = None) -> Any: ...
    def warm(self, url: str, headers: Mapping[str, str] | None = None) -> None: ...
    def close(self) -> None: ...
    def fetch_status(
        self, url: str, method: str = "GET",
        headers: Mapping[str, str] | None = None, body: str = "",
    ) -> tuple[int, str]:
        """返回 (HTTP状态码, 响应文本原文)，不因非 2xx 抛异常、不假定 JSON body。

        get_json/post_json 遇到 >=400 直接抛 HttpError，且 post_json 强制把
        payload 序列化成 JSON body —— 遇到"非 200 状态码本身就是有效业务语义"
        的接口（如同花顺追踪接口: 首次成功返回 500 空体，见
        sources/tonghuashun.py::follow_expert）就不够用了，需要这个更底层的
        原始访问方式。
        """
        ...


class HttpError(RuntimeError):
    """请求失败（网络异常或状态码异常）。"""


class _RateLimiter:
    """按客户端粒度的限速器，避免触发目标站点频控。"""

    def __init__(self, min_interval: float) -> None:
        self._min_interval = max(0.0, min_interval)
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            delta = time.monotonic() - self._last
            if delta < self._min_interval:
                time.sleep(self._min_interval - delta)
            self._last = time.monotonic()


class HttpClient:
    """带指纹伪装、限速与重试的 HTTP 客户端。"""

    def __init__(
        self,
        min_interval: float = 0.8,
        timeout: int = 20,
        ua: str = CHROME_UA,
        proxy: str | None = None,
        impersonate: str = DEFAULT_IMPERSONATE,
    ) -> None:
        self.timeout = timeout
        self.ua = ua
        self._limiter = _RateLimiter(min_interval)
        self.session = creq.Session(impersonate=impersonate)
        self.session.headers.update(
            {
                "User-Agent": ua,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type((HttpError,)),
    )
    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        timeout: int | None = None,
    ) -> Any:
        self._limiter.wait()
        try:
            resp = self.session.request(
                method,
                url,
                params=params,
                json=json,
                headers=dict(headers or {}),
                timeout=timeout or self.timeout,
            )
        except Exception as exc:
            raise HttpError(f"{method} {url} 网络异常：{exc}") from exc

        if resp.status_code >= 400:
            raise HttpError(f"{method} {url} 返回状态码 {resp.status_code}")
        return resp

    def get_text(self, url: str, **kwargs: Any) -> str:
        return self.request("GET", url, **kwargs).text

    def get_json(self, url: str, **kwargs: Any) -> Any:
        resp = self.request("GET", url, **kwargs)
        try:
            return resp.json()
        except Exception as exc:
            raise HttpError(f"{url} 响应不是合法 JSON：{exc}") from exc

    def post_json(self, url: str, payload: Any, headers: Mapping[str, str] | None = None) -> Any:
        resp = self.request("POST", url, json=payload, headers=headers)
        try:
            return resp.json()
        except Exception as exc:
            raise HttpError(f"{url} 响应不是合法 JSON：{exc}") from exc

    def warm(self, url: str, headers: Mapping[str, str] | None = None) -> None:
        """访问页面以换取必要的 Cookie（如雪球的 xq_a_token）。"""
        try:
            self.request("GET", url, headers=headers)
        except HttpError as exc:
            logger.warning(f"预热请求失败 {url}：{exc}")

    def fetch_status(
        self, url: str, method: str = "GET",
        headers: Mapping[str, str] | None = None, body: str = "",
    ) -> tuple[int, str]:
        """底层原始访问：不因状态码抛异常，直接返回 (status_code, text)。见 Fetcher 协议注释。"""
        self._limiter.wait()
        try:
            resp = self.session.request(
                method, url, data=body or None,
                headers=dict(headers or {}), timeout=self.timeout,
            )
        except Exception as exc:
            raise HttpError(f"{method} {url} 网络异常：{exc}") from exc
        return resp.status_code, resp.text

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # pragma: no cover - 防御性
            pass
