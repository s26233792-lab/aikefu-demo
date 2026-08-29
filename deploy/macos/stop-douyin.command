#!/bin/zsh
set -u

script_dir="$(cd "$(dirname "$0")" && pwd)"
project_dir="$(cd "$script_dir/../.." && pwd)"
pid_file="$project_dir/data/macos-douyin-worker.pid"

if [[ -f "$pid_file" ]]; then
  pid=$(/bin/cat "$pid_file" 2>/dev/null)
  if [[ "$pid" == <-> ]] && /bin/kill -0 "$pid" >/dev/null 2>&1; then
    command=$(/bin/ps -p "$pid" -o command= 2>/dev/null)
    [[ "$command" == *'run.py douyin'* ]] && /bin/kill "$pid" >/dev/null 2>&1
  fi
  /bin/rm -f "$pid_file"
fi

echo '抖店飞鸽 Worker 已停止；专用 Chrome 可手动关闭。'
