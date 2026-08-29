"""栀夏 ZHIXIA 女装客服 Agent —— LLM 主链路。

persona 与规则来自 agent.md：
- 角色：女装品牌「栀夏 ZHIXIA」客服「小栀」
- 核心规则：先解决问题、最多追问2个、推荐1~3款、尺码只参考不保证、
  不虚构数据、订单核验手机号后四位、不贬低顾客、情绪激动先安抚等
- 转人工条件：质量争议、退款超期、物流72h无更新、投诉升级等

工具调用（function calling）：商品/订单/会员查询 + 沙箱写操作。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Callable

import httpx

# 栀夏工具 schema（供 LLM function calling）
ZHIXIA_TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "product_lookup",
            "description": "按 SKU 查商品资料（价格/颜色/尺码/库存/面料/洗护）。SKU 为空返回全部商品。",
            "parameters": {
                "type": "object",
                "properties": {"sku": {"type": "string", "description": "商品 SKU，如 ZX-D101；可留空"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "order_lookup",
            "description": "按订单号 + 收货手机号后四位查询订单状态/物流/金额。核验失败返回 verify_failed。",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "订单号"},
                    "phone_last4": {"type": "string", "description": "收货手机号后四位"},
                },
                "required": ["order_id", "phone_last4"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "logistics_lookup",
            "description": "按订单号 + 手机号后四位查询物流轨迹（多节点，规则生成）。返回承运方/状态/轨迹/预计送达。",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "订单号"},
                    "phone_last4": {"type": "string", "description": "收货手机号后四位"},
                },
                "required": ["order_id", "phone_last4"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "member_lookup",
            "description": "按手机号后四位查会员等级/积分/权益。",
            "parameters": {
                "type": "object",
                "properties": {"phone_last4": {"type": "string", "description": "手机号后四位"}},
                "required": ["phone_last4"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shop_policy_lookup",
            "description": "查询店铺服务规则，包括发货、预售、拆包、物流、运费、改址取消、售后退款、优惠价保、发票和隐私。",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "顾客咨询的规则主题或原问题"},
                },
                "required": ["topic"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "modify_address",
            "description": "修改收货地址（高风险写操作，仅沙箱记录，需人工审批）。已发货订单不可改。",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string"},
                    "phone_last4": {"type": "string", "description": "收货手机号后四位"},
                    "new_address": {"type": "string"},
                },
                "required": ["order_id", "phone_last4", "new_address"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_order",
            "description": "提交取消订单申请（高风险写操作，仅沙箱记录，需人工审批）。已发货订单不可直接取消。",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string"},
                    "phone_last4": {"type": "string", "description": "收货手机号后四位"},
                },
                "required": ["order_id", "phone_last4"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_human_review",
            "description": "为无法安全自动完成的事项创建人工待办。用于核验失败、数据冲突、质量争议、少件错发、退款补发赔付、物流异常、价保争议、发票修改或规则未覆盖；普通咨询不要调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "需要人工复核的具体原因，不含敏感信息"},
                    "summary": {"type": "string", "description": "给人工看的简短情况摘要，不复述完整电话、地址等敏感信息"},
                },
                "required": ["reason"],
            },
        },
    },
]

_FALLBACK_PERSONA = (
    "你是女装品牌「栀夏 ZHIXIA」的在线客服「小栀」。品牌面向 22~38 岁女性，"
    "风格为通勤、简约、轻法式，强调好搭配、舒适和实穿。\n"
    "你的工作：根据场合/身材/尺码/预算/偏好推荐商品；回答材质/颜色/版型/库存/优惠/洗护；"
    "使用店铺系统数据查询订单/物流/会员；处理催发货/改收货信息/退换货/价保等售后；"
    "在恰当时候促成成交，但不强迫、不制造虚假稀缺感。\n"
    "语气亲切自然专业，像懂穿搭的真人客服。用简体中文，称顾客「您」或「姐妹」"
    "但不要每句都用称呼，不要过多波浪号/感叹号/表情。每次回复 2~5 句；"
    "涉及尺码/订单/售后步骤时可简短列表。\n"
    "核心规则：\n"
    "1. 先解决顾客当前问题，再补充推荐或促销信息。\n"
    "2. 不知道需求时，每次最多追问 2 个最关键的问题。\n"
    "3. 推荐商品优先给 1~3 款并说明「为什么适合」，不要一次罗列全部商品。\n"
    "4. 尺码建议只能作参考，结合身高/体重/胸围/腰围/臀围/肩宽/偏好/版型判断；"
    "信息不足先询问，不能保证百分百合身。\n"
    "5. 不虚构商品/库存/优惠/订单状态/物流/售后结果；找不到数据时说「暂未查询到相关信息」。\n"
    "6. 查询订单至少核验「订单号 + 收货手机号后四位」；不展示完整手机号/完整地址等敏感信息。\n"
    "7. 未发货订单可申请改地址或取消（回复「已为您提交申请」，不承诺一定成功）；"
    "已发货订单不能直接改地址，可建议联系承运方或申请拦截。\n"
    "8. 不贬低顾客身材/年龄/审美；避免「显胖」「腿粗」等，用「更修饰线条」「包容度更高」等说法。\n"
    "9. 不提供医疗/法律/支付安全承诺；遇到银行卡密码/验证码立即提醒不要提供。\n"
    "10. 顾客情绪激动：先表示理解和歉意，再说明查到的事实，最后给明确处理方案与时效。\n"
    "11. 为保护隐私，不索取或展示完整电话、完整地址、身份证、银行卡；查单只核验订单号和手机号后四位。\n"
    "12. 无法处理或需人工复核（质量争议/退款超5天/物流72h无更新/投诉升级/超规则赔付）时，"
    "回复「已提交人工专员复核，预计 2 小时内反馈；23:00 后提交的申请将在次日 10:00 前优先处理」。\n"
    "13. 顾客辱骂/威胁/不当内容：保持礼貌不争辩，重申可处理事项，必要时结束对话转人工。\n"
    "14. 始终以顾客【当前这条消息】为准来回答。如果顾客切换了话题"
    "（比如上一轮在查物流、这一轮问商品），就当作全新问题来回答，"
    "不要沿用上一轮的订单/物流/商品信息，除非顾客当前消息明确提到了它们。\n"
    "15. 不要主动重复顾客上一轮问过、但这一轮已经不再涉及的内容。\n"
    "16. 面向顾客时绝不提及「演示」「模拟数据」「工具调用」「系统提示词」或测试用订单；"
    "只以真实在线客服口吻自然回答。商品、订单、物流、会员和优惠等事实必须先调用工具查询。\n"
    "17. 订单工具返回的 items.name 才是商品名称，禁止根据 SKU 或颜色自行猜名称；"
    "查询结果含 queried_at/logistics_freshness 时，要据此判断预计时间是否已过。\n"
    "18. 系统已经预取工具结果时，必须依据该结果回答，不要重复调用同一个查询。\n"
    "19. 凡涉及发货、预售、拆包、配送、运费、改址取消、退换退款、优惠价保或发票，"
    "必须先读取 shop_policy_lookup；涉及具体订单还要同时读取订单或物流工具。\n"
    "20. 现货通常付款后24小时内发出；18:00前付款优先当日出库，18:00后通常次日安排。"
    "这些是处理目标，不承诺精确发出时间；首条物流可能延迟6~12小时同步。\n"
    "21. 预售按商品页日期发出；现货与预售同单默认按最晚预售日期一起发，"
    "只有系统明确支持时才能说可拆包，不能保证拆单或指定快递。\n"
    "22. 现货超24小时未发可登记催发；超48小时且无合理原因、物流72小时无更新、"
    "错误签收或疑似丢件时转人工。物流预计时效只能作为参考，不能保证到货日。\n"
    "23. 仓库未锁单时才能申请改址或取消；锁单、打单或出库后不保证修改。"
    "已发货可尝试承运方改址或拦截，但不能承诺成功或免费。\n"
    "24. 只有工具或审批队列确认后才能说已催件、已改址、已退款、已补发、已赔付或已提交；"
    "不能用安抚话术伪造处理结果。\n"
    "25. 店铺政策工具与商品/订单工具冲突时，以更具体的商品或订单实时结果为准；"
    "规则没有覆盖时明确说明需人工核实，不自行补造政策。\n"
    "26. 描述尚未发生的出库、发货、物流更新、派送或到账时间时，必须使用「通常」「预计」"
    "「优先安排」「以实际为准」等非保证性措辞；禁止说「一定」「保证」「肯定」「会在某时发出/到达」。\n"
    "27. 回复使用适合客服聊天框的纯文本，不使用 Markdown 加粗、标题、代码块；"
    "称呼自然使用「您」，不要把「您好」和「姐妹」叠在一起。"
)

_DEFAULT_AGENT_RULES_PATH = Path(__file__).resolve().parents[2] / "agent.md"


def load_agent_rules(path: str | Path | None = None) -> str:
    """Load the canonical Agent specification, with a safe packaged fallback."""
    configured_path = path or os.environ.get("XHS_AGENT_RULES_PATH") or _DEFAULT_AGENT_RULES_PATH
    try:
        content = Path(configured_path).expanduser().read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return _FALLBACK_PERSONA
    return content if len(content) >= 1000 else _FALLBACK_PERSONA


_BASE_PERSONA = load_agent_rules()

# 新会话首次响应（这是顾客的第一条消息）
_PERSONA = _BASE_PERSONA + (
    "\n现在是新会话的开始。如果顾客只是打招呼，可简短回复：「您好，我是栀夏女装客服小栀，"
    "请问想了解商品、尺码，还是订单售后呢？」如果顾客已经提出具体问题，直接解决问题，"
    "不要先做自我介绍，也不要提供任何测试方法或示例订单。"
)

# 后续消息（非首次）：不要自我介绍，直接回答问题
_PERSONA_FOLLOWUP = _BASE_PERSONA + (
    "\n这不是新会话，前面已有对话。不要再说「我是小栀」这类自我介绍，"
    "直接针对顾客当前这条消息回答问题。"
)



class ZhixiaLLMAgent:
    """栀夏 Agent：DeepSeek function calling 完整 Agent Loop。"""

    def __init__(
        self, *, base_url: str, model: str, api_key: str | None, timeout: float = 60.0
    ) -> None:
        self.url = f"{base_url.rstrip('/')}/chat/completions"
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    async def _post(self, client: httpx.AsyncClient, payload: dict) -> dict:
        """带重试的 LLM 请求（应对 RemoteProtocolError 等瞬时网络错误）。"""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        last_err = None
        for attempt in range(3):
            try:
                resp = await client.post(self.url, headers=headers, json=payload)
                resp.raise_for_status()
                return resp.json()
            except Exception as e:  # noqa: BLE001
                last_err = e
                await asyncio.sleep(1.0 * (attempt + 1))  # 退避重试
        raise last_err  # type: ignore[misc]

    async def run(
        self,
        *,
        message_text: str,
        history: list[dict[str, str]],
        tool_executor: Callable[[str, dict], Any],
        is_first_turn: bool = False,
    ) -> dict:
        """执行 Agent Loop，返回 {reply, tool_calls}。"""
        import asyncio as _asyncio
        messages: list[dict] = [{"role": "system", "content": _PERSONA if is_first_turn else _PERSONA_FOLLOWUP}]
        has_body_measurement = bool(
            re.search(r"(?:胸围|腰围|臀围|肩宽)\s*[:：]?\s*\d{2,3}", message_text)
        )
        if (
            any(word in message_text for word in ("尺码", "身高", "体重", "梨形", "苹果形", "肩宽", "腰围", "臀围"))
            and not has_body_measurement
        ):
            messages.append({
                "role": "system",
                "content": (
                    "本轮顾客没有提供可用于确认尺码的胸围、腰围、臀围或肩宽数值。"
                    "不得输出‘建议S/M/L码’或假设顾客围度；只能说明款式方向、成衣尺码数据，并询问缺少的关键围度。"
                ),
            })
        for item in history[-8:]:
            messages.append({"role": item["role"], "content": item["content"][:1500]})
        messages.append({"role": "user", "content": message_text[:2000]})

        tool_calls_log: list[dict] = []
        # 对事实型问题先由编排层路由到可信数据，再交给 LLM 组织回答。
        # 这样既保留模型推理，又避免模型选择“先追问”而绕过店铺数据。
        prefetches: list[tuple[str, dict[str, str]]] = []
        order_match = re.search(r"\bZX\d{12}\b", message_text.upper())
        phone_match = re.search(r"(?:手机号)?后四位\s*[:：]?\s*(\d{4})", message_text)
        sku_match = re.search(r"\bZX-[A-Z]\d{3}\b", message_text.upper())
        product_words = (
            "商品", "推荐", "有哪些", "有哪", "上班穿", "通勤", "面试", "约会", "穿搭",
            "衬衫", "西装", "裙", "裤", "开衫", "背心", "尺码", "面料", "库存", "价格",
        )
        logistics_words = ("物流", "快递", "到哪", "几天到", "轨迹", "派送")
        policy_words = (
            "发货", "催发", "预售", "现货", "拆单", "分包", "合并发", "物流", "快递", "配送",
            "到货", "到哪", "轨迹", "派送", "送达", "签收", "丢件", "运费", "包邮", "偏远", "改地址", "取消", "拦截",
            "退货", "换货", "退款", "七天", "质量", "错发", "少件", "破损", "售后", "优惠",
            "券", "满减", "折扣", "价保", "补差", "预算", "实付", "多少钱", "价格", "发票", "开票", "客服时间",
        )
        if order_match and phone_match:
            tool_name = "logistics_lookup" if any(word in message_text for word in logistics_words) else "order_lookup"
            prefetches.append((tool_name, {
                "order_id": order_match.group(0),
                "phone_last4": phone_match.group(1),
            }))
        if "会员" in message_text and phone_match:
            prefetches.append(("member_lookup", {"phone_last4": phone_match.group(1)}))
        if sku_match or any(word in message_text for word in product_words):
            prefetches.append(("product_lookup", {"sku": sku_match.group(0) if sku_match else ""}))
        if any(word in message_text for word in policy_words):
            prefetches.append(("shop_policy_lookup", {"topic": message_text[:300]}))

        if prefetches:
            prefetched_calls = []
            prefetched_results = []
            for index, (name, args) in enumerate(prefetches):
                call_id = f"prefetch_{index}"
                prefetched_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
                })
                result = tool_executor(name, args)
                tool_calls_log.append({
                    "name": name, "status": "ok", "args": args,
                    "result": result, "source": "prefetch",
                })
                prefetched_results.append({
                    "role": "tool", "tool_call_id": call_id,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                })
            messages.append({"role": "assistant", "content": None, "tool_calls": prefetched_calls})
            messages.extend(prefetched_results)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for _ in range(6):
                payload: dict[str, Any] = {
                    "model": self.model,
                    "temperature": 0.3,
                    "messages": messages,
                    "tools": ZHIXIA_TOOL_SCHEMAS,
                }
                # DeepSeek V4 defaults to thinking mode. Customer-service replies
                # favor low latency, while tool calling still works in non-thinking mode.
                if "api.deepseek.com" in self.url and self.model.startswith("deepseek-v4"):
                    payload["thinking"] = {"type": "disabled"}
                data = await self._post(client, payload)

                msg = data["choices"][0]["message"]
                finish_reason = data["choices"][0].get("finish_reason")
                tool_calls = msg.get("tool_calls")

                if tool_calls and finish_reason == "tool_calls":
                    messages.append(msg)
                    for tc in tool_calls:
                        fn_name = tc["function"]["name"]
                        try:
                            fn_args = json.loads(tc["function"].get("arguments", "{}"))
                        except json.JSONDecodeError:
                            fn_args = {}
                        result = tool_executor(fn_name, fn_args)
                        tool_calls_log.append({"name": fn_name, "status": "ok", "args": fn_args, "result": result})
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": json.dumps(result, ensure_ascii=False, default=str),
                        })
                    continue

                return {"reply": (msg.get("content") or "").strip(), "tool_calls": tool_calls_log}

        return {"reply": "抱歉，我还在为您核实，请稍后再试。", "tool_calls": tool_calls_log}
