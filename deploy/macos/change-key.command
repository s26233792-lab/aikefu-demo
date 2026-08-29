#!/bin/zsh
set -u

script_dir="$(cd "$(dirname "$0")" && pwd)"
project_dir="$(cd "$script_dir/../.." && pwd)"
keychain_service='aikefu-demo.deepseek'

new_key=$(/usr/bin/osascript -e 'text returned of (display dialog "请输入新的 DeepSeek API Key。密钥只保存在这台 Mac 的系统钥匙串中。" with title "更换 DeepSeek 密钥" default answer "" hidden answer buttons {"取消", "保存并重启"} default button "保存并重启")' 2>/dev/null || true)
if [[ -z "$new_key" ]]; then
  exit 0
fi

/usr/bin/security add-generic-password -U -a "$USER" -s "$keychain_service" -w "$new_key" >/dev/null
unset new_key

api_pid=$(/usr/sbin/lsof -tiTCP:18081 -sTCP:LISTEN 2>/dev/null | /usr/bin/head -n 1)
api_cwd=$(/usr/sbin/lsof -a -p "$api_pid" -d cwd -Fn 2>/dev/null | /usr/bin/sed -n 's/^n//p')
if [[ -n "$api_pid" && "$api_cwd" == "$project_dir" ]]; then
  /bin/kill "$api_pid" >/dev/null 2>&1
  /bin/sleep 1
fi

exec "$script_dir/start.command"
