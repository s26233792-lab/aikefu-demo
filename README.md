# 栀夏 ZHIXIA 女装客服 Agent · Demo

一个**女装电商 AI 客服演示**，按 `agent.md` 规格实现：品牌「栀夏 ZHIXIA」客服「小栀」，面向 22~38 岁女性（通勤/简约/轻法式），支持售前导购、订单物流查询、会员查询、售后处理，含完整模拟商品库/订单库/会员库/活动规则/售后规则。

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

1. **栀夏 Agent**（`zhixia_*.py`，主演示）：LLM 完整 Agent Loop + 工具（商品/订单/会员）+ 语气分析 + 转人工，端点 `/zhixia/decide`；
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

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

> 需要千帆真实浏览器 Worker 时再：`pip install playwright && playwright install chromium`

### 2. 配置

```bash
copy .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY；无 Key 可先设 XHS_LLM_MODE=rules 体验规则降级
```

### 3. 启动

```bash
python run.py web      # 启动 API + Web 演示（http://127.0.0.1:18081）
python run.py smoke    # 离线冒烟测试（rules 模式，无需 Key）
```

### 4. 接入真实小红书千帆（真实售后/客服）

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
> - DOM 选择器已按 2026-08-19 登录后真实页面校准，见 `workers/qianfan_browser.py` 顶部 `SELECTORS`；页面升级后需重新校准。

打开 http://127.0.0.1:18081 ，在左侧切换「售前/售中/售后」场景并点击脚本按钮逐条体验；右侧实时展示 Agent 决策链路，底部可审批/执行高风险写操作。

## 目录结构

```
xhs-kefu-demo/
├── run.py                      # 一键启动 (web/worker/smoke)
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
└── workers/qianfan_browser.py  # Playwright 千帆网页版真实收发 Worker
```

## 说明与免责

- 本 Demo 的订单/物流/商品均为**演示夹具数据**，不接真实 ERP；写操作在"沙箱"中记录动作状态，不真正调用快递/千帆写接口。
- 千帆网页版是 SPA，DOM 结构与选择器会随版本变化，`workers/qianfan_browser.py` 顶部的 `SELECTORS` 需按当前页面校准；登录态由本地持久化 profile 保存。
- 接入真实顾客前，请按自己的数据规范配置鉴权、备份、保留期与人工接管流程。

## 许可证

参考项目为 MIT；本 Demo 仅用于技术演示，接入平台需遵守小红书千帆及第三方服务的账号权限与平台规则。
