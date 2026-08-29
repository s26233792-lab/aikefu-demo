#!/bin/zsh
set -u

script_dir="$(cd "$(dirname "$0")" && pwd)"
project_dir="$(cd "$script_dir/../.." && pwd)"

stop_pid_file() {
  local pid_file="$1"
  [[ -f "$pid_file" ]] || return 0
  local pid command
  pid=$(/bin/cat "$pid_file" 2>/dev/null)
  if [[ "$pid" == <-> ]] && /bin/kill -0 "$pid" >/dev/null 2>&1; then
    command=$(/bin/ps -p "$pid" -o command= 2>/dev/null)
    if [[ "$command" == *'run.py web'* || "$command" == *'run.py desktop'* ]]; then
      /bin/kill "$pid" >/dev/null 2>&1
    fi
  fi
  /bin/rm -f "$pid_file"
}

stop_pid_file "$project_dir/data/macos-worker.pid"
stop_pid_file "$project_dir/data/macos-api.pid"

quit_choice=$(/usr/bin/osascript -e 'button returned of (display dialog "API 和自动回复 Worker 已停止。是否同时退出千帆客服工作台？" with title "停止栀夏客服 Agent" buttons {"保留千帆", "退出千帆"} default button "保留千帆")' 2>/dev/null || true)
if [[ "$quit_choice" == '退出千帆' ]]; then
  /usr/bin/osascript -e 'tell application id "com.xhs.eva" to quit' >/dev/null 2>&1
fi

echo '栀夏客服 Agent 已停止。'
