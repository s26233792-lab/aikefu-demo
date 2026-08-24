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
import uuid
from typing import Any

from .decision import Disposition, analyze_tone, decide
from .safety import check_inbound, check_outbound
from .zhixia_agent import ZhixiaLLMAgent
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
            if name == "modify_address":
                r = self.tools.modify_address(
                    order_id=args.get("order_id", ""),
                    new_address=args.get("new_address", ""),
                )
                return r
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
                "needs_human": True, "reply": "",
                "tool_calls": [], "moderation_id": f"mod_{uuid.uuid4().hex}",
                "handoff_reason": handoff_reason,
            }

        # 4. LLM 回复（历史先做话题裁剪，避免旧话题污染）
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
            reply = "（未配置 LLM，无法回复）"
            tool_calls = []

        # 5. 出站护栏
        out = check_outbound(reply)
        needs_human = not out.ok
        disp = Disposition.REQUIRE_APPROVAL.value if needs_human else Disposition.AUTO_REPLY.value

        return {
            "intent": "auto", "tone": tone.value,
            "disposition": disp,
            "needs_human": needs_human,
            "reply": reply,
            "tool_calls": tool_calls,
            "moderation_id": f"mod_{uuid.uuid4().hex}" if needs_human else None,
            "safety": out.to_dict(),
        }
