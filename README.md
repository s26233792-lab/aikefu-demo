# 栀夏 ZHIXIA 女装客服 Agent · Demo

一个**女装电商 AI 客服演示**，按 `agent.md` 规格实现：品牌「栀夏 ZHIXIA」客服「小栀」，面向 22~38 岁女性（通勤/简约/轻法式），支持售前导购、订单物流查询、会员查询、售后处理，含完整模拟商品库/订单库/会员库/活动规则/售后规则。

> 架构忠实参考 [dxl-commerce-agent](https://github.com/whichmen/dxl-commerce-agent)：意图识别 → 工具查事实 → 风控 → 写操作审批 → 回复。支持真实 DeepSeek LLM 推理 + 规则降级。Web 演示界面在 http://127.0.0.1:18081 。

支持 **Windows 与 macOS** 的小红书千帆客服工作台：桌面客户端采用 Electron CDP 接入，网页后台采用 Playwright 作为兼容兜底。macOS 可使用仓库根目录的一键安装、启动脚本。

## 演示场景（售前 / 订单 / 售后）

| 阶段 | 演示问题 | 涉及能力 |
|---|---|---|
| 售前导购 | 梨形身材面试穿搭（预算800）| 商品推荐 + 尺码建议 + 搭配 |
| 售前导购 | 阔腿裤 M 码能穿吗（腰围70臀围95）| 尺码核验（结合身材数据）|
| 售前导购 | 不透的白衬衫推荐 | 商品筛选 + 面料说明 |
| 订单物流 | 订单 ZX202608200147 查物流 | 订单核验（手机号后四位）+ 物流 |
| 订单物流 | 订单 ZX202608210083 改地址 | 写操作（沙箱 + 人工审批）|
| 售后 | 订单退款怎么还没到 | 退款时效 + 转人工条件 |
| 售后 | 裙子穿了一天能退吗 | 七天无理由规则 |

## 架构

本项目包含**三套并存的架构**：

1. **栀夏 Agent**（`zhixia_*.py`，主演示）：LLM 完整 Agent Loop + 工具（商品/订单/会员）+ 语气分析 + 转人工，端点 `/zhixia/decide`；
2. **单 Agent 版**（`xhs_kefu/` 顶层）：LLM 完整 Agent Loop + 工具 + 风控 + 审批，已接真实千帆；
3. **多 Agent MVP 版**（`xhs_kefu/mvp/`）：Router → FAQ/商品/售后 三个子 Agent + RAG + 工具。

```
小红书千帆（Windows/macOS 桌面端或网页版）/ Web 演示界面
        │  统一 IncomingMessage
        ▼
Decision API  POST /v1/decide （去重 + 会话锁 + 短时记忆）
        │
        ▼
Agent Runtime  （LLM/规则 意图识别 → 工具调用 → 风控 → 审批 → 生成回复）
        │
        ├─ 只读工具: order_lookup / logistics_lookup / product_lookup
        ├─ 写操作:   modify_address / intercept_express / compensate（风控+人工审批）
        ▼
SQLite（会话 / 决策 / 动作 / 回执）  ← 决策·发送·回执三态分离
```

## 售后风控规则（移植自 dxl-commerce-agent 的 kefu-core）

`aftersale_policy.py` 忠实移植了参考项目真实运营踩坑总结的售后规则：

| 机制 | 规则 |
|---|---|
| 缺货纸条单/礼物单/没发单 | `gift_order.is_gift` 或 `seller_memo` 含「没发」→ 全额仅退款，不走 3/5 元赔付 |
| 3元→5元赔偿阶梯 | 默认 3 元（≤实付）；实付≥15 元且顾客不接受 3 元才行提至 5 元；不退货留货安抚 |
| 证据要求 | 破损/错发/少发/质量需照片（≥2 张），否则要求补证据，不得承诺金额 |
| 与商家协商一致 | 售后原因统一引导「与商家协商一致」（系统自动审批，快；其他原因人工审核，慢）|
| 禁止异步回访 | 只在本轮响应，不承诺"核实后回复/主动回访" |
| 先核实再回复 | 无订单事实时「暂时不能确认」，不得输出大于 5 元的仅退款金额 |

**关键安全边界**（对齐参考架构）：

- 业务事实只来自工具结果，模型禁止凭记忆编造订单/物流/金额；
- 写操作按金额上限、证据要求、高风险人工审批三重拦截；
- 模型只"决定调用哪个工具"，订单号/SKU/金额由确定性解析器从顾客文本抽取；
- LLM 任何失败安全降级到规则 Planner，不让内部结构泄露给顾客。

## 多 Agent MVP（`xhs_kefu/mvp/`）

参考你给出的多 Agent 架构，实现极简版（端点 `/mvp/decide`）：

```
Platform Adapter（千帆真接 + 抖音/千牛占位）
   → Router Agent → FAQ / 商品 / 售后 三个子 Agent
   → RAG（商品资料/FAQ/售后规则，关键词检索）
   → Tool Calling（商品/订单/物流 API）
   → Guardrails → 自动回复 / 转人工
```

## 回答机制（什么自动答、什么转人工）

统一决策引擎在 `decision.py`，每个顾客消息先归类再处置，共四种结果：

| 处置 | 场景 | 行为 |
|---|---|---|
| `AUTO_REPLY` | 普通咨询（商品/物流/规则） | LLM 查事实生成回复，**自动发送** |
| `REQUIRE_APPROVAL` | 写操作（退款/赔偿/改地址/拦截） | 生成回复草稿，**转人工审批**后再发 |
| `HANDOFF_HUMAN` | 情绪升级/投诉/要求转人工/超出能力 | **转人工接管**，Agent 停手等真人 |
| `REJECT` | 提示词注入/异常内容 | **拒绝**，绝不回复 |

**判定优先级**（从高到低）：注入 → 情绪升级/投诉/转人工 → 写操作 → 明确咨询自动答。

### 安全护栏（`safety.py`）

1. **入站护栏**：顾客消息检测提示词注入（"忽略之前规则""泄露系统提示词"等）→ 直接拒绝处理；
2. **敏感信息过滤**：手机号、微信/个人联系方式、身份证、银行卡自动脱敏，禁止索取或输出；
3. **出站护栏**：回复非空校验、内部结构泄漏检测（trace_id/action_id 等）、危险内容拦截。

### 人工介入方式

| 能力 | 说明 |
|---|---|
| **审批台** | Web 面板（http://127.0.0.1:18081）实时展示待审队列，点「✅ 通过发送 / ❌ 拒绝」 |
| **手写回复** | 从待办中选择明确的目标顾客后输入消息，经待发送队列由 Worker 回填到千帆；目标不匹配时保持排队 |
| **会话接管** | 「🚫 接管」停止自动回复转人工，「✅ 恢复」重新交还 Agent |
| **浏览器通知** | 有新待办时弹桌面通知（需授权 Notification） |
| **闭环发送** | 审批通过 → 待发送队列（outbox）→ 核对当前千帆顾客 → 回填 → 成功回执 |
| **防重复/错发** | 审批只允许处理一次；接口或输入框失败会重试；会话切换时取消发送并保留待办 |

### 审批队列 API

| 接口 | 用途 |
|---|---|
| `GET /v1/moderation?status=pending` | 列出待审项 |
| `POST /v1/moderation/{id}/approve` | 审批通过（自动入发送队列） |
| `POST /v1/moderation/{id}/reject` | 拒绝 |
| `POST /v1/handoff` | 接管/恢复会话（`action=take_over/release`） |
| `POST /v1/outbox` | 手写回复入发送队列 |
| `GET /v1/outbox/pull` | Worker 拉取待发送内容 |
| `POST /v1/outbox/{id}/ack` | 确认已发送 |

## 快速开始

### 0. 先体验栀夏女装客服（无需千帆）

项目主演示 Agent 是「栀夏 ZHIXIA」女装客服（规则见桌面 `agent.md`）：

```bash
python run.py web      # 启动 API
# 浏览器打开 http://127.0.0.1:18081 ，左侧切「售前导购/订单物流/售后」点按钮体验
```

或直接调接口：

```bash
curl -X POST http://127.0.0.1:18081/zhixia/decide \
  -H "Content-Type: application/json" \
  -d '{"text":"我158cm52kg梨形身材，想买面试穿的，预算800","session_key":"demo"}'
```

### 1. 安装依赖

Windows：

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

macOS 推荐直接双击 `install-macos.command`，或在终端执行：

```bash
chmod +x install-macos.command start-macos.command
./install-macos.command
```

该脚本会创建 `.venv`、安装桌面 CDP 与网页 Playwright 依赖，并在缺少时创建 `.env`。官方千帆客服工作台可从[小红书下载中心](https://walle.xiaohongshu.com/client-update/)安装 Mac 版本。

### 2. 配置

```bash
# Windows
copy .env.example .env

# macOS / Linux
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY；无 Key 可先设 XHS_LLM_MODE=rules 体验规则降级
```

### 3. 启动

```bash
python run.py web      # 启动 API + Web 演示（http://127.0.0.1:18081）
python run.py smoke    # 离线冒烟测试（rules 模式，无需 Key）
python run.py doctor   # 检查当前系统的千帆客户端、CDP、依赖和 API
```

### 4. 接入真实小红书千帆（真实售后/客服）

#### macOS 桌面客户端（推荐）

1. 从[千帆官方下载中心](https://walle.xiaohongshu.com/client-update/)安装 Mac 版，并至少登录一次；
2. 双击 `install-macos.command` 完成首次安装；
3. 双击 `start-macos.command`，脚本会启动决策 API、打开审批台、以 CDP 模式启动千帆并连接 Worker；
4. 按 `Control+C` 会安全停止本次 API 与 Worker，不会强制退出千帆客户端。

也可以手动运行：

```bash
source .venv/bin/activate
python run.py doctor
python run.py web       # 终端 1
python run.py qianfan   # 终端 2，自动发现 /Applications/千帆客服工作台.app
```

若客户端安装在其他位置，在 `.env` 中设置：

```bash
XHS_QIANFAN_APP_PATH=/Applications/千帆客服工作台.app
XHS_QIANFAN_CDP_PORT=9222
XHS_QIANFAN_STORE_NAME=你的真实店铺名
```

人工介入时，Mac 会尝试激活千帆窗口并发送系统通知。首次触发时，请在“系统设置 → 隐私与安全性 → 自动化/通知”中允许终端或 Python。

#### 网页后台兼容模式

```bash
# 第一步：扫码登录（打开千帆浏览器窗口，用小红书 App 扫码，登录态持久化，仅需一次）
python run.py login

# 第二步：另开终端启动决策 API（若未启动）
python run.py web

# 第三步：启动 Worker（复用登录态，自动轮询新买家消息 → 决策 → 回填回复）
python run.py worker
```

> - 千帆是 SPA，登录后进入 `ark.xiaohongshu.com/app-system/home` 即成功；
> - Worker 为 **只读 + 自动回复**：顾客咨询自动决策回复；退款/改址/拦截等写操作**不在真实后台自动点击**，会上报到审批队列，人工确认后再处理；
> - Worker 无法识别当前顾客或检测到会话切换时会暂停发送，不会把回复投递到另一个会话；
> - DOM 选择器已按 2026-08-19 登录后真实页面校准，见 `workers/qianfan_browser.py` 顶部 `SELECTORS`；页面升级后需重新校准。

#### macOS 常见问题

| 现象 | 处理方式 |
|---|---|
| `doctor` 提示未找到客户端 | 安装官方 Mac 版，或设置 `XHS_QIANFAN_APP_PATH` |
| 客户端已打开但 CDP 未就绪 | 完全退出千帆后重新运行 `python run.py qianfan`；脚本会用调试参数启动新实例 |
| 系统通知或窗口激活无效 | 在 macOS 系统设置中允许终端/Python 的通知与自动化权限 |
| 桌面客户端升级后 DOM 不匹配 | 暂时改用 `python run.py login` + `python run.py worker` 网页模式，并重新校准选择器 |
| Apple Silicon 安装依赖失败 | 确认使用原生 arm64 Python 3.11+，删除 `.venv` 后重跑安装脚本 |

打开 http://127.0.0.1:18081 ，在左侧切换「售前/售中/售后」场景并点击脚本按钮逐条体验；右侧实时展示 Agent 决策链路，底部可审批/执行高风险写操作。

## 目录结构

```
xhs-kefu-demo/
├── run.py                      # 一键启动 (web/worker/smoke)
├── install-macos.command       # macOS 首次安装
├── start-macos.command         # macOS 启动 API + 千帆 + Worker
├── pyproject.toml / .env.example
├── config/policy.toml          # 风控策略（补偿金额上限/证据/审批）
├── src/xhs_kefu/
│   ├── domain.py               # IncomingMessage/Intent/DecisionPlan/风控枚举
│   ├── fixtures.py + data/     # 演示订单/商品/物流
│   ├── tools.py                # 6 个工具（3 只读 + 3 写）
│   ├── tool_schemas.py         # LLM function calling JSON Schema
│   ├── policy.py               # 风控引擎
│   ├── planner.py              # LLM(DeepSeek fc) + 规则降级
│   ├── runtime.py              # 会话锁/去重/记忆/回执/动作执行
│   ├── storage.py              # SQLite
│   ├── api.py                  # FastAPI 决策/工具/审批/历史接口
│   └── web/                    # 千帆模拟器 + Agent 链路可视化面板
└── workers/
    ├── qianfan_launcher.py     # Windows/macOS 客户端发现、启动、诊断
    ├── qianfan_cdp_worker.py   # 千帆桌面端 CDP Worker
    ├── qianfan_browser.py      # Playwright 千帆网页版 Worker
    └── notifier.py             # Windows/macOS 人工介入提醒
```

## 说明与免责

- 本 Demo 的订单/物流/商品均为**演示夹具数据**，不接真实 ERP；写操作在"沙箱"中记录动作状态，不真正调用快递/千帆写接口。
- 千帆桌面端与网页版均可能随官方升级改变 DOM；桌面端使用 `workers/qianfan_cdp_worker.py` 的选择器，网页版使用 `workers/qianfan_browser.py` 的选择器。登录态和 Cookie 只保存在本机用户数据目录，不提交到 Git。
- 接入真实顾客前，请设置非空 `XHS_API_KEY`，并按自己的数据规范配置备份、保留期与人工接管流程。

## 许可证

参考项目为 MIT；本 Demo 仅用于技术演示，接入平台需遵守小红书千帆及第三方服务的账号权限与平台规则。
