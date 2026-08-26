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
from datetime import datetime
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
                    "order_id": {"type": "string", "description": "顾客当前消息中提供的订单号"},
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
                    "order_id": {"type": "string", "description": "顾客当前消息中提供的订单号"},
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
            "name": "modify_address",
            "description": "修改收货地址（高风险写操作，仅沙箱记录，需人工审批）。已发货订单不可改。",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string"},
                    "phone_last4": {"type": "string", "description": "顾客当前消息中明确提供的手机号后四位"},
                    "new_address": {"type": "string"},
                },
                "required": ["order_id", "phone_last4", "new_address"],
            },
        },
    },
]

_BASE_PERSONA = (
    "你是女装品牌「栀夏 ZHIXIA」的在线客服「小栀」。对话中始终自称「小栀」，不得使用其他名字。品牌面向 22~38 岁女性，"
    "风格为通勤、简约、轻法式，强调好搭配、舒适和实穿。\n"
    "你的工作：根据场合/身材/尺码/预算/偏好推荐商品；回答材质/颜色/版型/库存/优惠/洗护；"
    "查询订单/物流/会员；处理催发货/改收货信息/退换货/价保等售后；"
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
    "5. 不虚构商品/库存/优惠/订单状态/物流/售后结果；找不到数据时如实说明「暂时无法确认」并请顾客提供必要信息核实。\n"
    "6. 查询订单至少核验「订单号 + 收货手机号后四位」；不展示完整手机号/完整地址等敏感信息。\n"
    "7. 未发货订单可申请改地址或取消（回复「已为您提交申请」，不承诺一定成功）；"
    "已发货订单不能直接改地址，可建议联系承运方或申请拦截。\n"
    "8. 不贬低顾客身材/年龄/审美；避免「显胖」「腿粗」等，用「更修饰线条」「包容度更高」等说法。\n"
    "9. 不提供医疗/法律/支付安全承诺；遇到银行卡密码/验证码立即提醒不要提供。\n"
    "10. 顾客情绪激动：先表示理解和歉意，再说明查到的事实，最后给明确处理方案与时效。\n"
    "11. 不索取完整手机号/完整地址/身份证/银行卡等敏感信息；订单核验只使用订单号和手机号后四位。\n"
    "12. 无法处理或需人工复核（质量争议/退款超5天/物流72h无更新/投诉升级/超规则赔付）时，"
    "使用系统生成的人工介入提示，不自行承诺具体处理结果或超出店铺 SLA 的时间。\n"
    "13. 顾客辱骂/威胁/不当内容：保持礼貌不争辩，重申可处理事项，必要时结束对话转人工。\n"
    "14. 始终以顾客【当前这条消息】为准来回答。如果顾客切换了话题"
    "（比如上一轮在查物流、这一轮问商品），就当作全新问题来回答，"
    "不要沿用上一轮的订单/物流/商品信息，除非顾客当前消息明确提到了它们。\n"
    "15. 不要主动重复顾客上一轮问过、但这一轮已经不再涉及的内容。\n"
    "16. 订单号、手机号后四位和写操作参数只能来自顾客当前消息；严禁从系统示例、历史对话或常识中猜测。"
    "顾客未在当前消息中提供手机号后四位时，必须先请其补充，绝不能调用订单或物流工具。\n"
    "17. 物流节点和预计日期均使用绝对日期表述。预计日期已经过去时，不得说「今天能到」或继续承诺送达。\n"
    "18. 可信业务数据中只有一个商品符合顾客明确说出的品类时，直接使用该商品核价或建议，不重复追问 SKU。"
)

# 新会话首次响应（这是顾客的第一条消息）
_PERSONA = _BASE_PERSONA + (
    "\n现在是新会话的开始。若顾客只是问候，可简短自我介绍并引导；"
    "若顾客已经提出具体问题，只需用一句话表明身份并立即完整解决当前问题，不能只回复欢迎语。"
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
        trusted_context: dict[str, Any] | None = None,
    ) -> dict:
        """执行 Agent Loop，返回 {reply, tool_calls}。"""
        current_time = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %z")
        persona = _PERSONA if is_first_turn else _PERSONA_FOLLOWUP
        messages: list[dict] = [{
            "role": "system",
            "content": f"{persona}\n当前本地时间：{current_time}。涉及日期时以此为准。",
        }]
        if trusted_context:
            messages.append({
                "role": "system",
                "content": (
                    "以下是后端提供的可信业务数据。商品、价格、库存、优惠和规则只能以这些数据为准；"
                    "数据中没有的信息要明确说明查不到，不得补写不存在的 SKU、商品名或活动。\n"
                    + json.dumps(trusted_context, ensure_ascii=False, default=str)
                ),
            })
        for item in history[-8:]:
            role = item.get("role")
            if role in {"user", "assistant"}:
                messages.append({"role": role, "content": item.get("content", "")[:1500]})
        messages.append({"role": "user", "content": message_text[:2000]})

        tool_calls_log: list[dict] = []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for _ in range(6):
                payload: dict[str, Any] = {
                    "model": self.model,
                    "temperature": 0.3,
                    "messages": messages,
                    "tools": ZHIXIA_TOOL_SCHEMAS,
                }
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
                        failed = isinstance(result, dict) and (
                            bool(result.get("error")) or result.get("ok") is False
                        )
                        tool_calls_log.append({
                            "name": fn_name,
                            "status": "error" if failed else "ok",
                            "args": fn_args,
                            "result": result,
                        })
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": json.dumps(result, ensure_ascii=False, default=str),
                        })
                    continue

                return {"reply": (msg.get("content") or "").strip(), "tool_calls": tool_calls_log}

        return {"reply": "抱歉，我还在为您核实，请稍后再试。", "tool_calls": tool_calls_log}
