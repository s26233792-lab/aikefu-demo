# 栀夏 ZHIXIA 女装客服 Agent · Demo

一个**女装电商 AI 客服演示**，按 `agent.md` 规格实现：品牌「栀夏 ZHIXIA」客服「小栀」，面向 22~38 岁女性（通勤/简约/轻法式），支持售前导购、订单物流查询、发货履约、会员查询、售后处理，并包含商品、订单、会员和店铺政策数据。

> 架构忠实参考 [dxl-commerce-agent](https://github.com/whichmen/dxl-commerce-agent)：意图识别 → 工具查事实 → 风控 → 写操作审批 → 回复。支持真实 DeepSeek LLM 推理 + 规则降级。Web 演示界面在 http://127.0.0.1:18081 。

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

1. **栀夏 Agent**（`zhixia_*.py`，主演示）：LLM 完整 Agent Loop + 工具（商品/订单/物流/会员/店铺政策）+ 语气分析 + 转人工，端点 `/zhixia/decide`；
2. **单 Agent 版**（`xhs_kefu/` 顶层）：LLM 完整 Agent Loop + 工具 + 风控 + 审批，已接真实千帆；
3. **多 Agent MVP 版**（`xhs_kefu/mvp/`）：Router → FAQ/商品/售后 三个子 Agent + RAG + 工具。

```
小红书千帆(网页版) / Web演示界面
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
| **手写回复** | 审批台内直接输入消息，经待发送队列由 Worker 回填到千帆 |
| **会话接管** | 「🚫 接管」停止自动回复转人工，「✅ 恢复」重新交还 Agent |
| **浏览器通知** | 有新待办时弹桌面通知（需授权 Notification） |
| **闭环发送** | 审批通过 → 待发送队列（outbox）→ Worker 轮询回填千帆 → 回执 |

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

## 部署方式（macOS / Windows）

先安装 Python 3.11 或更高版本，并安装、登录「千帆客服工作台」。项目统一使用：

- 管理后台：http://127.0.0.1:18081
- 千帆专用调试端口：`19222`
- LLM：DeepSeek（密钥不会写进仓库）

| 项目 | macOS | Windows |
|---|---|---|
| 首次安装 | `deploy/macos/install.command` | `deploy\windows\install.bat` |
| 日常启动 | `deploy/macos/start.command` | `deploy\windows\start.bat` |
| 停止服务 | `deploy/macos/stop.command` | `deploy\windows\stop.bat` |
| 更换密钥 | `deploy/macos/change-key.command` | `deploy\windows\change-key.bat` |
| 密钥保存 | macOS 钥匙串 | Windows DPAPI（当前用户加密） |
| 详细说明 | [macOS 部署文档](docs/deployment-macos.md) | [Windows 部署文档](docs/deployment-windows.md) |

### macOS

首次安装双击：

```text
deploy/macos/install.command
```

以后双击 `deploy/macos/start.command` 即可。启动器会读取钥匙串中的 DeepSeek 密钥，检查千帆配对状态，并启动 API 和自动回复 Worker。停止时双击 `deploy/macos/stop.command`。

若 macOS 首次阻止脚本运行，可在 Finder 中右键脚本，选择“打开”。

### Windows 10 / 11

首次安装双击：

```text
deploy\windows\install.bat
```

以后双击 `deploy\windows\start.bat` 即可。启动器会解密当前 Windows 用户保存的 DeepSeek 密钥，自动寻找千帆安装位置，并启动 API 和自动回复 Worker。停止时双击 `deploy\windows\stop.bat`。

仓库根目录保留的 `start.bat`、`stop.bat` 和 `启动器.vbs` 是兼容入口，实际会调用 `deploy\windows` 下的新脚本。

### 手动启动 / 仅体验 Web 演示

不接千帆时，也可以手动安装并体验「栀夏 ZHIXIA」女装客服：

```bash
python -m venv .venv
source .venv/bin/activate      # macOS
# .venv\Scripts\activate      # Windows
pip install -r requirements.txt
pip install -e .
python run.py web
```

浏览器打开 http://127.0.0.1:18081 。演示查单可使用订单号 `ZX202608200147` 和手机号后四位 `7319`。

也可直接调接口：

```bash
curl -X POST http://127.0.0.1:18081/zhixia/decide \
  -H "Content-Type: application/json" \
  -d '{"text":"我158cm52kg梨形身材，想买面试穿的，预算800","session_key":"demo"}'
```

手动接入千帆桌面端时，先让千帆以 `19222` 调试端口启动，再分别运行：

```bash
python run.py web
XHS_CDP_PORT=19222 python run.py desktop   # macOS
# Windows PowerShell: $env:XHS_CDP_PORT=19222; python run.py desktop
```

Worker 会自动回复普通咨询；退款、赔偿、改址、拦截等高风险操作会进入管理后台的待审队列，不会直接操作真实后台。

## 目录结构

```
xhs-kefu-demo/
├── run.py                      # 一键启动 (web/worker/smoke)
├── agent.md                    # 运行时直接加载的完整客服 Agent 规范
├── pyproject.toml / .env.example
├── deploy/
│   ├── macos/                  # macOS 安装/启动/停止/换 Key
│   └── windows/                # Windows 安装/启动/停止/换 Key
├── docs/                       # 分系统部署说明
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
└── workers/qianfan_browser.py  # Playwright 千帆网页版真实收发 Worker
```

## 说明与免责

- 本 Demo 的订单/物流/商品均为**演示夹具数据**，不接真实 ERP；写操作在"沙箱"中记录动作状态，不真正调用快递/千帆写接口。
- 千帆网页版是 SPA，DOM 结构与选择器会随版本变化，`workers/qianfan_browser.py` 顶部的 `SELECTORS` 需按当前页面校准；登录态由本地持久化 profile 保存。
- 接入真实顾客前，请按自己的数据规范配置鉴权、备份、保留期与人工接管流程。

## 许可证

参考项目为 MIT；本 Demo 仅用于技术演示，接入平台需遵守小红书千帆及第三方服务的账号权限与平台规则。
