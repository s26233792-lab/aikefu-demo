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
from .zhixia_agent import ZhixiaLLMAgent, looks_like_internal_analysis
from .zhixia_rules import ZhixiaRuleAgent
from .zhixia_tools import ZhixiaTools

# 转人工触发词（agent.md 第 11 节）
HANDOFF_KEYWORDS = (
    "投诉", "差评", "平台介入", "12315", "工商", "法院", "报警",
    "转人工", "人工客服", "骗子", "欺诈", "气死", "垃圾",
)


def _handoff_ack(text: str, tone: str) -> str:
    """生成可直接发给顾客的一次性短安抚语，不承诺处理结果或时效。"""
    if tone == "needs_human":
        return "好的，我这边马上转人工客服为您处理，请稍等一下。"
    if any(word in text for word in ("物流", "快递", "没到", "没发货", "退款", "未到账")):
        return "抱歉让您久等了，我已经转给人工客服核实处理，请稍等一下。"
    if any(word in text for word in ("色差", "线头", "破损", "开线", "瑕疵", "错发", "少件")):
        return "真的很抱歉让您遇到这个情况，我已经转给人工客服核实，请稍等一下。"
    return "很抱歉这次没有让您满意，我已经转给人工客服介入处理，请稍等一下。"

# 话题分组关键词：用于检测顾客是否切换话题（避免历史上下文污染）。
# 顺序很重要：先匹配更具体的话题，避免“退款”里的“款”被识别成商品。
_TOPIC_GROUPS: dict[str, tuple[str, ...]] = {
    "aftersale": ("退货", "换货", "退款", "退钱", "仅退款", "能退", "可以退", "退吗", "退掉", "退回", "售后", "七天无理由", "质量问题", "色差", "破损", "少件", "错发", "价保", "补差", "补偿"),
    "logistics": ("物流", "快递", "到哪", "几天到", "多久到", "签收", "运单", "轨迹", "派送", "送达", "催发", "发货", "到货"),
    "order": ("订单", "查单", "核对订单", "订单明细", "待付款", "付款", "催付", "备注", "改地址", "修改地址", "取消订单", "拦截"),
    "campaign": ("活动", "满减", "折扣", "优惠", "优惠券", "促销"),
    "member": ("会员", "积分", "等级", "成长值", "券"),
    "product": ("商品", "推荐", "这款", "那款", "款式", "颜色", "尺码", "材质", "面料", "价格", "多少钱", "衬衫", "西装", "裙", "开衫", "裤", "背心", "穿搭", "通勤", "面试", "约会", "有货", "库存", "深灰", "灰色", "卡其", "黑色", "白色", "雾蓝", "奶杏", "酒红", "珍珠白", "M码", "L码", "S码", "XL码", "胸围", "腰围", "臀围", "肩宽", "身高", "体重", "SKU"),
    "service": ("客服时间", "几点下班", "几点上班", "营业时间", "开发票", "发票", "隐私", "手机号", "验证码"),
    "chitchat": ("你好", "在吗", "谢谢", "再见", "你是谁", "你们"),
}

# 只有明确的指代表达才允许继承历史。不能再用裸字“这/那”做子串匹配，
# 否则“这个活动”“这次退款”等独立新问题都会错误继承上一轮。
_REFERENCE_RE = re.compile(
    r"(?:^|[，。！？、\s])(?:这个|那个|这件|那件|它|上面(?:那个|那款)?|刚才(?:说的)?|"
    r"前面(?:说的)?|就它|就是这个|就是那个)(?:呢|怎么样|可以吗|还有吗)?(?:$|[，。！？、\s])"
)
_SHORT_FOLLOWUP_RE = re.compile(
    r"^(?:(?:嗯|对|是的|没错)[，,\s]*)?(?:好|好的|好呀|行|可以|不可以|确认|确认写入|确认备注|要|不要|需要|不需要|"
    r"有|没有|还有吗|多少钱|多久|为什么|怎么弄|怎么办|哪个|都要|就这个|就那个|"
    r"\d{4}|\d{6}|ZX\d{12})[。！？!?]?$",
    re.IGNORECASE,
)


