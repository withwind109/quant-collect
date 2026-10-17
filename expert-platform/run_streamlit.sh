#!/usr/bin/env bash
# 启动 Streamlit UI（社区高手 + 高手跟随策略）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/.venv"
PORT="${1:-8501}"

if [[ ! -f "$VENV_DIR/bin/activate" ]]; then
    echo "虚拟环境不存在，请先创建:" >&2
    echo "  python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt" >&2
    exit 1
fi

source "$VENV_DIR/bin/activate"

# 宿主注入的 PYTHONPATH shim 会把每次 open()/mkdir 包一层 broker IPC，先剥掉
# （与 multi-factor-stock 的 run_streamlit.sh 同款防护）
unset PYTHONPATH

echo "使用虚拟环境: $VENV_DIR"
echo "启动 Streamlit: src/ui/app.py"
echo "监听端口: $PORT"

export PYTHONPATH="$SCRIPT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
exec python -m streamlit run src/ui/app.py --server.port "$PORT"
