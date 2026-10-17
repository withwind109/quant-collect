"""无头浏览器抓取层：用 Playwright 驱动真实 Chromium 模拟用户行为，规避 WAF / 反爬。

与 :mod:`src.core.http.HttpClient` 暴露**完全相同的方法签名**（`get_text` /
`get_json` / `post_json` / `warm` / `fetch_status` / `close`），因此数据源代码
无需改动即可在「curl_cffi 指纹伪装」与「无头浏览器拟人化」两种抓取后端之间切换
（``config.yaml`` 的 ``browser.enabled`` 控制，启动失败自动回退 curl_cffi）。

为什么用浏览器（相比裸 HTTP 直连）：
- 真实 TLS 指纹、真实 JS 引擎（canvas / font / WebGL 指纹与真人一致）
- 自动执行站点 JS 挑战、建立会话 Cookie
- init-script 抹掉 ``navigator.webdriver`` 等自动化特征
- 拟人交互（随机鼠标移动、随机停留、持续会话）降低被识别概率

实现要点（反爬关键，2026-10-16 重构）：
- **按域缓存常驻 Page**：真人不会每个 XHR 开新标签页。同一 API 域复用一个
  已落地的 Page，请求间隔内保持会话连续；Page 被站点 JS 导航跑飞或关闭时
  自动重建（重建时检查 ``page.url`` 的 origin，跨域漂移也重建）。
- **先落地再 fetch**：Page 首次打开时 ``goto`` 该域站点根（轻量导航），之后
  API 请求一律用页面内 ``fetch`` 发 XHR —— 携带真实 Referer/Origin/Sec-Fetch
  头与 Cookie，同源不受 CORS 限制。
  ⚠️ 不再 ``goto`` API URL 本身：早期实现每个请求都先把 JSON 接口当文档导航
  一次再 fetch 一遍，等于**双倍请求**打服务端，反而放大频控/封禁风险。
- **forbidden headers 走 CDP 层**：``Cookie`` / ``Referer`` 等是 fetch 规范
   forbidden header，在页面 fetch 的 headers 里设置会被浏览器静默剥离；
   Cookie 由 ``context.add_cookies`` 注入，Referer 由同域落地页自动携带，
  source 传入的额外头通过 ``page.set_extra_http_headers``（CDP 层，不受
  forbidden 限制）生效。
- **启动预热**：开一个临时 Page 访问 ``WARMUP_PAGES``（东财主站），让 WAF
  看到正常的页面浏览行为、建立匿名会话 Cookie；失败不阻塞主流程。
"""
from __future__ import annotations

import json
import random
import re
import time
from typing import Any, Mapping
from urllib.parse import urlencode, urlparse

from loguru import logger

from .http import CHROME_UA, HttpError

#: fetch 规范中的 forbidden header names —— 页面 fetch() 的 headers 里设置这些
#: 会被浏览器静默剥离（不报错也不生效）。Cookie/Referer 必须走 context /
#: set_extra_http_headers / 同域落地页，见模块 docstring。
FORBIDDEN_HEADERS = frozenset(
    {
        "cookie", "cookie2", "referer", "origin", "host", "date", "dnt",
        "expect", "keep-alive", "te", "trailer", "transfer-encoding",
        "upgrade", "via",
    }
)


def _clean_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """剔除页面 fetch 会静默剥离的 forbidden headers（含 proxy-/sec- 前缀）。

    source 传入的 Cookie / Referer 实际通过 ``page.set_extra_http_headers``
    （CDP 层）与 context cookie 生效，在 fetch headers 里传是无效的。
    """
    out: dict[str, str] = {}
    for k, v in (headers or {}).items():
        lk = k.lower()
        if lk in FORBIDDEN_HEADERS or lk.startswith(("proxy-", "sec-")):
            continue
        out[k] = v
    return out

#: 启动后预热落地的页面（建立会话 Cookie / 过 JS 挑战）；API 请求仍在各 API
#: 域自己的同源 Page 内 fetch（预热页不参与 API 请求）。
WARMUP_PAGES: tuple[str, ...] = ("https://www.eastmoney.com/",)