def naturalize_customer_reply(reply: str, *, suppress_intro: bool = False) -> str:
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
        "这个我无法保证": "到货日期暂时无法保证",
        "核实并安排补发或相应处理": "核实后按结果处理",
    }
    for source, target in fulfillment_replacements.items():
        reply = reply.replace(source, target)
    if suppress_intro:
        reply = re.sub(
            r"^\s*(?:您好|你好)?[，,～~！!。]*\s*我是.{0,30}?客服(?:小栀)?[。！？，,～~]*\s*",
            "",
            reply,
            count=1,
        )
        reply = reply.replace("作为栀夏女装客服小栀，", "作为店铺客服，")
    # 最后的兜底：即使模型偏离提示，也不把内部测试语境暴露给顾客。
    reply = reply.replace("演示", "").replace("模拟", "").replace("`", "")
    # 只移除 Markdown 加粗符号，保留地址脱敏中的连续星号（例如“文三路***号”）。
    masked_star_runs: list[str] = []

    def protect_mask(match: re.Match[str]) -> str:
        masked_star_runs.append(match.group(0))
        return f"\x00MASKED_STARS_{len(masked_star_runs) - 1}\x00"

    reply = re.sub(r"\*{3,}", protect_mask, reply)
    reply = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"\1", reply)
    for index, stars in enumerate(masked_star_runs):
        reply = reply.replace(f"\x00MASKED_STARS_{index}\x00", stars)
    # 客服聊天框使用纯文本项目符号，清除模型偶尔输出的 Markdown 列表标记。
    reply = re.sub(r"(?m)^\s*[-*]\s+", "· ", reply)
    reply = re.sub(r"(?m)^\s*(\d+)\.\s+", r"\1、", reply)
    reply = re.sub(r"\n{3,}", "\n\n", reply)
    return reply.strip()


def detect_topic(text: str) -> str:
    """识别消息主题；明确主题优先于指代，防止新问题误继承上文。"""
    for topic, words in _TOPIC_GROUPS.items():
        if any(w in text for w in words):
            return topic
    if _REFERENCE_RE.search(text.strip()) or _SHORT_FOLLOWUP_RE.fullmatch(text.strip()):
        return "reference"
    return "other"


