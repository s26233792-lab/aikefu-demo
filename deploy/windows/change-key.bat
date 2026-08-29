@echo off
chcp 65001 >nul
title 更换 DeepSeek 密钥（Windows）
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0change-key.ps1"
if errorlevel 1 pause
