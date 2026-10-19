"""core/broker_account.py — 账户中心（东财登录态管理，SQLite 版）。

从 xcq-platform scripts/dashboard/account_manager.py 移植（2026-10-19），差异：
  * 存储: MySQL xcq_broker_accounts → 本地 SQLite experts.db 的 broker_accounts 表
  * 告警: 企微推送移除（改由 Web 页徽章 + 日志体现）
  * 登录模式: 仅扫码（账密在无头下大概率触发滑块，砍掉）
  * 时间: DATE_ADD(NOW(),...) → SQLite datetime('now','localtime',...)

职责:
  * 登录会话: Playwright 打开东财统一登录 → passport2 二维码 → 前端轮询截图 →
    用户手机扫码 → 捕获 storage_state + rtV3 令牌四元组 → 入库
  * 令牌捕获: 监听对 spzhapi.dfcfs.cn / emdcspzhapi.dfcfs.cn 的请求，从
    query/body 提取 ctToken/utToken/userId/deviceId；未捕获到时用 cookie 兜底映射
    （ct→ctToken, ut→utToken, pi→userId, sid→deviceId，xcq 已实测 rtV3 认证通过）
  * 心跳保活: 定期用 storage_state 打开落地页验证登录态，顺带续期 cookie 快照与令牌
  * 供给: active_tokens() 给 pipeline._build_eastmoney 兜底填 rtV3 令牌
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core import db as DB
from loguru import logger
from paths import BROKER_DIR, DB_PATH

EM_LOGIN_URL = "https://login.eastmoney.com/login.aspx?backurl=https%3A%2F%2Fgroup.eastmoney.com%2F"
EM_CHECK_URL = "https://group.eastmoney.com/"          # 心跳落地页（登录态下才是「我的」视图）
TOKEN_CAPTURE_HOSTS = ("spzhapi.dfcfs.cn", "emdcspzhapi.dfcfs.cn")
TOKEN_KEYS = ("ctToken", "utToken", "userId", "deviceId", "ct_token", "ut_token", "user_id", "device_id")
REQUIRED_TOKEN_KEYS = ("ctToken", "utToken", "userId")

_HEARTBEAT_MIN = int(os.getenv("BROKER_HEARTBEAT_MIN", "30"))

_DDL = [
    """CREATE TABLE IF NOT EXISTS broker_accounts (
        id TEXT PRIMARY KEY,
        platform TEXT NOT NULL,
        alias TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        em_user_id TEXT DEFAULT '',
        storage_state_path TEXT DEFAULT '',
        tokens_json TEXT,
        last_heartbeat_at TEXT,
        last_error TEXT DEFAULT '',
        keepalive_enabled INTEGER DEFAULT 1,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS broker_account_log (
        ts TEXT NOT NULL,
        account_id TEXT,
        event TEXT,
        detail TEXT DEFAULT ''
    )""",
]


def _ensure_tables() -> None:
    DB.ensure_table(DB_PATH, "broker_accounts", _DDL[0])
    DB.ensure_table(DB_PATH, "broker_account_log", _DDL[1])


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log_event(account_id: str, event: str, detail: str = "") -> None:
    try:
        _ensure_tables()
        DB.qexec("INSERT INTO broker_account_log (ts, account_id, event, detail) VALUES (?,?,?,?)",
                 (_now(), account_id, event, str(detail)[:500]), db_path=DB_PATH)
    except Exception as exc:  # 审计失败不影响主流程
        logger.warning(f"[broker] 审计写入失败 {account_id}/{event}: {exc}")


def _new_account_id(platform: str, alias: str) -> str:
    h = hashlib.md5(f"{platform}:{alias}:{time.time()}".encode()).hexdigest()[:8]
    return f"{'em' if platform == 'eastmoney' else 'ths'}_{h}"


def list_accounts() -> List[Dict[str, Any]]:
    _ensure_tables()
    rows = DB.q("SELECT * FROM broker_accounts ORDER BY created_at", db_path=DB_PATH)
    out = []
    for r in rows:
        a = dict(r)
        a.pop("tokens_json", None)                     # 脱敏
        a["has_tokens"] = _tokens_complete(r.get("tokens_json") or "")
        out.append(a)
    return out


def get_account(account_id: str) -> Optional[Dict[str, Any]]:
    _ensure_tables()
    return DB.qone("SELECT * FROM broker_accounts WHERE id=?", (account_id,), db_path=DB_PATH)


def account_logs(account_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    _ensure_tables()
    return DB.q("SELECT * FROM broker_account_log WHERE account_id=? "
                "ORDER BY ts DESC LIMIT ?", (account_id, int(limit)), db_path=DB_PATH)


def _tokens_complete(tokens_json: str) -> bool:
    try:
        t = json.loads(tokens_json or "{}")
        return isinstance(t, dict) and all(str(t.get(k) or "").strip() for k in REQUIRED_TOKEN_KEYS)
    except Exception:
        return False


def check_login(platform: str = "eastmoney") -> Dict[str, Any]:
    """前端爬取前快速探测: 是否有在线且令牌齐全的账户。"""
    _ensure_tables()
    row = DB.qone("SELECT id, alias, status, tokens_json, last_heartbeat_at, last_error "
                  "FROM broker_accounts WHERE platform=? AND status='active' "
                  "ORDER BY last_heartbeat_at DESC", (platform,), db_path=DB_PATH)
    if not row:
        return {"logged_in": False, "reason": "no_account"}
    if not _tokens_complete(row.get("tokens_json") or ""):
        return {"logged_in": False, "reason": "no_tokens", "account_id": row["id"], "alias": row["alias"]}
    return {"logged_in": True, "account_id": row["id"], "alias": row["alias"],
            "last_heartbeat_at": row.get("last_heartbeat_at")}


def active_tokens(platform: str = "eastmoney") -> Optional[Dict[str, str]]:
    """取登录态账户的实盘令牌（pipeline 兜底供给）；无可用账户/令牌不全时返回 None。"""
    _ensure_tables()
    rows = DB.q("SELECT id, tokens_json FROM broker_accounts "
                "WHERE platform=? AND status='active' AND tokens_json IS NOT NULL "
                "ORDER BY last_heartbeat_at DESC", (platform,), db_path=DB_PATH)
    for r in rows:
        try:
            tokens = json.loads(r["tokens_json"] or "{}")
        except Exception:
            continue
        if isinstance(tokens, dict) and all(str(tokens.get(k) or "").strip() for k in REQUIRED_TOKEN_KEYS):
            out = {k: str(v) for k, v in tokens.items() if v}
            out["account_id"] = r["id"]
            return out
    return None


# ─────────────────────────────────────────────────────────────────────────
# 登录会话: 独立线程持有 playwright（sync API 必须单线程）
# ─────────────────────────────────────────────────────────────────────────
class LoginSession:
    """一次扫码登录流程。生命周期: start() → 轮询 snapshot()/status() → 成功自动入库 / cancel()。"""

    _QR_IMG_SELECTORS = [
        "img[src*=qrcode i]", "img[id*=qrcode i]", "img[class*=qrcode i]",
        "img[src*=ewm i]", "img[class*=ewm i]", "#ewmImg", "[class*=ewm-code i]",
    ]

    def __init__(self, platform: str, alias: str) -> None:
        self.session_id = uuid.uuid4().hex[:12]
        self.platform = platform
        self.alias = alias
        self.status = "starting"          # starting/waiting/success/failed/cancelled
        self.detail = ""
        self.account_id: Optional[str] = None
        self.error = ""
        self._shot = b""                  # 最新截图（base64 前的原始 png）
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"login-{self.session_id}")

    # ── 对外 API（线程安全） ─────────────────────────────────────────────
    def start(self) -> None:
        self._thread.start()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            img = base64.b64encode(self._shot).decode() if self._shot else ""
        return {"session_id": self.session_id, "status": self.status,
                "detail": self.detail, "image": img}

    def status(self) -> Dict[str, Any]:
        return {"session_id": self.session_id, "status": self.status,
                "detail": self.detail, "account_id": self.account_id, "error": self.error}

    def cancel(self) -> None:
        self._stop.set()
        self.status = "cancelled"

    # ── 内部 ────────────────────────────────────────────────────────────
    def _shot_save(self, page) -> None:
        try:
            png = page.screenshot()
            with self._lock:
                self._shot = png
        except Exception:
            pass

    def _open_qr_page(self, page) -> None:
        """从 login 落地页进入带二维码的 passport2 独立登录页（xcq 2026-09-25 实测路径）。"""
        page.wait_for_timeout(2500)
        if "passport2.eastmoney.com/pub/login" in (page.url or ""):
            return
        clicked = False
        for sel in ["text=登录", "a:has-text('登录')"]:
            try:
                for el in page.query_selector_all(sel):
                    bb = el.bounding_box()
                    if bb and bb["width"] < 120 and bb["y"] < 120:
                        el.click()
                        clicked = True
                        break
            except Exception:
                continue
            if clicked:
                break
        if clicked:
            try:
                page.wait_for_url("**/pub/login**", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(2500)
        self.detail = f"登录页: {page.url[:60]}"

    def _shot_qr_or_page(self, page) -> None:
        """优先截二维码元素本体（清晰）；找不到就全页截图。"""
        for f in page.frames:
            for sel in self._QR_IMG_SELECTORS:
                try:
                    el = f.query_selector(sel)
                except Exception:
                    continue
                if not el:
                    continue
                try:
                    if not el.is_visible():
                        continue
                    box = el.bounding_box()
                    if box and box["width"] >= 60 and box["height"] >= 60:
                        with self._lock:
                            self._shot = el.screenshot()
                        return
                except Exception:
                    continue
        self._shot_save(page)

    def _run(self) -> None:
        from playwright.sync_api import sync_playwright
        pw = None
        try:
            pw = sync_playwright().start()
            browser = pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
                      "--disable-dev-shm-usage", "--disable-infobars"],
            )
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                viewport={"width": 1100, "height": 760}, locale="zh-CN", timezone_id="Asia/Shanghai",
            )
            ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
            captured: List[Dict[str, Any]] = []
            self._attach_token_listener(ctx, captured)
            page = ctx.new_page()
            page.goto(EM_LOGIN_URL, timeout=45000, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            self._shot_save(page)

            self._open_qr_page(page)
            self.status = "waiting"
            self.detail = "请用东财APP「扫一扫」扫描页面左侧二维码"

            # 轮询登录成功: 跳出 login 域 + group 域(backurl)或身份 cookie（xcq 2026-09-25 校准）
            deadline = time.time() + 300   # 5 分钟
            while time.time() < deadline and not self._stop.is_set():
                page.wait_for_timeout(2000)
                try:
                    self._shot_qr_or_page(page)
                except Exception:
                    pass
                url = page.url or ""
                cookies = {c["name"]: c["value"] for c in ctx.cookies()}
                left_login = ("passport2.eastmoney.com" not in url
                              and "login.eastmoney.com" not in url
                              and "pub/login" not in url)
                auth_cookie = any(k in cookies for k in ("ct", "ut", "sid", "pi"))
                logged_in = left_login and ("group.eastmoney.com" in url or auth_cookie)
                self.detail = f"url={url[:70]} cookie: {','.join(sorted(cookies.keys()))[:110]}"
                if logged_in:
                    self._finish_success(ctx, page, captured)
                    return
            if not self._stop.is_set():
                self.status = "failed"
                self.error = "登录超时（5分钟）"
        except Exception as exc:
            logger.warning(f"[broker] 登录会话异常 {self.session_id}: {exc}")
            self.status, self.error = "failed", str(exc)[:300]
        finally:
            try:
                if pw:
                    pw.stop()
            except Exception:
                pass

    @staticmethod
    def _attach_token_listener(ctx, captured: List[Dict[str, Any]]) -> None:
        def _on_request(req):
            try:
                url = req.url
                if any(h in url for h in TOKEN_CAPTURE_HOSTS):
                    params: Dict[str, str] = {}
                    if "?" in url:
                        for kv in url.split("?", 1)[1].split("&"):
                            if "=" in kv:
                                k, v = kv.split("=", 1)
                                if k in TOKEN_KEYS:
                                    params[k] = v
                    pd = req.post_data or ""
                    for k in TOKEN_KEYS:
                        if k in pd:
                            params.setdefault(k, pd.split(k + "=")[-1].split("&")[0][:128])
                    if params:
                        captured.append(params)
            except Exception:
                pass
        ctx.on("request", _on_request)

    def _finish_success(self, ctx, page, captured: List[Dict[str, Any]]) -> None:
        account_id = _new_account_id(self.platform, self.alias)
        # 1) storage_state 持久化（600 权限）
        acc_dir = BROKER_DIR / account_id
        acc_dir.mkdir(parents=True, exist_ok=True)
        ss_path = acc_dir / "storage_state.json"
        ctx.storage_state(path=str(ss_path))
        os.chmod(ss_path, 0o600)
        # 2) 令牌: 监听到的参数 > cookie 兜底映射
        tokens = self._extract_tokens(ctx, captured)
        cookies = {c["name"]: c["value"] for c in ctx.cookies()}
        em_user_id = tokens.get("userId") or cookies.get("uid") or ""
        # 3) 入库（同 alias 旧账户直接删除，避免同名堆积）
        _ensure_tables()
        DB.qexec("DELETE FROM broker_accounts WHERE platform=? AND alias=?",
                 (self.platform, self.alias), db_path=DB_PATH)
        DB.qexec("""INSERT INTO broker_accounts
                 (id, platform, alias, status, em_user_id, storage_state_path, tokens_json,
                  last_heartbeat_at, last_error, keepalive_enabled, created_at)
                 VALUES (?,?,?,'active',?,?,?,?, '', 1, ?)""",
                 (account_id, self.platform, self.alias, em_user_id, str(ss_path),
                  json.dumps(tokens, ensure_ascii=False) if tokens else None,
                  _now(), _now()), db_path=DB_PATH)
        _log_event(account_id, "login", f"mode=qr tokens={'yes' if tokens else 'cookies_only'}")
        self.account_id = account_id
        self.status = "success"
        self.detail = f"登录成功，账户ID {account_id}" + ("" if tokens else "（未捕获到 rtV3 令牌，请进入组合页后手动心跳一次）")
        try:
            self._shot_save(page)
        except Exception:
            pass
        logger.info(f"[broker] 账户登录成功: {account_id} ({self.alias}) tokens={bool(tokens)}")

    @staticmethod
    def _extract_tokens(ctx, captured: List[Dict[str, Any]]) -> Dict[str, str]:
        """优先取监听到的 rtV3 四元组；缺失字段用 passport2 cookie 兜底补齐
        （ct→ctToken, ut→utToken, pi→userId, sid→deviceId；xcq 已实测 rtV3 认证通过）。

        [2026-10-19 修] 监听到的 H5 请求常常只带 ctToken/utToken/userId 而无 deviceId，
        之前满足三个必需键就提前 return，跳过 cookie 兜底 → deviceId 恒为空，
        rtV3 返回 30001「用户暂未授权」（关注关系绑定 userId+deviceId）。
        现在 cookie 兜底始终执行，只补缺失字段、不覆盖已捕获值。"""
        tokens: Dict[str, str] = {}
        for item in captured:                      # 后到的覆盖
            for k, v in item.items():
                if k in ("ctToken", "ct_token"):
                    tokens.setdefault("ctToken", v)
                elif k in ("utToken", "ut_token"):
                    tokens.setdefault("utToken", v)
                elif k in ("userId", "user_id"):
                    tokens.setdefault("userId", v)
                elif k in ("deviceId", "device_id"):
                    tokens.setdefault("deviceId", v)
        try:
            cookies = {c["name"]: c["value"] for c in ctx.cookies()}
            if not tokens.get("ctToken") and cookies.get("ct"):
                tokens["ctToken"] = cookies["ct"]
            if not tokens.get("utToken") and cookies.get("ut"):
                tokens["utToken"] = cookies["ut"]
            if not tokens.get("userId"):
                pi = (cookies.get("pi") or "").split("%")[0].split(";")[0]
                if pi:
                    tokens["userId"] = pi
            if not tokens.get("deviceId") and cookies.get("sid"):
                tokens["deviceId"] = cookies["sid"]
        except Exception:
            pass
        return tokens


# ─────────────────────────────────────────────────────────────────────────
# 心跳保活
# ─────────────────────────────────────────────────────────────────────────
def heartbeat_once(account: Dict[str, Any]) -> bool:
    """单账户心跳: 用 storage_state 打开东财落地页验证登录态。返回是否仍在线。"""
    from playwright.sync_api import sync_playwright
    account_id = account["id"]
    ss_path = account.get("storage_state_path") or ""
    if not ss_path or not Path(ss_path).exists():
        _mark(account_id, "expired", "storage_state 文件缺失")
        _log_event(account_id, "heartbeat_fail", "storage_state 缺失")
        return False
    captured: List[Dict[str, Any]] = []
    pw = None
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            viewport={"width": 1100, "height": 760}, locale="zh-CN", timezone_id="Asia/Shanghai",
            storage_state=ss_path,
        )
        LoginSession._attach_token_listener(ctx, captured)
        page = ctx.new_page()
        resp = page.goto(EM_CHECK_URL, timeout=45000, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        url = page.url or ""
        cookies = {c["name"]: c["value"] for c in ctx.cookies()}
        auth_cookies = [k for k in ("ct", "ut", "pi", "sid") if cookies.get(k)]
        alive = bool(resp and resp.ok) and "login.eastmoney.com" not in url and bool(auth_cookies)
        if alive:
            ctx.storage_state(path=ss_path)      # 续期 cookie 快照
            os.chmod(ss_path, 0o600)
            tokens = LoginSession._extract_tokens(ctx, captured)
            if tokens:
                DB.qexec("UPDATE broker_accounts SET tokens_json=? WHERE id=?",
                         (json.dumps(tokens, ensure_ascii=False), account_id), db_path=DB_PATH)
                _log_event(account_id, "token_refresh", f"keys={sorted(tokens.keys())}")
            DB.qexec("UPDATE broker_accounts SET status='active', last_heartbeat_at=?, last_error='' "
                     "WHERE id=?", (_now(), account_id), db_path=DB_PATH)
            _log_event(account_id, "heartbeat_ok", "")
        else:
            DB.qexec("UPDATE broker_accounts SET status='expired', last_heartbeat_at=?, "
                     "last_error='心跳检测登录态失效' WHERE id=?", (_now(), account_id), db_path=DB_PATH)
            _log_event(account_id, "expired", f"url={url[:120]} cookies={auth_cookies}")
        return alive
    except Exception as exc:
        logger.warning(f"[broker] 心跳异常 {account_id}: {exc}")
        DB.qexec("UPDATE broker_accounts SET last_heartbeat_at=?, last_error=? WHERE id=?",
                 (_now(), str(exc)[:400], account_id), db_path=DB_PATH)
        return False
    finally:
        try:
            if pw:
                pw.stop()
        except Exception:
            pass


def _mark(account_id: str, status: str, err: str) -> None:
    DB.qexec("UPDATE broker_accounts SET status=?, last_error=?, last_heartbeat_at=? WHERE id=?",
             (status, err[:400], _now(), account_id), db_path=DB_PATH)


def _hb_loop() -> None:
    logger.info(f"[broker] 心跳保活线程启动 (interval={_HEARTBEAT_MIN}min)")
    while True:
        try:
            _ensure_tables()
            rows = DB.q("SELECT * FROM broker_accounts WHERE keepalive_enabled=1 "
                        "AND status IN ('active','expired')", db_path=DB_PATH)
            for r in rows:
                if r.get("status") == "expired" and r.get("last_heartbeat_at"):
                    # 已失效的账户不再高频重试: 每天最多探测 2 次
                    try:
                        last = datetime.strptime(str(r["last_heartbeat_at"]), "%Y-%m-%d %H:%M:%S")
                        if (datetime.now() - last).total_seconds() < 43200:
                            continue
                    except Exception:
                        pass
                try:
                    heartbeat_once(dict(r))
                except Exception as exc:
                    logger.warning(f"[broker] 心跳单账户失败 {r['id']}: {exc}")
        except Exception as exc:
            logger.warning(f"[broker] 心跳轮失败: {exc}")
        time.sleep(_HEARTBEAT_MIN * 60)


class AccountManager:
    """进程内单例: 管理登录会话 + 心跳线程。"""

    def __init__(self) -> None:
        self._sessions: Dict[str, LoginSession] = {}
        self._hb_started = False
        self._lock = threading.Lock()

    def start_heartbeat(self) -> None:
        with self._lock:
            if self._hb_started:
                return
            self._hb_started = True
        threading.Thread(target=_hb_loop, daemon=True, name="broker-heartbeat").start()

    def start_login(self, platform: str, alias: str) -> Dict[str, Any]:
        if platform != "eastmoney":
            return {"ok": False, "error": "当前仅支持东方财富（同花顺后续接入）"}
        alias = (alias or "").strip() or "东财账户"
        sess = LoginSession(platform, alias)
        with self._lock:
            for sid in [s for s, x in self._sessions.items()
                        if x.status in ("success", "failed", "cancelled")]:
                self._sessions.pop(sid, None)
            self._sessions[sess.session_id] = sess
        sess.start()
        return {"ok": True, "session_id": sess.session_id}

    def get_session(self, sid: str) -> Optional[LoginSession]:
        return self._sessions.get(sid)


account_manager = AccountManager()
