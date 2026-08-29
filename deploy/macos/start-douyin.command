#!/bin/zsh
set -u

script_dir="$(cd "$(dirname "$0")" && pwd)"
project_dir="$(cd "$script_dir/../.." && pwd)"
chrome_app='/Applications/Google Chrome.app'
agent_url='http://127.0.0.1:18081'
cdp_port='19223'
cdp_base_url="http://127.0.0.1:$cdp_port"
worker_pid_file="$project_dir/data/macos-douyin-worker.pid"

alert() {
  /usr/bin/osascript -e "display alert \"栀夏客服 Agent · 抖店\" message \"$1\" as critical" >/dev/null
}

pid_running() {
  [[ -f "$worker_pid_file" ]] || return 1
  local pid command
  pid=$(/bin/cat "$worker_pid_file" 2>/dev/null)
  if [[ "$pid" == <-> ]] && /bin/kill -0 "$pid" >/dev/null 2>&1; then
    command=$(/bin/ps -p "$pid" -o command= 2>/dev/null)
    [[ "$command" == *'run.py douyin'* ]] && return 0
  fi
  /bin/rm -f "$worker_pid_file"
  return 1
}

cd "$project_dir" || exit 1
/bin/mkdir -p data/douyin-profile

if [[ ! -x .venv/bin/python ]]; then
  alert '尚未安装依赖，请先双击 install.command。'
  exit 1
fi
if [[ ! -d "$chrome_app" ]]; then
  alert '没有在“应用程序”中找到 Google Chrome。'
  exit 1
fi
if ! /usr/bin/curl --silent --fail "$agent_url/health" >/dev/null 2>&1; then
  alert '栀夏客服 Agent 尚未启动。请先双击 start.command，再启动抖店。'
  exit 1
fi

if ! /usr/bin/curl --silent --fail "$cdp_base_url/json/version" >/dev/null 2>&1; then
  /usr/bin/open -na "$chrome_app" --args \
    --remote-debugging-port="$cdp_port" \
    '--remote-allow-origins=*' \
    '--no-first-run' \
    '--no-default-browser-check' \
    --user-data-dir="$project_dir/data/douyin-profile" \
    'https://fxg.jinritemai.com/'
fi

for attempt in {1..40}; do
  /usr/bin/curl --silent --fail "$cdp_base_url/json/version" >/dev/null 2>&1 && break
  /bin/sleep 0.5
done
if ! /usr/bin/curl --silent --fail "$cdp_base_url/json/version" >/dev/null 2>&1; then
  alert 'Chrome 配对端口没有启动，请完全退出专用 Chrome 窗口后重试。'
  exit 1
fi

# 某些 Chrome 首次启动只创建后台页，通过 CDP 明确打开抖店登录页。
if ! /usr/bin/curl --silent --fail "$cdp_base_url/json/list" | /usr/bin/grep -q '"type"[[:space:]]*:[[:space:]]*"page"'; then
  /usr/bin/curl --silent --fail -X PUT \
    "$cdp_base_url/json/new?https%3A%2F%2Ffxg.jinritemai.com%2F" >/dev/null 2>&1 || true
fi

if ! pid_running; then
  /usr/bin/nohup /usr/bin/env \
    PYTHONUNBUFFERED=1 \
    DOUYIN_DECISION_URL="$agent_url" \
    DOUYIN_CDP_URL="$cdp_base_url" \
    .venv/bin/python run.py douyin > data/macos-douyin-worker.log 2>&1 &
  worker_pid=$!
  print -r -- "$worker_pid" > "$worker_pid_file"
fi

/usr/bin/open -a 'Google Chrome'
/usr/bin/osascript -e 'display dialog "首次使用请在刚打开的专用 Chrome 中登录抖店，然后从商家后台进入“飞鸽客服”，并打开一个会话。登录态以后会保留。" with title "抖店飞鸽已配对" buttons {"知道了"} default button "知道了"' >/dev/null 2>&1
echo '抖店飞鸽 Worker 已启动。'