#: 注入到每个新文档的 stealth 脚本，抹除常见自动化特征
STEALTH_JS = """
(function () {
  try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch (e) {}
  try {
    // 伪造 chrome 运行时对象，避免被检测为无头
    window.navigator.chrome = { runtime: {}, app: { isInstalled: false }, webstore: {} };
  } catch (e) {}
  try {
    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en-US', 'en'] });
  } catch (e) {}
  try {
    // 伪造插件列表（真实 Chrome 通常非空）
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
  } catch (e) {}
  try {
    const orig = window.navigator.permissions.query;
    window.navigator.permissions.query = (params) =>
      params.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission, onchange: null })
        : orig(params);
  } catch (e) {}
  try {
    // 抹掉 headless 下 window 上的 cdc_ 等特征变量
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
  } catch (e) {}
})();
"""


class BrowserClient:
    """基于 Playwright 的无头浏览器抓取客户端（单浏览器实例、多源复用、按域常驻 Page）。"""

    def __init__(
        self,
        headless: bool = True,
        stealth: bool = True,
        min_interval: float = 1.2,
        timeout: int = 30000,
        ua: str = CHROME_UA,
        cookies: list[tuple[str, str]] | None = None,
        warmup: bool = True,
    ) -> None:
        from playwright.sync_api import sync_playwright

        self.timeout = timeout
        self.ua = ua
        self._min_interval = max(0.0, min_interval)
        self._last_req = 0.0

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-infobars",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ],
        )
        self._ctx = self._browser.new_context(
            user_agent=ua,
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            device_scale_factor=2,
            extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        )
        if stealth:
            self._ctx.add_init_script(STEALTH_JS)
        if cookies:
            self._inject_cookies(cookies)
        #: origin -> 常驻 Page（按域缓存复用，见模块 docstring）
        self._pages: dict[str, Any] = {}
        logger.info(
            f"无头浏览器已启动（headless={headless}, stealth={stealth}, "
            f"限速 {self._min_interval}s，UA={ua[:40]}…）"
        )
        if warmup:
            self._warmup()

    # ── 限速 / 拟人 / Cookie ────────────────────────────────────────────────
    def _wait(self) -> None:
        if self._min_interval <= 0:
            return
        delta = time.monotonic() - self._last_req
        if delta < self._min_interval:
            time.sleep(self._min_interval - delta)
        self._last_req = time.monotonic()

    @staticmethod
    def _humanize(page) -> None:
        """模拟真人操作：随机移动鼠标 + 随机滚动 + 随机停留。"""
        try:
            page.mouse.move(random.randint(200, 1100), random.randint(150, 720))
            if random.random() < 0.6:
                page.mouse.wheel(0, random.randint(-120, 200))
        except Exception:
            pass
        time.sleep(random.uniform(0.25, 0.8))

    def _inject_cookies(self, cookies: list[tuple[str, str]]) -> None:
        for domain, cookie_str in cookies:
            if not cookie_str:
                continue
            parsed = []
            for part in cookie_str.split(";"):
                part = part.strip()
                if "=" not in part:
                    continue
                k, v = part.split("=", 1)
                parsed.append(
                    {
                        "name": k.strip(),
                        "value": v.strip(),
                        "domain": domain,
                        "path": "/",
                        # 前导点域（.eastmoney.com）即域+全部子域; https 站点统一 secure
                        "secure": True,
                    }
                )
            if parsed:
                try:
                    self._ctx.add_cookies(parsed)
                except Exception as exc:  # pragma: no cover - 防御性
                    logger.warning(f"注入 Cookie 失败（domain={domain}）：{exc}")

    # ── 页面管理（按域常驻 + 失效重建）────────────────────────────────────
    @staticmethod
    def _origin_of(url: str) -> str:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"

    @staticmethod
    def _build_url(url: str, params: Mapping[str, Any] | None) -> str:
        """把 query 参数拼到 URL（数据源常以 params= 传入，需与 curl 客户端行为一致）。"""
        if not params:
            return url
        sep = "&" if "?" in url else "?"
        return url + sep + urlencode(params, doseq=True)

    def _close_page(self, origin: str) -> None:
        page = self._pages.pop(origin, None)
        if page is not None:
            try:
                page.close()
            except Exception:
                pass

    def _page_for(self, origin: str):
        """取该域的常驻 Page：缓存命中且 origin 未漂移则复用；否则新建并落地站点根。

        站点 JS 可能把 Page 导航到别的域（跨域漂移）或关掉它，命中缓存时都
        要校验；重建后照常先 goto 落地页（失败也继续，fetch 仍可能可用）。
        """
        page = self._pages.get(origin)
        if page is not None:
            try:
                if not page.is_closed() and self._origin_of(page.url) == origin:
                    return page
            except Exception:
                pass
            self._close_page(origin)
        page = self._ctx.new_page()
        self._pages[origin] = page
        try:
            page.goto(origin + "/", wait_until="domcontentloaded", timeout=self.timeout)
            self._humanize(page)
        except Exception as exc:
            logger.debug(f"[browser] 落地页 {origin}/ 打开失败（仍尝试同源 fetch）：{exc}")
        return page

    def _warmup(self) -> None:
        """启动预热：访问主站首页，让 WAF 看到正常浏览行为、建立会话 Cookie。"""
        for url in WARMUP_PAGES:
            try:
                self._wait()
                page = self._ctx.new_page()
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=self.timeout)
                    self._humanize(page)
                finally:
                    page.close()
                logger.info(f"[browser] 预热完成 {url}")
            except Exception as exc:
                logger.debug(f"[browser] 预热失败 {url}：{exc}")

    # ── 页面内 fetch（核心） ───────────────────────────────────────────────
    def _page_fetch(
        self,
        page,
        url: str,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        body: str | None = None,
    ) -> str:
        """在已落地的同源 Page 内执行 fetch，返回响应文本。

        forbidden headers（Cookie/Referer/Sec-*…）在页面 fetch 里会被浏览器
        剥离，这里直接剔除以免误导；它们通过 set_extra_http_headers / context
        cookie / 同域 Referer 生效。
        """
        h = _clean_headers(headers)
        body_js = f", body: {json.dumps(body)}" if body is not None else ""
        js = (
            "(async () => {"
            f"  const resp = await fetch({json.dumps(url)}, {{"
            f"    method: {json.dumps(method)},"
            f"    headers: {json.dumps(h)},"
            f"    credentials: 'include'{body_js}"
            "  });"
            "  return await resp.text();"
            "})()"
        )
        return page.evaluate(js)

    def _fetch_with_retry(
        self,
        url: str,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        body: str | None = None,
    ) -> str:
        """同源 fetch + 一次重建重试。

        [2026-09-23] 雪球登录态修复经验：页面 domcontentloaded 后站点 JS 仍可能
        触发跳转/刷新，把 evaluate 的 JS 上下文销毁（"Execution context was
        destroyed"）。对策：失败即关闭该域 Page、重建落地后重试一轮，两轮都
        失败才抛 HttpError。
        """
        origin = self._origin_of(url)
        last_exc: Exception | None = None
        for attempt in (0, 1):
            page = self._page_for(origin)
            try:
                if headers:
                    page.set_extra_http_headers(dict(headers))
                return self._page_fetch(page, url, method, headers, body)
            except Exception as exc:
                last_exc = exc
                self._close_page(origin)
                logger.warning(f"[browser] {method} {url} 第 {attempt + 1} 轮失败，重建页面重试：{exc}")
        raise HttpError(f"{method} {url} 浏览器抓取失败：{last_exc}")

    # ── 公开接口（与 HttpClient 同签名） ────────────────────────────────────
    def get_text(self, url: str, **kwargs: Any) -> str:
        """抓 HTML 页面文本：goto 导航（会执行页面 JS，可过 WAF 挑战）+ content()。

        导航失败时回退同源 fetch（对服务端渲染的静态榜单页等价）。
        """
        url = self._build_url(url, kwargs.get("params"))
        self._wait()
        headers = kwargs.get("headers")
        origin = self._origin_of(url)
        page = self._page_for(origin)
        try:
            if headers:
                page.set_extra_http_headers(dict(headers))
            page.goto(url, wait_until="load", timeout=self.timeout)
            self._humanize(page)
            return page.content()
        except Exception as exc:
            logger.debug(f"[browser] GET(导航) {url} 失败，回退 fetch：{exc}")
            return self._fetch_with_retry(url, "GET", headers)
        finally:
            try:
                if page.is_closed():
                    self._close_page(origin)
            except Exception:
                self._close_page(origin)

    def get_json(self, url: str, **kwargs: Any) -> Any:
        url = self._build_url(url, kwargs.get("params"))
        self._wait()
        text = self._fetch_with_retry(url, "GET", kwargs.get("headers"))
        return self._parse_json(text, url)

    def post_json(
        self, url: str, payload: Any, headers: Mapping[str, str] | None = None
    ) -> Any:
        self._wait()
        body = payload if isinstance(payload, str) else json.dumps(payload)
        text = self._fetch_with_retry(url, "POST", headers, body)
        return self._parse_json(text, url)

    @staticmethod
    def _parse_json(text: str, url: str) -> Any:
        if isinstance(text, (dict, list)):
            return text
        try:
            return json.loads(text)
        except Exception:
            pass
        # 兜底：从 HTML 包裹中提取首个 JSON 对象 / 数组
        m = re.search(r"(\{.*\}|\[.*\])", text or "", re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
        raise HttpError(f"{url} 响应不是合法 JSON（前 200 字：{(text or '')[:200]!r}）")

    def fetch_status(
        self, url: str, method: str = "GET",
        headers: Mapping[str, str] | None = None, body: str = "",
    ) -> tuple[int, str]:
        """底层原始访问：不因状态码抛异常，直接返回 (status_code, text)。见 Fetcher 协议注释。

        用页面内 fetch()（浏览器原生 fetch 对 4xx/5xx 不 reject，只在网络层
        失败才 reject）拿到真实 HTTP 状态码 —— get_json/post_json 只返回解析
        后的 body，拿不到状态码，某些接口的成功/失败恰恰编码在状态码里
        （如同花顺追踪接口，见 sources/tonghuashun.py::follow_expert）。
        """
        self._wait()
        origin = self._origin_of(url)
        last_exc: Exception | None = None
        for attempt in (0, 1):
            page = self._page_for(origin)
            try:
                if headers:
                    page.set_extra_http_headers(dict(headers))
                h = _clean_headers(headers)
                body_js = f", body: {json.dumps(body)}" if body else ""
                js = (
                    "(async () => {"
                    f"  const resp = await fetch({json.dumps(url)}, {{"
                    f"    method: {json.dumps(method)},"
                    f"    headers: {json.dumps(h)},"
                    f"    credentials: 'include'{body_js}"
                    "  });"
                    "  const text = await resp.text();"
                    "  return {status: resp.status, text: text};"
                    "})()"
                )
                result = page.evaluate(js)
                return int(result.get("status", 0)), str(result.get("text") or "")
            except Exception as exc:
                last_exc = exc
                self._close_page(origin)
                logger.warning(f"[browser] {method} {url} 第 {attempt + 1} 轮失败，重建页面重试：{exc}")
        raise HttpError(f"{method} {url} 浏览器执行失败：{last_exc}")

    def warm(self, url: str, headers: Mapping[str, str] | None = None) -> None:
        """访问页面以建立会话 Cookie（如雪球 xq_a_token）。"""
        try:
            self._wait()
            origin = self._origin_of(url)
            page = self._page_for(origin)
            if headers:
                page.set_extra_http_headers(dict(headers))
            page.goto(url, wait_until="load", timeout=self.timeout)
            self._humanize(page)
        except Exception as exc:  # pragma: no cover - 防御性
            logger.warning(f"预热请求失败 {url}：{exc}")

    def close(self) -> None:
        for origin in list(self._pages):
            self._close_page(origin)
        for step in ("_ctx", "_browser", "_pw"):
            obj = getattr(self, step, None)
            if obj is None:
                continue
            try:
                obj.close()
            except Exception:
                pass
            setattr(self, step, None)
