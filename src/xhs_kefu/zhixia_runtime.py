"""栀夏 ZHIXIA 女装客服 Agent —— 运行时编排。

流程：入站护栏 → 意图判断（LLM/规则）→ 转人工判定 → ZhixiaLLMAgent 回复 →
出站护栏 → 需人工时入待审队列。

与 agent.md 的对应：
- 转人工条件（第 11 节）：质量争议/退款超期/物流72h无更新/投诉升级/超规则赔付/数据缺失
- 写操作（改地址/取消订单）：仅沙箱 + 人工审批
- 敏感信息：不展示完整手机号/地址
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any

from .decision import Disposition, analyze_tone, decide
from .safety import check_inbound, check_outbound
from .zhixia_agent import ZhixiaLLMAgent
from .zhixia_rules import ZhixiaRuleAgent
from .zhixia_tools import ZhixiaTools

# 转人工触发词（agent.md 第 11 节）
HANDOFF_KEYWORDS = (
    "投诉", "差评", "平台介入", "12315", "工商", "法院", "报警",
    "转人工", "人工客服", "骗子", "欺诈", "气死", "垃圾",
)

# 话题分组关键词：用于检测顾客是否切换话题（避免历史上下文污染）
_TOPIC_GROUPS: dict[str, tuple[str, ...]] = {
    "product": ("推荐", "买", "款", "颜色", "尺码", "材质", "面料", "价格", "多少钱", "白衬衫", "西装", "裙子", "开衫", "阔腿裤", "背心", "穿搭", "通勤", "面试", "约会", "有货", "库存", "深灰", "灰色", "卡其", "黑色", "白色", "雾蓝", "奶杏", "酒红", "珍珠白", "M码", "L码", "S码", "XL码", "胸围", "腰围", "臀围", "身高", "体重"),
    "order": ("订单", "查单", "下单", "改地址", "发货", "取消"),
    "logistics": ("物流", "快递", "到哪", "几天到", "签收", "运单", "轨迹", "派送"),
    "aftersale": ("退", "换货", "退款", "售后", "七无", "价保", "补偿"),
    "member": ("会员", "积分", "等级", "成长值", "券"),
    "chitchat": ("你好", "在吗", "谢谢", "再见", "你是谁", "你们"),
}

# 指代词：含这些词说明是"跟随上一轮话题"，不应裁历史
_REFERENCE_WORDS = ("那", "这个", "这", "它", "上面", "刚才", "这款", "那款", "那件", "它家", "就它", "这个有", "那有")


def naturalize_customer_reply(reply: str) -> str:
    """Remove internal/demo wording before a reply reaches a customer."""
    reply = re.sub(
        r"如果要体验查单[^。！？]*?(?:7319|手机号后四位)[^。！？]*[。！？]?",
        "",
        reply,
    )
    replacements = {
        "演示数据中暂未查询到": "暂未查询到",
        "演示环境": "当前服务",
        "演示订单": "订单",
        "测试用订单": "订单",
        "测试订单": "订单",
        "模拟订单号": "订单号",
        "模拟订单": "订单",
        "模拟数据": "系统数据",
    }
    for source, target in replacements.items():
        reply = reply.replace(source, target)
    fulfillment_replacements = {
        "会安排在次日发出": "通常在次日安排出库",
        "会在次日发出": "通常在次日安排出库",
        "当天是来不及出库的": "当天通常无法完成出库",
        "一般次日就会安排发货": "通常会在次日安排出库",
        "明天会优先为您安排出库": "预计明天优先安排出库，以实际进度为准",
        "稍等片刻就能看到更新": "可稍后留意物流更新",
        "仓库有进展我会第一时间同步给您": "请留意后续人工反馈",
        "我会持续帮您关注": "请留意后续进展",
        "我会第一时间同步给您": "请留意后续消息",
        "如有更新我会第一时间帮您跟进": "请留意后续人工反馈",
        "有任何进展我会帮您跟进": "后续请留意客服消息",
        "我可以帮您留意发货情况": "付款后可在订单页留意发货状态",
        "我会帮您留意发货情况": "请在订单页留意发货状态",
        "我可以帮您留意物流情况": "可在订单页查看最新物流状态",
        "通常需要 1~3 个工作日到账": "审核通过后通常 1～5 个工作日到账，以原支付渠道为准",
        "通常需要 1～3 个工作日到账": "审核通过后通常 1～5 个工作日到账，以原支付渠道为准",
        "保证当天发出": "优先安排当天出库，以实际进度为准",
        "保证明天发出": "预计明天安排出库，以实际进度为准",
        "保证准时送达": "会尽力按预计时效配送，以实际物流为准",
        "这个没法给您保证": "到货日期暂时无法保证",
        "这个无法给您保证": "到货日期暂时无法保证",
        "核实并安排补发或相应处理": "核实后按结果处理",
    }
    for source, target in fulfillment_replacements.items():
        reply = reply.replace(source, target)
    # 最后的兜底：即使模型偏离提示，也不把内部测试语境暴露给顾客。
    reply = reply.replace("演示", "").replace("模拟", "").replace("**", "").replace("`", "")
    return reply.strip()


def detect_topic(text: str) -> str:
    """识别消息主题分组。含指代词时返回 'reference'（跟随上文）。"""
    if any(w in text for w in _REFERENCE_WORDS):
        return "reference"
    for topic, words in _TOPIC_GROUPS.items():
        if any(w in text for w in words):
            return topic
    return "other"


class ZhixiaRuntime:
    def __init__(self, *, llm_agent: ZhixiaLLMAgent | None = None, tools: ZhixiaTools | None = None) -> None:
        self.llm_agent = llm_agent
        self.tools = tools or ZhixiaTools()
        self.rule_agent = ZhixiaRuleAgent(self.tools)

    @staticmethod
    def _build_llm_history(text: str, history: list[dict[str, str]]) -> list[dict[str, str]]:
        """裁剪历史，避免话题切换时上一轮内容污染回答。

        - 含指代词（"那/这个/它"）→ 跟随上文，不裁剪，保留最近若干轮；
        - 同一个话题 → 保留最近同话题轮次；
        - 切换到全新话题 → 清空历史。
        """
        current_topic = detect_topic(text)
        # 指代或无法识别时，尽量保留最近历史（帮助理解指代）
        if current_topic in ("reference", "other"):
            return history[-6:]
        # 同话题：保留最近同话题轮次（最多6轮）
        filtered: list[dict[str, str]] = []
        for item in reversed(history):
            if detect_topic(item.get("content", "")) in (current_topic, "reference", "other"):
                filtered.append(item)
            if len(filtered) >= 6:
                break
        return list(reversed(filtered))

    def _tool_executor(self):
        """工具执行器：注入可信上下文，写操作沙箱化。"""

        def execute(name: str, args: dict) -> Any:
            if name == "product_lookup":
                return self.tools.product_lookup(sku=args.get("sku"))
            if name == "order_lookup":
                return self.tools.order_lookup(
                    order_id=args.get("order_id", ""),
                    phone_last4=args.get("phone_last4"),
                )
            if name == "logistics_lookup":
                return self.tools.logistics_lookup(
                    order_id=args.get("order_id", ""),
                    phone_last4=args.get("phone_last4"),
                )
            if name == "member_lookup":
                return self.tools.member_lookup(phone_last4=args.get("phone_last4", ""))
            if name == "shop_policy_lookup":
                return self.tools.policy_lookup(topic=args.get("topic", ""))
            if name == "modify_address":
                r = self.tools.modify_address(
                    order_id=args.get("order_id", ""),
                    phone_last4=args.get("phone_last4", ""),
                    new_address=args.get("new_address", ""),
                )
                return r
            if name == "cancel_order":
                return self.tools.cancel_order(
                    order_id=args.get("order_id", ""),
                    phone_last4=args.get("phone_last4", ""),
                )
            if name == "request_human_review":
                return {
                    "ok": True,
                    "queued": True,
                    "reason": str(args.get("reason", "需人工复核"))[:200],
                    "note": "人工待办将在本轮结束后创建",
                }
            return {"error": f"unknown tool: {name}"}

        return execute

    async def handle(self, *, text: str, history: list[dict[str, str]] | None = None) -> dict:
        history = history or []
        # 1. 入站护栏（注入检测）
        inbound = check_inbound(text)
        if not inbound.ok:
            return {
                "intent": "security_rejected", "tone": "normal",
                "disposition": Disposition.REJECT.value,
                "needs_human": False, "reply": "抱歉，这个请求我无法处理。请咨询商品、订单或售后问题。",
                "tool_calls": [], "moderation_id": None,
            }

        # 2. 语气分析（两级）
        tone = analyze_tone(text)

        # 3. 转人工判定（agent.md 第 11 节条件）
        handoff_reason = None
        if tone in ("negative", "needs_human"):
            handoff_reason = f"tone={tone}"
        elif any(kw in text for kw in HANDOFF_KEYWORDS):
            handoff_reason = "关键词触发"
        elif "退" in text and ("5个工作日" in text or "5天" in text or "还没到账" in text):
            handoff_reason = "退款超期"

        if handoff_reason:
            return {
                "intent": "handoff_human", "tone": tone.value,
                "disposition": Disposition.HANDOFF_HUMAN.value,
                "needs_human": True,
                "reply": "已提交人工专员复核，预计 2 小时内反馈；23:00 后提交的申请将在次日 10:00 前优先处理。",
                "tool_calls": [], "moderation_id": f"mod_{uuid.uuid4().hex}",
                "handoff_reason": handoff_reason,
            }

        # 4. LLM 回复（历史先做话题裁剪，避免旧话题污染）
        intent = "auto"
        fallback_needs_human = False
        if self.llm_agent is not None:
            try:
                llm_history = self._build_llm_history(text, history)
                is_first = len(history) == 0
                result = await self.llm_agent.run(
                    message_text=text, history=llm_history,
                    tool_executor=self._tool_executor(),
                    is_first_turn=is_first,
                )
                reply = result["reply"]
                tool_calls = result["tool_calls"]
            except Exception:  # noqa: BLE001
                # 不泄露内部错误给顾客，统一转人工兜底
                reply = "抱歉，我这边暂时无法处理，已为您转交人工客服，请稍候。"
                tool_calls = []
        else:
            result = self.rule_agent.run(text)
            intent = result["intent"]
            reply = result["reply"]
            tool_calls = result["tool_calls"]
            fallback_needs_human = result.get("needs_human", False)

        # 5. 客服口径规范化 + 出站护栏
        reply = naturalize_customer_reply(reply)
        if not reply:
            reply = "您好，请问想了解商品、尺码，还是订单售后呢？"
        logistics_exception = any(
            bool(
                isinstance(call.get("result"), dict)
                and isinstance(call["result"].get("logistics_freshness"), dict)
                and call["result"]["logistics_freshness"].get("over_72h_no_update")
            )
            for call in tool_calls
        )
        fulfillment_exception = any(
            bool(
                isinstance(call.get("result"), dict)
                and isinstance(call["result"].get("fulfillment_freshness"), dict)
                and call["result"]["fulfillment_freshness"].get("over_48h_unshipped")
            )
            for call in tool_calls
        )
        high_risk_write = any(
            call.get("name") in {"modify_address", "cancel_order"}
            for call in tool_calls
        )
        requested_human_review = any(
            call.get("name") == "request_human_review"
            for call in tool_calls
        )
        human_review_reason = next(
            (
                str(call.get("args", {}).get("reason", "需人工复核"))[:200]
                for call in tool_calls
                if call.get("name") == "request_human_review"
            ),
            None,
        )
        safe_exception_review = (
            not requested_human_review
            or any(
                word in (human_review_reason or "")
                for word in ("物流", "发货", "未发", "派送", "揽收", "时效", "超时", "72小时")
            )
        )
        lookup_failure = any(
            call.get("name") in {"order_lookup", "logistics_lookup"}
            and (
                call.get("result") is None
                or (
                    isinstance(call.get("result"), dict)
                    and call["result"].get("error") not in {None, "verify_required"}
                )
            )
            for call in tool_calls
        )
        if logistics_exception or fulfillment_exception:
            # API 随后会创建人工待办，因此异常回执要直接告知已受理，
            # 不能让模型反问“是否需要处理”，导致顾客误以为尚未登记。
            reply = re.sub(
                r"(?:考虑到[^。！？]*[，,])?(?:我建议|建议)[^。！？]*(?:登记|催件|提交人工|人工复核)[^。！？]*[。！？]?",
                "",
                reply,
            )
            reply = re.sub(r"(?:这种情况)?我会为您提交人工专员复核[^。！？]*[。！？]?", "", reply)
            reply = re.sub(
                r"[^。！？\n]*(?:我(?:也)?会|我将)[^。！？\n]*(?:提交|登记|持续跟进|一并核实)[^。！？]*[。！？]?",
                "",
                reply,
            )
            reply = re.sub(r"您看(?:是否|需要)[^。！？]*[？?]", "", reply).strip()
            if "已提交人工专员复核" not in reply:
                reply = reply.rstrip("。 \n") + "。\n\n已提交人工专员复核，预计 2 小时内反馈；23:00 后提交的申请将在次日 10:00 前优先处理。"
        out = check_outbound(reply)
        if high_risk_write:
            intent = "order_change_request"
        elif logistics_exception and safe_exception_review:
            intent = "logistics_exception"
        elif fulfillment_exception and safe_exception_review:
            intent = "shipping_exception"
        elif requested_human_review or lookup_failure:
            intent = "human_review"
        needs_human = (
            fallback_needs_human
            or logistics_exception
            or fulfillment_exception
            or high_risk_write
            or requested_human_review
            or lookup_failure
            or not out.ok
        )
        disp = Disposition.REQUIRE_APPROVAL.value if needs_human else Disposition.AUTO_REPLY.value

        return {
            "intent": intent, "tone": tone.value,
            "disposition": disp,
            "needs_human": needs_human,
            "reply": reply,
            "tool_calls": tool_calls,
            "moderation_id": f"mod_{uuid.uuid4().hex}" if needs_human else None,
            # 发货/物流超时说明属于可安全自动发送的事实型回执；发送后仍进入人工队列。
            "send_before_handoff": (
                (logistics_exception or fulfillment_exception)
                and not high_risk_write
                and safe_exception_review
                and out.ok
            ),
            "handoff_reason": human_review_reason or ("订单核验失败" if lookup_failure else None),
            "safety": out.to_dict(),
        }