class ZhixiaRuntime:
    def __init__(self, *, llm_agent: ZhixiaLLMAgent | None = None, tools: ZhixiaTools | None = None) -> None:
        self.llm_agent = llm_agent
        self.tools = tools or ZhixiaTools()
        self.rule_agent = ZhixiaRuleAgent(self.tools)

    @staticmethod
    def _build_llm_history(text: str, history: list[dict[str, str]]) -> list[dict[str, str]]:
        """裁剪历史，避免话题切换时上一轮内容污染回答。

        - 明确指代或短确认 → 只保留最近两轮；
        - 同一个话题 → 只保留最近一轮问答；
        - 切换到全新话题 → 清空历史。
        """
        if not history:
            return []
        current_topic = detect_topic(text)
        if current_topic == "reference":
            return history[-4:]

        # 无法识别的完整新问题默认不带历史。宁可追问一次，也不把旧答案带进来。
        if current_topic in ("other", "chitchat"):
            return []

        last_user_index = next(
            (
                index
                for index in range(len(history) - 1, -1, -1)
                if history[index].get("role") == "user"
            ),
            None,
        )
        if last_user_index is None:
            return []
        last_topic = detect_topic(history[last_user_index].get("content", ""))
        if last_topic != current_topic:
            return []
        # 只给模型最近一轮同话题问答，不混入更早的“other/reference”内容。
        return history[last_user_index:][-2:]

    def _tool_executor(
        self,
        *,
        current_text: str = "",
        history: list[dict[str, str]] | None = None,
    ):
        """工具执行器：注入可信上下文，写操作沙箱化。"""

        recent_history = history or []

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
            if name == "add_order_note":
                explicit_confirmation = any(
                    marker in current_text
                    for marker in ("确认", "确认写入", "确认备注", "确认提交", "就按这个")
                )
                note_context = "备注" in current_text or any(
                    item.get("role") == "assistant"
                    and "备注" in item.get("content", "")
                    and "确认" in item.get("content", "")
                    and "写入" in item.get("content", "")
                    for item in recent_history[-4:]
                )
                return self.tools.add_order_note(
                    order_id=args.get("order_id", ""),
                    phone_last4=args.get("phone_last4", ""),
                    note=args.get("note", ""),
                    confirmed=bool(args.get("confirmed", False) and explicit_confirmation and note_context),
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

    async def handle(
        self,
        *,
        text: str,
        history: list[dict[str, str]] | None = None,
        suppress_intro: bool = False,
    ) -> dict:
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
                "reply": _handoff_ack(text, tone.value),
                "tool_calls": [], "moderation_id": f"mod_{uuid.uuid4().hex}",
                # 渠道先发送这一条短安抚语并提醒人工；是否暂停后续自动回复
                # 由各渠道的接管策略决定。
                "send_before_handoff": True,
                "handoff_reason": handoff_reason,
            }

        # 4. LLM 回复（历史先做话题裁剪，避免旧话题污染）
        intent = "auto"
        fallback_needs_human = False
        if self.llm_agent is not None:
            try:
                llm_history = self._build_llm_history(text, history)
                # 千帆/飞鸽等外部客服工作台往往已经显示平台欢迎语或人工接入语。
                # 即便本地数据库里还没有历史，也按后续会话口吻直接解决问题。
                is_first = len(history) == 0 and not suppress_intro
                result = await self.llm_agent.run(
                    message_text=text, history=llm_history,
                    tool_executor=self._tool_executor(current_text=text, history=llm_history),
                    is_first_turn=is_first,
                )
                reply = result["reply"]
                tool_calls = result["tool_calls"]
                # 两次模型约束后仍输出内部工作笔记时，宁可退回确定性客服回复，
                # 也不能把“顾客提到/我需要核实”一类分析发送出去。
                if looks_like_internal_analysis(reply):
                    safe_result = self.rule_agent.run(
                        text,
                        is_first_turn=(len(history) == 0 and not suppress_intro),
                    )
                    intent = safe_result["intent"]
                    reply = safe_result["reply"]
                    tool_calls = safe_result["tool_calls"]
                    fallback_needs_human = safe_result.get("needs_human", False)
            except Exception:  # noqa: BLE001
                # 不泄露内部错误给顾客，统一转人工兜底
                reply = "抱歉，我这边暂时无法处理，已为您转交人工客服，请稍候。"
                tool_calls = []
        else:
            result = self.rule_agent.run(
                text,
                is_first_turn=(len(history) == 0 and not suppress_intro),
            )
            intent = result["intent"]
            reply = result["reply"]
            tool_calls = result["tool_calls"]
            fallback_needs_human = result.get("needs_human", False)

        # 5. 客服口径规范化 + 出站护栏
        reply = naturalize_customer_reply(reply, suppress_intro=suppress_intro)
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
        lookup_retry = any(
            call.get("name") in {"order_lookup", "logistics_lookup"}
            and (
                call.get("result") is None
                or (
                    isinstance(call.get("result"), dict)
                    and call["result"].get("error") in {"verify_required", "verify_failed"}
                )
            )
            for call in tool_calls
        )
        lookup_failure = any(
            call.get("name") in {"order_lookup", "logistics_lookup"}
            and isinstance(call.get("result"), dict)
            and call["result"].get("error") not in {None, "verify_required", "verify_failed"}
            for call in tool_calls
        )
        order_note_call = next(
            (call for call in reversed(tool_calls) if call.get("name") == "add_order_note"),
            None,
        )
        order_note_result = order_note_call.get("result") if order_note_call else None
        if order_note_call and isinstance(order_note_result, dict):
            if order_note_result.get("ok"):
                note_summary = str(order_note_result.get("note_summary", "")).strip()
                reply = (
                    f"订单备注已记录：“{note_summary}”。备注会供客服和仓库参考，"
                    "但不代表配送时间或其他要求一定能够满足。"
                )
            else:
                note_error = order_note_result.get("error")
                if note_error == "confirm_required":
                    note_summary = str(order_note_result.get("note_summary", "")).strip()
                    reply = f"准备写入的备注是“{note_summary}”。确认将这段内容写入订单备注吗？"
                elif note_error == "sensitive_content":
                    reply = "备注中不能包含完整手机号、身份证、银行卡、验证码或支付密码。请删去敏感信息后重新提供备注内容。"
                elif note_error == "note_too_long":
                    reply = "订单备注最多 80 字，请精简后重新发送。"
                else:
                    reply = "订单备注暂未写入，请核对订单信息和备注内容后重试。"
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
        elif order_note_call and isinstance(order_note_result, dict):
            intent = "order_note_written" if order_note_result.get("ok") else "order_note_confirm"
        elif logistics_exception and safe_exception_review:
            intent = "logistics_exception"
        elif fulfillment_exception and safe_exception_review:
            intent = "shipping_exception"
        elif requested_human_review or lookup_failure:
            intent = "human_review"
        elif lookup_retry:
            intent = "order_verification_retry"
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
