# Windows 部署

## 适用环境

- Windows 10 或 Windows 11。
- Python 3.11 或更高版本，安装时勾选 `Add Python to PATH`。
- Windows PowerShell 5.1 或 PowerShell 7。
- 已安装并登录“千帆客服工作台”。
- 可用的 DeepSeek API Key。

## 首次安装

1. 克隆或下载仓库，解压到不需要管理员权限的目录。
2. 双击 `deploy\windows\install.bat`。
3. 按提示输入 DeepSeek API Key。输入内容不可见，密钥通过 Windows DPAPI 加密后保存在 `data\deepseek-key.dpapi`，只能由当前 Windows 用户解密。
4. 安装完成后双击 `deploy\windows\start.bat` 或 `deploy\windows\启动器.vbs`。

启动脚本会自动在常见目录中查找千帆，不包含固定用户名。默认优先检查：

```text
%LOCALAPPDATA%\Programs\eva\千帆客服工作台.exe
```

如果千帆安装在其他位置，脚本还会在 `%LOCALAPPDATA%\Programs` 中递归查找。

## 日常使用

- 启动：双击 `deploy\windows\start.bat`。
- 无路径启动入口：双击 `deploy\windows\启动器.vbs`。
- 停止：双击 `deploy\windows\stop.bat`。
- 更换 DeepSeek Key：双击 `deploy\windows\change-key.bat`。

停止脚本只结束 `data` 目录 PID 文件中记录的本项目 API 和 Worker，不会结束电脑上的其他 Python 程序。是否同时退出千帆由用户选择。

## 手动启动

在 PowerShell 中执行：

```powershell
& "$env:LOCALAPPDATA\Programs\eva\千帆客服工作台.exe" `
  --remote-debugging-port=19222 --remote-allow-origins=*

.\.venv\Scripts\python.exe run.py web
$env:XHS_CDP_URL = "http://127.0.0.1:19222"
$env:XHS_DECISION_URL = "http://127.0.0.1:18081"
.\.venv\Scripts\python.exe run.py desktop
```

手动启动时还需通过环境变量 `XHS_LLM_API_KEY` 提供 DeepSeek Key；推荐优先使用自动脚本，避免明文密钥进入命令历史。

## 日志与排查

- API：`data\windows-api.log`、`data\windows-api-error.log`
- Worker：`data\windows-worker.log`、`data\windows-worker-error.log`
- 健康检查：浏览器打开 `http://127.0.0.1:18081/health`
- 千帆配对检查：浏览器打开 `http://127.0.0.1:19222/json/version`

常见问题：

- 找不到 Python：重新安装 Python 3.11+ 并勾选加入 PATH。
- 找不到千帆：确认已安装桌面版，或把客户端安装到当前用户的 Programs 目录。
- 配对端口失败：保存输入内容，完全退出千帆后重新运行启动脚本。
- Key 无法解密：DPAPI 文件只能由创建它的 Windows 用户使用，切换账号后请重新运行 `change-key.bat`。
