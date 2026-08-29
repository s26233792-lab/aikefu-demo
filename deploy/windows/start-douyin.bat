@echo off
chcp 65001 >nul
title 启动栀夏客服 Agent（抖店飞鸽）
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-douyin.ps1"
if errorlevel 1 pause
