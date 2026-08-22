@echo off
chcp 65001 >nul
title 小红书千帆客服 Agent - 一键启动
echo ============================================
echo   小红书千帆客服 Agent 一键启动
echo ============================================
echo.

cd /d "%~dp0"

REM 1. 关闭旧的千帆客户端（避免 CDP 端口冲突）
echo [1/4] 关闭旧的千帆客户端...
taskkill /IM "千帆客服工作台.exe" /F >nul 2>&1
timeout /t 2 /nobreak >nul

REM 2. 带调试端口启动千帆客户端
echo [2/4] 带调试端口启动千帆客户端...
start "" "C:\Users\Terrt\AppData\Local\Programs\eva\千帆客服工作台.exe" --remote-debugging-port=9222 --remote-allow-origins=http://127.0.0.1:9222
timeout /t 6 /nobreak >nul

REM 3. 启动决策 API（新窗口）
echo [3/4] 启动决策 API...
start "千帆客服API" cmd /k "cd /d %~dp0 && set PYTHONPATH=src&& python -u run.py web"

timeout /t 4 /nobreak >nul

REM 4. 启动 Worker（新窗口）
echo [4/4] 启动 Worker...
start "千帆客服Worker" cmd /k "cd /d %~dp0 && set PYTHONPATH=src;workers&& set XHS_DECISION_URL=http://127.0.0.1:18081&& python -u run.py desktop"

echo.
echo ============================================
echo   启动完成！
echo   - 审批台: http://127.0.0.1:18081
echo   - 千帆客户端: 已带调试端口启动（请确认已登录）
echo   - API 和 Worker 分别在两个新窗口运行
echo ============================================
echo.
echo 提示：若千帆客户端未登录，请先在它里面扫码/登录。
pause
