@echo off
chcp 65001 >nul
title 小红书千帆客服 Agent - 一键启动
echo ============================================
echo   小红书千帆客服 Agent 一键启动
echo ============================================
echo.

cd /d "%~dp0"

echo [1/3] 检查千帆客户端与依赖...
python run.py doctor
if errorlevel 1 (
  echo.
  echo 环境检查未通过，请根据上方提示修复后重试。
  pause
  exit /b 1
)

echo [2/3] 启动决策 API...
start "千帆客服API" cmd /k "cd /d %~dp0 && python -u run.py web"

timeout /t 3 /nobreak >nul

echo [3/3] 启动千帆客户端与 Worker...
start "千帆客服Worker" cmd /k "cd /d %~dp0 && python -u run.py qianfan"

echo.
echo ============================================
echo   启动完成！
echo   - 审批台: http://127.0.0.1:18081
echo   - 千帆客户端会自动发现并以调试模式启动
echo   - 不再强制关闭现有千帆或其他 Python 程序
echo ============================================
pause
