@echo off
chcp 65001 >nul
title 安装栀夏客服 Agent（Windows）
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
if errorlevel 1 (
  echo.
  echo 安装失败，请查看上方错误信息。
)
pause
