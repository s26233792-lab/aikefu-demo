"""三个专业子 Agent：FAQ / 商品 / 售后。

每个 Agent 职责单一：
- 用 RAG 检索对应知识库；
- 必要时调用工具（商品/订单/物流 API）；
- 生成确定性回复。

MVP 用确定性规则生成回复（离线可跑），保留 LLM 增强位（后续可注入 LLM 生成更自然话术）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .rag import RAG
from .tools_api import ToolRegistry


@dataclass(frozen=True, slots=True)
class AgentReply:
    text: str
    disposition: str = "auto_reply"  # auto_reply | require_approval | handoff_human
    reason_code: str = ""
    facts: list[str] = field(default_factory=list)


class FAQAgent:
    """FAQ Agent：发货/退换货规则/尺码/发票等常见问题。"""

    name = "faq"

    def __init__(self, rag: RAG) -> None:
        self.rag = rag

    def answer(self, message_text: str, ctx: dict[str, Any] | None = None) -> AgentReply:
        sections = self.rag.search_faq(message_text, top_k=2)
        if not sections:
            return AgentReply(
                "抱歉，我没太理解您的问题。您可以问我：发货时间、多久到货、怎么退款、能开发票吗等。",
                "auto_reply", "NO_FAQ_MATCH",
            )
        # 取最相关片段，拼接"问+答"
        best = sections[0]
        lines = best["content"].splitlines()
        answers = [l for l in lines if l.startswith("答：") or (l and not l.startswith("问："))]
        if not answers and len(sections) > 1:
            sec2 = sections[1]
            answers = [l for l in sec2["content"].splitlines() if l.startswith("答：")]
        text = "\n".join(answers[:3]).strip() if answers else best["content"]
        return AgentReply(text, "auto_reply", "", facts=[best["title"]])


class ProductAgent:
    """商品 Agent：商品推荐/参数/价格/库存/尺码。"""

    name = "product"

    def __init__(self, rag: RAG, tools: ToolRegistry) -> None:
        self.rag = rag
        self.tools = tools

    def answer(self, message_text: str, ctx: dict[str, Any] | None = None) -> AgentReply:
        # 先从消息提取 SKU（若有）
        import re
        sku = None
        m = re.search(r"(?<![A-Za-z0-9])SKU-[A-Z0-9-]+(?![A-Za-z0-9])", message_text, re.IGNORECASE)
        if m:
            sku = m.group(0).upper()

        if sku:
            # 指定 SKU：查具体商品
            products = self.tools.product.lookup(sku)
        else:
            products = self.rag.search_products(message_text, top_k=3)
            if not products:
                products = self.tools.product.lookup()

        if not products:
            return AgentReply("抱歉，没有找到相关商品。您可以告诉我商品名称或 SKU，我帮您查询。", "auto_reply", "NO_PRODUCT")

        if sku or len(products) == 1:
            p = products[0]
            sizes = "、".join(p.get("sizes") or ["均码"])
            text = (
                f"【{p['name']}】\n"
                f"· 价格：¥{p['price_cents']/100:.2f}\n"
                f"· 材质：{p['material']}\n"
                f"· 尺码：{sizes}\n"
                f"· 库存：{p['stock']} 件\n"
                f"· 卖点：{'、'.join(p.get('selling_points', []))}"
            )
            return AgentReply(text, "auto_reply", "", facts=["product." + p["sku"]])
        else:
            # 多商品：列出推荐
            lines = [f"{i+1}. {p['name']} ¥{p['price_cents']/100:.2f}（{'、'.join(p.get('selling_points',[]))}）"
                     for i, p in enumerate(products)]
            text = "为您推荐这几款：\n" + "\n".join(lines) + "\n回复商品名可查看详情。"
            return AgentReply(text, "auto_reply", "", facts=["products.top_" + str(len(products))])


class AftersaleAgent:
    """售后 Agent：物流查询/退款/补偿/改地址/拦截。

    售后决策用 AftersalePolicyEngine（移植参考项目 SKILL.md 的规则）：
    缺货纸条单/礼物单/没发单全额仅退款、3元→5元小额赔偿挽留、
    证据要求、与商家协商一致引导。
    """

    name = "aftersale"

    def __init__(self, rag: RAG, tools: ToolRegistry) -> None:
        self.rag = rag
        self.tools = tools
        from xhs_kefu.aftersale_policy import AftersalePolicyEngine
        self.policy = AftersalePolicyEngine()

    def answer(self, message_text: str, ctx: dict[str, Any] | None = None) -> AgentReply:
        ctx = ctx or {}
        customer_id = ctx.get("customer_id", "")
        order_id = ctx.get("order_id", "")

        # 1. 物流查询（无写操作）
        if any(w in message_text for w in ("物流", "快递", "到哪", "签收", "催货", "几天到")):
            if order_id:
                shipment = self.tools.logistics.lookup(order_id)
                if shipment:
                    text = (
                        f"您的订单 {order_id} 物流状态：{shipment['status']}。"
                        f"最新进度：{shipment['latest_event']}（{shipment['updated_at']}）。"
                    )
                    return AgentReply(text, "auto_reply", "", facts=["logistics." + order_id])
                return AgentReply(f"订单 {order_id} 暂无物流轨迹，请稍后再试。", "auto_reply", "NO_SHIPMENT")
            return AgentReply("请提供订单号，我帮您查物流。", "auto_reply", "NEED_ORDER_ID")

        # 2. 改地址/拦截（高风险写操作，不涉及金额的售后）→ 转人工
        if any(w in message_text for w in ("改地址", "修改地址", "拦截")):
            return AgentReply(
                "该操作涉及收货信息/物流变更，属于高风险操作，需人工审核，已为您转交人工客服。",
                "require_approval", "HIGH_RISK_ACTION",
            )

        # 3. 退款/补偿/破损/少发 → 用 AftersalePolicyEngine 做售后风控决策
        issue_type = self.policy.classify_issue(message_text)
        has_evidence = bool(ctx.get("attachments") or ctx.get("has_evidence"))
        # 判断是否顾客不接受3元想升级（含"5元""再赔""多赔"等升级信号）
        upgrade = any(w in message_text for w in ("5元", "五元", "再多", "再赔", "不够", "不接受3元"))

        order = None
        if order_id:
            for o in self.tools.order.lookup(order_id=order_id, customer_id=customer_id):
                order = o
                break

        decision = self.policy.evaluate(
            order=order, issue_type=issue_type, user_text=message_text,
            has_evidence=has_evidence, upgrade_compensation=upgrade,
        )

        # 把 disposition 映射到 AgentReply
        if decision.verdict == "full_refund_gift":
            disp = "require_approval"  # 全额仅退款仍需人工确认执行
            reason = "FULL_REFUND_GIFT"
        elif decision.verdict == "small_compensate":
            disp = "require_approval"  # 补偿是写操作，转人工审批
            reason = "SMALL_COMPENSATE"
        elif decision.verdict == "need_evidence":
            disp = "auto_reply"  # 要证据是正常引导，可自动回复
            reason = "NEED_EVIDENCE"
        elif decision.verdict == "return_refund":
            disp = "require_approval"
            reason = "RETURN_REFUND"
        elif decision.verdict == "need_clarify":
            disp = "auto_reply"  # 要订单号是正常引导
            reason = "NEED_CLARIFY"
        else:
            disp = "handoff_human"
            reason = "NEED_HUMAN"

        return AgentReply(decision.message, disp, reason, facts=decision.rules)
