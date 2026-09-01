# 抖店飞鸽客服接入

项目已把抖店飞鸽接到与千帆相同的栀夏 DeepSeek Agent、审批台、人工接管和待发送队列中。两个平台按 `channel + store_id + customer_id` 隔离，审批回复不会串到另一个平台或另一位顾客。

## 推荐方式：已登录 Chrome 扩展桥接

如果飞鸽已经登录在日常使用的 Chrome 中，推荐加载项目自带的本地扩展 `integrations/chrome-douyin-bridge/`。它与千帆 Worker 使用相同的 Agent、店内数据、审批台和风险规则，不需要重新登录专用 Chrome。

处理流程：

```text
飞鸽顾客消息
  → Chrome 飞鸽桥接（方向识别、历史基线、去重、会话核对）
  → POST /platforms/douyin/decide
  → 栀夏 DeepSeek Agent + 店内工具数据 + agent.md 规则
  ├─ 普通咨询：回填原飞鸽会话
  └─ 退款/赔偿/投诉等：审批台或人工接管
       → outbox 只允许原渠道、原顾客的 Worker 领取
```

### macOS 与 Windows 安装扩展

1. 先启动栀夏工作台：macOS 双击 `deploy/macos/start.command`；Windows 运行 `deploy\windows\start.bat`。
2. 在用于登录抖店的 Chrome 打开 `chrome://extensions/`。
3. 打开右上角“开发者模式”，点击“加载已解压的扩展程序”。
4. 选择项目中的 `integrations/chrome-douyin-bridge` 文件夹。
5. 返回飞鸽页面并刷新一次；扩展角标显示 `ON`，页面右上角会提示“栀夏 Agent 已连接”。
6. 首次连接当前会话只记录历史基线，不回复旧消息；后续普通咨询会像千帆一样自动回复。

点击 Chrome 工具栏中的“栀夏客服 Agent”可随时暂停或恢复自动回复。退款、赔偿、改址、投诉和平台介入仍进入 http://127.0.0.1:18081 的审批或人工接管流程。

扩展只访问 `im.jinritemai.com` 和本机 `127.0.0.1:18081`，不读取 Cookie、密码或其他网站。

## 备用方式：专用 Chrome 调试端口

无法安装本地扩展时，可继续使用项目原有的专用 Chrome Worker。这个方式的登录状态单独保存在 `data/douyin-profile/`，与日常 Chrome 不共享。

### macOS

1. 先双击 `deploy/macos/start.command`。
2. 再双击 `deploy/macos/start-douyin.command`。
3. 在新打开的专用 Chrome 登录抖店并进入飞鸽客服。

### Windows 10 / 11

1. 先运行 `deploy\windows\start.bat`。
2. 再运行 `deploy\windows\start-douyin.bat`。
3. 在专用 Chrome 登录抖店并进入飞鸽客服。

停止抖店 Worker 使用 `deploy\windows\stop-douyin.bat`；主停止脚本也会一并停止它。

### 手动启动

macOS：

```bash
open -na "/Applications/Google Chrome.app" --args \
  --remote-debugging-port=19223 \
  --remote-allow-origins='*' \
  --user-data-dir="$PWD/data/douyin-profile" \
  'https://fxg.jinritemai.com/'

DOUYIN_CDP_URL=http://127.0.0.1:19223 python run.py douyin
```

Windows PowerShell：

```powershell
& "$env:ProgramFiles\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=19223 `
  --remote-allow-origins=* `
  --user-data-dir="$PWD\data\douyin-profile" `
  https://fxg.jinritemai.com/

$env:DOUYIN_CDP_URL="http://127.0.0.1:19223"
python run.py douyin
```

## 安全边界

- 扩展和 Worker 只处理页面明确标记为顾客方向的新消息；页面结构不明确时停止发送。
- 切换到一个没有未读标记的新会话时，先记录最后一条历史消息作为基线，不回复历史内容。
- 扩展默认只处理当前打开的会话，不主动切换会话；备用 Worker 可自动打开未读会话，设置 `DOUYIN_AUTO_OPEN_UNREAD=0` 可关闭。
- 人工审批消息同时匹配 `douyin_feige` 和当前顾客，匹配失败就不领取、不确认。
- 退款、赔偿、改址、取消、拦截、投诉和平台介入仍由 Agent 规则进入待审或人工接管，不自动执行后台写操作。
- 登录 Cookie、DeepSeek 密钥、数据库和日志不会提交到 Git。
- 外部平台会话默认不重复自我介绍，直接承接顾客当前问题；店铺展示名不同也不会主动报出错误品牌名。

## 飞鸽页面更新时校准

飞鸽网页类名可能变化。先登录并打开一个会话，然后运行：

```bash
DOUYIN_CDP_URL=http://127.0.0.1:19223 python run.py douyin-dump
```

它只输出页面地址、标题、有限的节点类名/方向和短文本样本到 `data/douyin-feige-structure.json`，不会导出 Cookie、Local Storage 或完整聊天记录。可按输出覆盖：

- `DOUYIN_PAGE_URL_KEYWORDS`
- `DOUYIN_PAGE_TITLE_KEYWORDS`
- `DOUYIN_MESSAGE_ROW_SELECTORS`
- `DOUYIN_INPUT_SELECTORS`
- `DOUYIN_CONTACT_SELECTORS`
- `DOUYIN_SESSION_ITEM_SELECTORS`
- `DOUYIN_UNREAD_SELECTORS`

## 官方开放平台接入

正式服务端接入需要在抖店开放平台创建应用，并获得与“客服机器人 / 商家 AI 智能客服”相符的场景和接口权限。公开文档说明消息推送需要配置事件回调，接口可见性也受应用授权场景控制；因此项目没有虚构飞鸽官方回调字段或签名算法。

获得权限后建议新增一个官方网关：

1. 按抖店当前官方文档验签、校验时间戳并做事件去重。
2. 把顾客消息转换成下方稳定内部请求。
3. 调用 Agent 后，通过获批的官方客服发送接口回传；人工审批仍复用现有 outbox。

内部入口：

```http
POST /platforms/douyin/decide
Content-Type: application/json

{
  "text": "这件衬衫什么时候发货？",
  "customer_id": "平台侧稳定顾客标识",
  "message_id": "平台消息唯一标识",
  "tenant_id": "demo",
  "store_id": "STORE-001"
}
```

这个入口不是抖店原始回调地址，不能跳过官方验签直接暴露到公网。

官方资料：

- [抖店开放平台](https://op.jinritemai.com/)
- [飞鸽客服系统使用指南](https://school.jinritemai.com/doudian/wap/article/103160?from=dibujiedu_02&from_school=1&should_full_screen=1&should_hide_bottom_nav=1)
- [消息推送接入指南](https://op.jinritemai.com/docs/tmp/153/99)
- [应用授权场景说明](https://op.jinritemai.com/docs/guide-docs/1367/4405)
