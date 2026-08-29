#!/bin/zsh
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
project_dir="$(cd "$script_dir/../.." && pwd)"
keychain_service='aikefu-demo.deepseek'

alert() {
  /usr/bin/osascript -e "display alert \"栀夏客服 Agent\" message \"$1\" as critical" >/dev/null
}

cd "$project_dir"

python_bin="${PYTHON_BIN:-$(command -v python3 || true)}"
if [[ -z "$python_bin" ]]; then
  alert '没有找到 Python 3，请先安装 Python 3.11 或更高版本。'
  exit 1
fi

if ! "$python_bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  alert 'Python 版本过低，需要 Python 3.11 或更高版本。'
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  "$python_bin" -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .

if [[ ! -f .env ]]; then
  /bin/cp .env.example .env
fi

if ! /usr/bin/security find-generic-password -s "$keychain_service" -w >/dev/null 2>&1; then
  deepseek_key=$(/usr/bin/osascript -e 'text returned of (display dialog "请输入 DeepSeek API Key。密钥只保存在这台 Mac 的系统钥匙串中。" with title "配置 DeepSeek" default answer "" hidden answer buttons {"取消", "保存"} default button "保存")' 2>/dev/null || true)
  if [[ -z "$deepseek_key" ]]; then
    alert '尚未配置 DeepSeek API Key，安装已完成，但启动前仍需配置密钥。'
    exit 0
  fi
  /usr/bin/security add-generic-password -U -a "$USER" -s "$keychain_service" -w "$deepseek_key" >/dev/null
  unset deepseek_key
fi

/bin/mkdir -p data
/bin/chmod +x deploy/macos/*.command
/usr/bin/osascript -e 'display notification "依赖和 DeepSeek 配置已完成" with title "栀夏客服 Agent" sound name "Glass"' >/dev/null 2>&1
echo 'macOS 安装完成。请双击 deploy/macos/start.command 启动。'
