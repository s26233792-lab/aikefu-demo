"""LLM 意图分类器。

用 DeepSeek 判断用户消息的真实意图，替代纯关键词匹配。核心价值：
能区分语义上的「咨询」vs「诉求」，例如：
- 「退款规则是什么」→ 咨询（可自动回复规则）
- 「我要退款」→ 退款诉求（转人工审批）

返回一个明确的意图枚举 + 是否需人工，再由确定性规则做安全兜底。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx


class Intent(StrEnum):
    """用户消息意图分类。"""

    PRODUCT_QUERY = "product_query"        # 商品咨询（推荐/参数/价格/库存）
    FAQ_QUERY = "faq_query"                # 常见问题（发货/退换规则/发票）
    LOGISTICS_QUERY = "logistics_query"    # 物流查询（到哪了/几天到）
    ORDER_QUERY = "order_query"            # 订单查询（状态/金额）
    REFUND_REQUEST = "refund_request"      # 退款/退货诉求
    COMPENSATION_REQUEST = "compensation_request"  # 补偿/赔偿诉求
    MISSING_ITEM = "missing_item"          # 少发/漏发/空袋诉求
    ADDRESS_CHANGE = "address_change"      # 改地址诉求
    INTERCEPT = "intercept"                # 快递拦截诉求
    COMPLAINT = "complaint"                # 投诉/情绪强烈/要求转人工
    CHITCHAT = "chitchat"                  # 闲聊/寒暄
    OUT_OF_SCOPE = "out_of_scope"          # 无关问题
    AMBIGUOUS = "ambiguous"                # 含糊、无法判断

    @property
    def needs_human(self) -> bool:
        """是否需要人工介入。"""
        return self in {
            Intent.REFUND_REQUEST,
            Intent.COMPENSATION_REQUEST,
            Intent.MISSING_ITEM,
            Intent.ADDRESS_CHANGE,
            Intent.INTERCEPT,
            Intent.COMPLAINT,
        }

    @property
    def is_write(self) -> bool:
        """是否写操作（需人工审批，非直接拒绝）。"""
        return self in {
            Intent.REFUND_REQUEST,
            Intent.COMPENSATION_REQUEST,
            Intent.MISSING_ITEM,
            Intent.ADDRESS_CHANGE,
            Intent.INTERCEPT,
        }


@dataclass(frozen=True, slots=True)
class IntentResult:
    intent: Intent
    confidence: float          # 0~1
    needs_human: bool
    reason: str = ""


_SYSTEM_PROMPT = (
    "你是电商客服的意图分类器。判断顾客消息属于哪个意图，只返回 JSON。\n"
    "意图类别（intent 字段取值）：\n"
    "- product_query：商品咨询（推荐/材质/价格/库存/尺码）\n"
    "- faq_query：常见问题（发货时间/多久到/退换货规则/发票/怎么下单）\n"
    "- logistics_query：物流查询（到哪了/几天到/快递进度）\n"
    "- order_query：订单查询（订单状态/金额）\n"
    "- refund_request：退款/退货诉求（我要退款、申请退货）\n"
    "- compensation_request：赔偿/补偿诉求（赔钱、给点补偿）\n"
    "- missing_item：少发/漏发/空袋/缺货诉求\n"
    "- address_change：修改收货地址诉求\n"
    "- intercept：拦截快递/追回诉求\n"
    "- complaint：投诉/强烈不满/要求转人工/差评威胁\n"
    "- chitchat：寒暄闲聊（你好、在吗）\n"
    "- out_of_scope：与客服无关的问题\n"
    "- ambiguous：含糊无法判断\n"
    "关键区分：\n"
    "1. 「退款规则是什么/怎么退款」是 faq_query（咨询），「我要退款/给我退款」才是 refund_request（诉求）。\n"
    "2. 咨询语气（是什么、怎么、能…吗、可以…吗）通常是 query；命令/诉求语气（我要、帮我、给我、申请）通常是 request。\n"
    "3. 投诉、强烈情绪、明确说要转人工 → complaint。\n"
    "返回 JSON 格式（不要有多余文字）：\n"
    '{"intent": "<上面某个类别>", "confidence": <0到1的小数>}'
)


class IntentClassifier:
    """用 LLM 判断意图。失败时安全回落到确定性规则（intent_fallback）。"""

    def __init__(self, *, base_url: str, model: str, api_key: str | None, timeout: float = 15.0) -> None:
        self.url = f"{base_url.rstrip('/')}/chat/completions"
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    async def classify(self, text: str) -> IntentResult:
        try:
            intent, confidence = await self._request(text)
            return IntentResult(
                intent=intent,
                confidence=confidence,
                needs_human=intent.needs_human,
                reason=intent.value,
            )
        except Exception:
            # LLM 失败回落确定性规则
            return fallback_classify(text)

    async def _request(self, text: str) -> tuple[Intent, float]:
        payload = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": text[:2000]},
            ],
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(self.url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        content = data["choices"][0]["message"]["content"]
        raw = json.loads(content)
        intent = Intent(raw.get("intent", "ambiguous"))
        confidence = float(raw.get("confidence", 0.5))
        return intent, confidence


def fallback_classify(text: str) -> IntentResult:
    """确定性兜底分类器（LLM 不可用时）。"""
    from .decision import detect_escalation, detect_write_intent

    if detect_escalation(text):
        return IntentResult(Intent.COMPLAINT, 1.0, True, "escalation")
    write = detect_write_intent(text)
    if write == "REFUND_REQUEST":
        return IntentResult(Intent.REFUND_REQUEST, 0.9, True, write)
    if write == "COMPENSATION_REQUEST":
        return IntentResult(Intent.COMPENSATION_REQUEST, 0.9, True, write)
    if write == "MISSING_ITEM_REQUEST":
        return IntentResult(Intent.MISSING_ITEM, 0.9, True, write)
    if write == "ADDRESS_CHANGE":
        return IntentResult(Intent.ADDRESS_CHANGE, 0.9, True, write)
    if write == "INTERCEPT_EXPRESS":
        return IntentResult(Intent.INTERCEPT, 0.9, True, write)
    # 咨询类
    if any(w in text for w in ("物流", "快递", "到哪", "几天到")):
        return IntentResult(Intent.LOGISTICS_QUERY, 0.8, False, "logistics")
    if any(w in text for w in ("推荐", "材质", "价格", "多少钱", "尺码", "库存", "参数")):
        return IntentResult(Intent.PRODUCT_QUERY, 0.8, False, "product")
    if any(w in text for w in ("发货", "多久", "发票", "退款规则", "退换", "怎么")):
        return IntentResult(Intent.FAQ_QUERY, 0.7, False, "faq")
    if any(w in text for w in ("hello", "hi", "你好", "在吗")):
        return IntentResult(Intent.CHITCHAT, 0.9, False, "chitchat")
    return IntentResult(Intent.AMBIGUOUS, 0.5, False, "ambiguous")
