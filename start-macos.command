#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

if [ ! -x ".venv/bin/python" ]; then
  echo "尚未完成安装，请先双击 install-macos.command。"
  exit 1
fi

mkdir -p data
API_PID=""

cleanup() {
  if [ -n "$API_PID" ] && kill -0 "$API_PID" 2>/dev/null; then
    kill "$API_PID" 2>/dev/null || true
    wait "$API_PID" 2>/dev/null || true
  fi
  rm -f data/api-macos.pid
}
trap cleanup EXIT INT TERM

if curl --silent --fail http://127.0.0.1:18081/health >/dev/null 2>&1; then
  echo "决策 API 已在运行。"
else
  .venv/bin/python -u run.py web > data/api-macos.log 2>&1 &
  API_PID=$!
  echo "$API_PID" > data/api-macos.pid
  attempts=0
  until curl --silent --fail http://127.0.0.1:18081/health >/dev/null 2>&1; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 30 ] || ! kill -0 "$API_PID" 2>/dev/null; then
      echo "决策 API 启动失败，请查看 data/api-macos.log。"
      exit 1
    fi
    sleep 0.5
  done
fi

open http://127.0.0.1:18081/
echo "审批台已打开。按 Control+C 可安全停止本次服务。"
.venv/bin/python -u run.py qianfan
