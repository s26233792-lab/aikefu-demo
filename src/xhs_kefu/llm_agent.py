"""小红书千帆客服 Agent —— DeepSeek LLM Agent Loop。

忠实还原参考架构的"单 Agent 主链路"：LLM 理解问题 → 调用工具查事实 →
拿到工具结果 → 结合会话上下文生成最终客服回复。

关键与参考架构一致的安全边界：
- 业务事实只来自工具结果，模型禁止凭记忆编造；
- 写操作（改地址/拦截/补偿）由后端风控校验，模型输出无权绕过；
- 订单号/SKU/金额由确定性解析器从顾客文本抽取，模型参数按不可信处理。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from .tool_schemas import TOOL_SCHEMAS


@dataclass
class LLMResult:
    """一次 Agent 调用的结果。"""

    reply: str
    intent: str | None
    tool_calls: list[dict] = field(default_factory=list)


class LLMAgent:
    """DeepSeek function calling 的完整 Agent Loop。"""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None,
        timeout: float = 60.0,
    ) -> None:
        self.url = f"{base_url.rstrip('/')}/chat/completions"
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    async def run(
        self,
        *,
        message_text: str,
        history: list[dict[str, str]],
        tool_executor: Callable[[str, dict], Any],
    ) -> LLMResult:
        """执行一次完整的 Agent Loop，返回最终回复与工具调用记录。

        tool_executor(name, arguments) 由 runtime 提供，负责真实调用工具并返回 JSON 事实。
        """
        system = (
            "你是小红书店铺「Icetea冻柠」的客服「小柠」。"
            "目标：先核实事实，再回复顾客，语气亲切专业、简洁。\n"
            "规则：\n"
            "1. 只基于工具返回的事实回复，严禁凭记忆编造订单号、物流、库存、金额、商品信息。\n"
            "2. 可通过 function calling 调用工具查询订单/物流/商品，或发起改地址/拦截/补偿。\n"
            "3. 写操作（modify_address / intercept_express / compensate）只在顾客明确提出时才调用，"
            "金额必须是顾客明确说的数字，不得自己编。\n"
            "4. 涉及隐私（收货手机号、微信、身份证）不得索取、不得输出。\n"
            "5. 若顾客的问题不明确（如只说'多少钱'但没说是哪件商品），"
            "先调用 product_lookup 获取店内商品，再结合会话上下文判断指哪件商品；"
            "若仍无法确定，就列出最相关的商品及价格并反问顾客。\n"
            "6. 用中文回复，直接给出顾客可用的答案，不要输出思考过程、不要输出 JSON。\n"
            "售后规则（必须严格遵守）：\n"
            "7. 退款/赔偿/退货/改地址/拦截等售后诉求，必须先核实订单实付金额，"
            "再决定方案，禁止未核实就承诺金额。\n"
            "8. 顾客反馈破损/错发/少发/质量时，优先「不退货小额赔偿」挽留："
            "默认建议 3 元且不超过实付；只有顾客不接受 3 元愿留货且实付≥15 元时，才可提至 5 元。"
            "严禁主动承诺退货退款或承担退回运费。\n"
            "9. 若订单备注（seller_memo）含「没发」，或为缺货纸条单/礼物单，"
            "则按全额仅退款处理（金额=实付全额），不走 3/5 元赔偿。\n"
            "10. 引导售后原因选「与商家协商一致」，并说明：该原因是系统自动审批、通常很快通过；"
            "其他原因需人工审核、会慢很多。\n"
            "11. 禁止承诺「核实后主动回复/回访/再确认」，只能在本轮响应；"
            "本轮未核实完成时，只让顾客补发订单号/订单详情页/凭证。\n"
            "12. 直播间买的尺码/颜色/款式以主播回复为准，店铺不支持换货；"
            "直播间付款金额错了，让顾客退款按正确金额重拍。"
        )
        messages: list[dict] = [{"role": "system", "content": system}]
        for item in history[-8:]:
            messages.append({"role": item["role"], "content": item["content"][:1500]})
        messages.append({"role": "user", "content": message_text[:2000]})

        tool_calls_log: list[dict] = []
        max_rounds = 6  # 防止 tool loop 无限循环

        for _ in range(max_rounds):
            payload: dict[str, Any] = {
                "model": self.model,
                "temperature": 0.2,
                "messages": messages,
            }
            if TOOL_SCHEMAS:
                payload["tools"] = TOOL_SCHEMAS

            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()

            msg = data["choices"][0]["message"]
            finish_reason = data["choices"][0].get("finish_reason")

            # 若模型要调用工具
            tool_calls = msg.get("tool_calls")
            if tool_calls and finish_reason == "tool_calls":
                messages.append(msg)  # 保留 assistant 的 tool_calls 消息
                for tc in tool_calls:
                    fn_name = tc["function"]["name"]
                    try:
                        fn_args = json.loads(tc["function"].get("arguments", "{}"))
                    except json.JSONDecodeError:
                        fn_args = {}
                    # 执行工具，拿到事实
                    result = tool_executor(fn_name, fn_args)
                    tool_calls_log.append(
                        {"name": fn_name, "status": "ok", "args": fn_args, "result": result}
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": json.dumps(result, ensure_ascii=False, default=str),
                        }
                    )
                continue  # 继续下一轮，让模型基于工具结果生成回复

            # 没有 tool_calls：拿到最终回复
            reply = (msg.get("content") or "").strip()
            intent = self._infer_intent(tool_calls_log)
            return LLMResult(reply=reply, intent=intent, tool_calls=tool_calls_log)

        # 超过轮数：用最后一轮 content 兜底
        return LLMResult(
            reply="抱歉，我正在核实信息，请稍后再试。",
            intent=None,
            tool_calls=tool_calls_log,
        )

    @staticmethod
    def _infer_intent(tool_calls_log: list[dict]) -> str | None:
        """从工具调用记录推断意图（供追踪展示，不作业务决策）。"""
        names = [tc["name"] for tc in tool_calls_log]
        if "modify_address" in names:
            return "modify_address"
        if "intercept_express" in names:
            return "intercept_express"
        if "compensate" in names:
            return "compensation"
        if "logistics_lookup" in names:
            return "logistics_status"
        if "product_lookup" in names:
            return "product_question"
        if "order_lookup" in names:
            return "order_status"
        return None
