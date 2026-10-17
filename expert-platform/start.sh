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
# =============================================================================
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
PORT="${1:-8601}"
PYTHON="${PYTHON:-python3}"

log() { printf '\033[0;36m[start]\033[0m %s\n' "$*"; }
err() { printf '\033[0;31m[start]\033[0m %s\n' "$*" >&2; }

# ── 1. 虚拟环境（优先复用已有 .venv/；没有则建 .env/，与 multi-factor-stock 习惯一致） ──
if [ -x ".venv/bin/python" ]; then
  VENV_PY=".venv/bin/python"
elif [ ! -x ".env/bin/python" ]; then
  log "创建虚拟环境 .env/ …"
  "$PYTHON" -m venv .env
fi
# .env/bin/python 不存在于某些老 venv（python3-venv 未装）——兜底用系统解释器提示
if [ ! -x ".env/bin/python" ] && [ ! -x ".venv/bin/python" ]; then
  err "venv 创建失败：请先安装 python3-venv（如 apt install python3-venv / brew python）"
  exit 1
fi
[ -x ".venv/bin/python" ] && VENV_PY=".venv/bin/python" || VENV_PY=".env/bin/python"

# ── 2. 依赖 ────────────────────────────────────────────────────────────────
log "安装 Python 依赖（requirements.txt）…"
# 剥掉宿主注入的 PYTHONPATH shim（broker IPC 会让 pip 偶发 EEXIST）
env -u PYTHONPATH "$VENV_PY" -m pip install -q -r requirements.txt

# ── 3. Chromium（无头浏览器抓取通道；失败不阻塞，自动回退 curl 指纹伪装） ───
if ! "$VENV_PY" -c "import playwright" 2>/dev/null; then
  err "playwright 未安装（应在依赖内）；浏览器通道不可用，将走 curl 后端"
elif [ ! -d "$HOME/Library/Caches/ms-playwright" ] && [ ! -d "$HOME/.cache/ms-playwright" ]; then
  log "下载 Chromium（无头浏览器通道，约 100MB）…"
  env -u PYTHONPATH "$VENV_PY" -m playwright install chromium || \
    err "Chromium 下载失败；服务仍可启动（config.yaml 里 browser.enabled=false 可显式关闭）"
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
