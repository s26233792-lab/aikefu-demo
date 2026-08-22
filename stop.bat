@echo off
chcp 65001 >nul
title 停止千帆客服 Agent
echo 正在停止所有相关进程...
taskkill /IM "千帆客服工作台.exe" /F >nul 2>&1
REM 停止 API 和 Worker（通过窗口标题）
for /f "tokens=2 delims=," %%p in ('tasklist /fi "imagename eq python.exe" /fo csv /nh') do (
  taskkill /PID %%p /F >nul 2>&1
)
echo 已停止。
pause
