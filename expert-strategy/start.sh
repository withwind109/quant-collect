#!/usr/bin/env bash
# =============================================================================
# expert-platform 一键启动：venv → 依赖 → chromium → 服务
#
#   ./start.sh              # 安装（如缺）+ 启动服务（默认 8601）
#   ./start.sh 8602         # 指定端口
#   ./start.sh --install    # 只装依赖和浏览器，不启动
#
# 数据与配置：
#   .env            东财 Cookie 等敏感配置（首次运行从 .env.example 拷贝）
#   config.yaml     抓取/浏览器/数据源配置
#   data/*.db       SQLite 数据库（随 git 提交，可随时删除重建）
#
# 网络说明（国内环境）：
#   pip            全局 ~/.config/pip/pip.conf 已固定阿里云源 + timeout/retries
#   Chromium       默认走 npmmirror 镜像（cdn.playwright.dev 国内仅 ~24kB/s），
#                  失败自动回退官方 CDN；可用环境变量 PLAYWRIGHT_DOWNLOAD_HOST 覆盖
# =============================================================================
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
PORT="${1:-8601}"
PYTHON="${PYTHON:-python3}"

log() { printf '\033[0;36m[start]\033[0m %s\n' "$*"; }
err() { printf '\033[0;31m[start]\033[0m %s\n' "$*" >&2; }

# ── 1. 虚拟环境（.venv/；兼容迁移旧的 .env/ venv） ─────────────────────────
# 历史版本把 venv 放在 .env/，与 dotenv 配置文件 .env 同名（macOS 大小写不敏感
# 文件系统上无法共存，load_dotenv('.env') 会静默失败导致 Cookie 加载不上）——
# 一次性迁移到 .venv/（.gitignore 已覆盖），之后统一用 .venv/
if [ -x ".env/bin/python" ] && [ ! -x ".venv/bin/python" ]; then
  log "迁移旧虚拟环境 .env/ → .venv/（避免与 dotenv 配置 .env 同名冲突）"
  mv .env .venv
fi
if [ -x ".venv/bin/python" ]; then
  VENV_PY=".venv/bin/python"
else
  log "创建虚拟环境 .venv/ …"
  "$PYTHON" -m venv .venv || {
    err "venv 创建失败：请先安装 python3-venv（如 apt install python3-venv / brew python）"
    exit 1
  }
  VENV_PY=".venv/bin/python"
fi
[ -x "$VENV_PY" ] || { err "venv 不可用：$VENV_PY 不存在"; exit 1; }

# ── 2. 依赖 ────────────────────────────────────────────────────────────────
log "安装 Python 依赖（requirements.txt，阿里云源）…"
# 剥掉宿主注入的 PYTHONPATH shim（broker IPC 会让 pip 偶发 EEXIST）
# --timeout/--retries 防止单个包下载卡死；全局 pip.conf 已含同样设置，双保险
env -u PYTHONPATH "$VENV_PY" -m pip install -q --timeout 60 --retries 5 -r requirements.txt

# ── 3. Chromium（无头浏览器抓取通道；失败不阻塞，自动回退 curl 指纹伪装） ───
if "$VENV_PY" -c "import playwright" 2>/dev/null; then
  if [ ! -d "$HOME/Library/Caches/ms-playwright" ] && [ ! -d "$HOME/.cache/ms-playwright" ]; then
    # npmmirror 镜像：实测 3.4MB/s（官方 CDN 国内约 24kB/s，185MB 要 2 小时+）
    PLAYWRIGHT_DOWNLOAD_HOST="${PLAYWRIGHT_DOWNLOAD_HOST:-https://registry.npmmirror.com/-/binary/playwright}"
    log "下载 Chromium（无头浏览器通道，约 286MB，npmmirror 镜像）…"
    if env -u PYTHONPATH PLAYWRIGHT_DOWNLOAD_HOST="$PLAYWRIGHT_DOWNLOAD_HOST" \
        "$VENV_PY" -m playwright install chromium; then
      log "Chromium 安装完成（镜像）"
    else
      err "镜像下载失败，回退官方 CDN（国内可能极慢，属正常现象）…"
      env -u PYTHONPATH "$VENV_PY" -m playwright install chromium || \
        err "Chromium 下载失败；服务仍可启动（config.yaml 里 browser.enabled=false 可显式关闭）"
    fi
  else
    log "Chromium 已存在（$HOME/Library/Caches/ms-playwright），跳过下载"
  fi
else
  err "playwright 未安装（应在依赖内）；浏览器通道不可用，将走 curl 后端"
fi

# ── 4. 敏感配置 ────────────────────────────────────────────────────────────
if [ ! -f ".env" ]; then
  cp .env.example .env
  log "已从 .env.example 生成 .env —— 请填入东财 Cookie（EASTMONEY_COOKIE）后重启"
fi

if [ "${1:-}" = "--install" ]; then
  log "依赖安装完成（--install 模式，不启动服务）"
  exit 0
fi

# ── 5. 启动服务 ────────────────────────────────────────────────────────────
log "启动 Web 服务 → http://127.0.0.1:${PORT}"
log "  社区高手     #/experts"
log "  高手跟随策略 #/expert-strategy"
exec env -u PYTHONPATH "$VENV_PY" -m uvicorn web.app:app --host 127.0.0.1 --port "$PORT"
