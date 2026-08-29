@echo off
chcp 65001 >nul
title 启动栀夏客服 Agent（Windows）
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
if errorlevel 1 (
  echo.
  echo 启动失败，请查看上方错误信息和 data 目录日志。
  pause
)
