# macOS 部署

## 适用环境

- macOS 12 或更高版本，Apple Silicon 与 Intel 均可。
- Python 3.11 或更高版本。
- 已安装并登录“千帆客服工作台”。
- 可用的 DeepSeek API Key。

## 首次安装

1. 克隆仓库并进入项目目录。
2. 双击 `deploy/macos/install.command`。
3. 首次运行时输入 DeepSeek API Key。密钥保存到 macOS 系统钥匙串，服务名为 `aikefu-demo.deepseek`，不会写入仓库。
4. 安装完成后双击 `deploy/macos/start.command`。

如果 macOS 阻止首次运行，可在 Finder 中右键脚本选择“打开”，或在终端执行：

```bash
chmod +x deploy/macos/*.command
./deploy/macos/install.command
```

## 日常使用

- 启动：双击 `deploy/macos/start.command`。
- 停止：双击 `deploy/macos/stop.command`。
- 更换 DeepSeek Key：双击 `deploy/macos/change-key.command`。

启动器会完成以下操作：

1. 检查 Python 虚拟环境和 DeepSeek Key。
2. 启动本地决策服务 `http://127.0.0.1:18081`。
3. 必要时征得确认后重启千帆，并使用专用 CDP 端口 `19222`。
4. 启动自动回复 Worker，并打开千帆和本地审批台。

## 手动启动

```bash
open -na "/Applications/千帆客服工作台.app" --args \
  '--remote-debugging-port=19222' '--remote-allow-origins=*'

.venv/bin/python run.py web
XHS_CDP_URL=http://127.0.0.1:19222 \
XHS_DECISION_URL=http://127.0.0.1:18081 \
.venv/bin/python run.py desktop
```

## 日志与排查

- API：`data/macos-api.log`
- Worker：`data/macos-worker.log`
- 健康检查：`curl http://127.0.0.1:18081/health`
- 千帆配对检查：`curl http://127.0.0.1:19222/json/version`

若千帆无法配对，请完全退出千帆后重新运行启动器。不要让其他 Chrome/Electron 程序占用 `19222`。
