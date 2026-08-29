#!/bin/zsh
set -u

script_dir="$(cd "$(dirname "$0")" && pwd)"
project_dir="$(cd "$script_dir/../.." && pwd)"
qianfan_app='/Applications/千帆客服工作台.app'
agent_url='http://127.0.0.1:18081'
cdp_port='19222'
cdp_base_url="http://127.0.0.1:$cdp_port"
cdp_url="$cdp_base_url/json/version"
keychain_service='aikefu-demo.deepseek'
api_pid_file="$project_dir/data/macos-api.pid"
worker_pid_file="$project_dir/data/macos-worker.pid"

alert() {
  /usr/bin/osascript -e "display alert \"栀夏客服 Agent\" message \"$1\" as critical" >/dev/null
}

pid_running() {
  local pid_file="$1"
  local expected_command="$2"
  [[ -f "$pid_file" ]] || return 1
  local pid command
  pid=$(/bin/cat "$pid_file" 2>/dev/null)
  if [[ "$pid" == <-> ]] && /bin/kill -0 "$pid" >/dev/null 2>&1; then
    command=$(/bin/ps -p "$pid" -o command= 2>/dev/null)
    [[ "$command" == *"$expected_command"* ]] && return 0
  fi
  /bin/rm -f "$pid_file"
  return 1
}

cd "$project_dir" || exit 1
/bin/mkdir -p data

if [[ ! -x .venv/bin/python ]]; then
  alert '尚未安装依赖，请先双击 deploy/macos/install.command。'
  exit 1
fi

if [[ ! -d "$qianfan_app" ]]; then
  alert '没有在“应用程序”中找到千帆客服工作台。'
  exit 1
fi

if ! /usr/bin/security find-generic-password -s "$keychain_service" -w >/dev/null 2>&1; then
  deepseek_key=$(/usr/bin/osascript -e 'text returned of (display dialog "请输入 DeepSeek API Key。密钥只保存在这台 Mac 的系统钥匙串中。" with title "连接 DeepSeek" default answer "" hidden answer buttons {"取消", "保存并连接"} default button "保存并连接")' 2>/dev/null || true)
  if [[ -z "$deepseek_key" ]]; then
    exit 0
  fi
  /usr/bin/security add-generic-password -U -a "$USER" -s "$keychain_service" -w "$deepseek_key" >/dev/null
  unset deepseek_key
fi

health_json=$(/usr/bin/curl --silent --fail "$agent_url/health" 2>/dev/null || true)
if [[ -n "$health_json" && "$health_json" != *'"llm_ready":true'* ]]; then
  existing_pid=$(/usr/sbin/lsof -tiTCP:18081 -sTCP:LISTEN 2>/dev/null | /usr/bin/head -n 1)
  existing_cwd=$(/usr/sbin/lsof -a -p "$existing_pid" -d cwd -Fn 2>/dev/null | /usr/bin/sed -n 's/^n//p')
  if [[ -n "$existing_pid" && "$existing_cwd" == "$project_dir" ]]; then
    /bin/kill "$existing_pid" >/dev/null 2>&1
    /bin/sleep 1
  else
    alert '端口 18081 被其他程序占用，请先释放该端口。'
    exit 1
  fi
fi

if ! /usr/bin/curl --silent --fail "$agent_url/health" >/dev/null 2>&1; then
  deepseek_key=$(/usr/bin/security find-generic-password -s "$keychain_service" -w 2>/dev/null)
  if [[ -z "$deepseek_key" ]]; then
    alert '无法读取钥匙串中的 DeepSeek API Key，请运行 change-key.command 重新保存。'
    exit 1
  fi
  /usr/bin/nohup /usr/bin/env XHS_LLM_API_KEY="$deepseek_key" .venv/bin/python run.py web > data/macos-api.log 2>&1 &
  api_pid=$!
  print -r -- "$api_pid" > "$api_pid_file"
  unset deepseek_key
  for attempt in {1..30}; do
    health_json=$(/usr/bin/curl --silent --fail "$agent_url/health" 2>/dev/null || true)
    [[ "$health_json" == *'"llm_ready":true'* ]] && break
    /bin/sleep 0.5
  done
fi

health_json=$(/usr/bin/curl --silent --fail "$agent_url/health" 2>/dev/null || true)
if [[ "$health_json" != *'"llm_ready":true'* ]]; then
  alert '客服 API 未正常启动，请查看 data/macos-api.log。'
  exit 1
fi

if ! /usr/bin/curl --silent --fail "$cdp_url" >/dev/null 2>&1; then
  restart_choice=$(/usr/bin/osascript -e 'button returned of (display dialog "千帆需要开启本机配对端口。请先保存正在输入的内容，然后允许重启千帆。" with title "配对千帆客服" buttons {"取消", "重启并配对"} default button "重启并配对")' 2>/dev/null || true)
  if [[ "$restart_choice" != '重启并配对' ]]; then
    exit 0
  fi

  /usr/bin/osascript -e 'tell application id "com.xhs.eva" to quit' >/dev/null 2>&1
  for attempt in {1..20}; do
    /usr/bin/pgrep -f '^/Applications/千帆客服工作台\.app/Contents/MacOS/千帆客服工作台$' >/dev/null 2>&1 || break
    /bin/sleep 0.5
  done
  qianfan_pids=(${(f)"$(/usr/bin/pgrep -f '^/Applications/千帆客服工作台\.app/Contents/MacOS/千帆客服工作台$' 2>/dev/null)"})
  for qianfan_pid in $qianfan_pids; do
    /bin/kill "$qianfan_pid" >/dev/null 2>&1
  done
  /bin/sleep 1
  /usr/bin/open -na "$qianfan_app" --args --remote-debugging-port="$cdp_port" '--remote-allow-origins=*'
fi

for attempt in {1..40}; do
  /usr/bin/curl --silent --fail "$cdp_url" >/dev/null 2>&1 && break
  /bin/sleep 0.5
done

if ! /usr/bin/curl --silent --fail "$cdp_url" >/dev/null 2>&1; then
  alert '千帆配对端口没有启动，请完全退出千帆后重试。'
  exit 1
fi

if ! pid_running "$worker_pid_file" 'run.py desktop'; then
  /usr/bin/nohup /usr/bin/env PYTHONUNBUFFERED=1 XHS_DECISION_URL="$agent_url" XHS_CDP_URL="$cdp_base_url" .venv/bin/python run.py desktop > data/macos-worker.log 2>&1 &
  worker_pid=$!
  print -r -- "$worker_pid" > "$worker_pid_file"
fi

/usr/bin/open -a '千帆客服工作台'
/usr/bin/open "$agent_url"
/usr/bin/osascript -e 'display notification "DeepSeek、千帆和自动回复 Worker 已连接" with title "栀夏客服 Agent" sound name "Glass"' >/dev/null 2>&1
echo '栀夏客服 Agent 已启动。'
