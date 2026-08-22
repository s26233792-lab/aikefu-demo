"""MVP 编排层：把 Router → 子 Agent → Guardrails → 自动回复/转人工 串成一条链路。

这是整个多 Agent 架构的入口。输入一条 PlatformMessage，输出最终处置。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .adapter import PlatformMessage
from .agents import AftersaleAgent, FAQAgent, ProductAgent
from .rag import RAG
from .router import Route, RouterAgent
from .tools_api import ToolRegistry


@dataclass(frozen=True, slots=True)
class PipelineResult:
    platform: str
    customer_id: str
    route: str
    agent: str
    disposition: str       # auto_reply | require_approval | handoff_human
    reply: str
    reason_code: str
    facts: list[str]


class MVPPipeline:
    """多 Agent 客服链路。"""

    def __init__(self) -> None:
        self.rag = RAG()
        self.tools = ToolRegistry()
        self.router = RouterAgent()
        self.faq_agent = FAQAgent(self.rag)
        self.product_agent = ProductAgent(self.rag, self.tools)
        self.aftersale_agent = AftersaleAgent(self.rag, self.tools)

    def process(self, message: PlatformMessage) -> PipelineResult:
        # 1. 路由
        routing = self.router.route(message.text)

        # 2. 转人工（投诉/情绪升级）
        if routing.route == Route.HANDOFF:
            return PipelineResult(
                platform=message.platform,
                customer_id=message.customer_id,
                route=Route.HANDOFF.value,
                agent="—",
                disposition="handoff_human",
                reply="",
                reason_code=routing.confidence,
                facts=[],
            )

        # 3. 分派到对应子 Agent
        ctx = {
            "customer_id": message.customer_id,
            "order_id": self._extract_order_id(message.text),
            "platform": message.platform,
        }
        if routing.route == Route.FAQ:
            answer = self.faq_agent.answer(message.text, ctx)
            agent_name = "FAQ Agent"
        elif routing.route == Route.PRODUCT:
            answer = self.product_agent.answer(message.text, ctx)
            agent_name = "商品 Agent"
        else:
            answer = self.aftersale_agent.answer(message.text, ctx)
            agent_name = "售后 Agent"

        # 4. Guardrails（复用单 Agent 版的安全护栏判定写操作/转人工）
        disposition = answer.disposition
        reason = answer.reason_code
        # 写操作意图兜底：若子 Agent 没识别，但消息含写操作词，强制转人工
        if disposition == "auto_reply":
            write_w = ("退款", "退货", "赔偿", "补偿", "改地址", "拦截")
            if any(w in message.text for w in write_w):
                disposition = "require_approval"
                reason = reason or "WRITE_ACTION"

        return PipelineResult(
            platform=message.platform,
            customer_id=message.customer_id,
            route=routing.route.value,
            agent=agent_name,
            disposition=disposition,
            reply=answer.text,
            reason_code=reason,
            facts=answer.facts,
        )

    @staticmethod
    def _extract_order_id(text: str) -> str:
        import re
        # 不用 \b（汉字前后 \b 不生效），改用非字母数字边界
        m = re.search(r"(?<![A-Za-z0-9])XHS-\d{8}-\d{3}(?![A-Za-z0-9])", text, re.IGNORECASE)
        return m.group(0).upper() if m else ""
