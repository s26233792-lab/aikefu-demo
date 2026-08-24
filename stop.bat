@echo off
chcp 65001 >nul
title 停止千帆客服 Agent
echo 正在停止本项目的 API 与 Worker 窗口...
taskkill /FI "WINDOWTITLE eq 千帆客服API*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq 千帆客服Worker*" /T /F >nul 2>&1
echo 已停止本项目进程；千帆客户端和其他 Python 程序未受影响。
pause
