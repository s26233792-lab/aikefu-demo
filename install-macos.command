#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "未找到 python3。请先安装 Python 3.11 或更高版本。"
  exit 1
fi

if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "当前 Python 版本过低，请安装 Python 3.11 或更高版本。"
  exit 1
fi

if [ ! -d ".venv" ]; then
  "$PYTHON_BIN" -m venv .venv
fi

source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[worker]'
python -m playwright install chromium

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "已创建 .env；需要 LLM 时请填写 DEEPSEEK_API_KEY。"
fi

echo
echo "安装完成。请确认已安装并登录官方千帆客服工作台，然后双击 start-macos.command。"
echo "官方下载：https://walle.xiaohongshu.com/client-update/"
