"""栀夏 ZHIXIA 女装客服 Agent —— 运行时编排。

流程：入站护栏 → 意图判断（LLM/规则）→ 转人工判定 → ZhixiaLLMAgent 回复 →
出站护栏 → 需人工时入待审队列。

与 agent.md 的对应：
- 转人工条件（第 11 节）：质量争议/退款超期/物流72h无更新/投诉升级/超规则赔付/数据缺失
- 写操作（改地址/取消订单）：仅沙箱 + 人工审批
- 敏感信息：不展示完整手机号/地址
"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from typing import Any

from .decision import Disposition, analyze_tone
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

_ORDER_ID_RE = re.compile(r"\bZX\d{12}\b", re.IGNORECASE)
_PHONE_LAST4_RE = re.compile(
    r"(?:收货)?手机号(?:后)?四位\s*[:：为是]?\s*(\d{4})|"
    r"(?:手机尾号|尾号|后四位)\s*[:：为是]?\s*(\d{4})"
)
_SKU_RE = re.compile(r"\bZX-[A-Z]\d{3}\b", re.IGNORECASE)
_LOGISTICS_WORDS = ("物流", "快递", "到哪", "派件", "运单", "轨迹", "几天到", "什么时候发货", "发货了吗")


def detect_topic(text: str) -> str:
    """识别消息主题分组。含指代词时返回 'reference'（跟随上文）。"""
    if any(w in text for w in _REFERENCE_WORDS):
        return "reference"
    for topic, words in _TOPIC_GROUPS.items():
        if any(w in text for w in words):
            return topic
    return "other"


def _extract_order_id(text: str) -> str:
    match = _ORDER_ID_RE.search(text)
    return match.group(0).upper() if match else ""


def _extract_phone_last4(text: str) -> str:
    match = _PHONE_LAST4_RE.search(text)
    if not match:
        return ""
    return next((part for part in match.groups() if part), "")


def _extract_sku(text: str) -> str:
    match = _SKU_RE.search(text)
    return match.group(0).upper() if match else ""


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

    def _tool_executor(self, message_text: str):
        """工具执行器：只信任顾客当前消息中确定性提取出的标识。"""

        trusted_order_id = _extract_order_id(message_text)
        trusted_phone_last4 = _extract_phone_last4(message_text)
        trusted_sku = _extract_sku(message_text)

        def execute(name: str, args: dict) -> Any:
            if name == "product_lookup":
                args.clear()
                args.update({"sku": trusted_sku})
                return self.tools.product_lookup(sku=trusted_sku or None)
            if name == "order_lookup":
                args.clear()
                args.update({"order_id": trusted_order_id, "phone_last4": trusted_phone_last4})
                return self.tools.order_lookup(
                    order_id=trusted_order_id,
                    phone_last4=trusted_phone_last4,
                )
            if name == "logistics_lookup":
                args.clear()
                args.update({"order_id": trusted_order_id, "phone_last4": trusted_phone_last4})
                return self.tools.logistics_lookup(
                    order_id=trusted_order_id,
                    phone_last4=trusted_phone_last4,
                )
            if name == "member_lookup":
                args.clear()
                args.update({"phone_last4": trusted_phone_last4})
                return self.tools.member_lookup(phone_last4=trusted_phone_last4)
            if name == "modify_address":
                new_address = str(args.get("new_address", "")).strip()
                args.clear()
                args.update({
                    "order_id": trusted_order_id,
                    "phone_last4": trusted_phone_last4,
                    "new_address": new_address,
                })
                r = self.tools.modify_address(
                    order_id=trusted_order_id,
                    phone_last4=trusted_phone_last4,
                    new_address=new_address,
                )
                return r
            return {"error": f"unknown tool: {name}"}

        return execute

    @staticmethod
    def _base_result(*, reply: str, intent: str = "auto", tool_calls: list[dict] | None = None) -> dict:
        return {
            "intent": intent,
            "tone": "normal",
            "disposition": Disposition.AUTO_REPLY.value,
            "needs_human": False,
            "reply": reply,
            "tool_calls": tool_calls or [],
            "moderation_id": None,
            "safety": {"ok": True, "reason_code": "", "detail": ""},
        }

    @staticmethod
    def build_human_prompt(*, intent: str, reason: str, disposition: str) -> dict[str, Any]:
        """生成顾客可见提醒和客服侧介入指引。"""
        reason_labels = {
            "signed_not_received": "物流显示签收但顾客未收到",
            "logistics_stale_72h": "物流超过 72 小时未更新",
            "duplicate_charge": "疑似重复扣款或预授权占用",
            "account_security_risk": "账户或资金安全风险",
            "personal_injury": "商品可能引发人身不适",
            "counterfeit_dispute": "疑似假货或商品真实性争议",
            "refund_overdue": "退款超过承诺时效",
            "退款超期": "退款超过承诺时效",
            "verify_failed": "订单核验信息不一致",
            "order_not_found": "订单数据未查询到",
            "shipped_address_change": "已发货订单申请修改地址",
            "shipped_cannot_cancel": "已进入发货环节仍申请取消",
            "address_change_approval": "待发货订单修改地址需审核",
            "cancel_order_approval": "取消订单需人工确认仓库状态",
            "关键词触发": "投诉、监管或明确转人工诉求",
            "tone=needs_human": "顾客明确要求人工处理",
            "tone=negative": "顾客情绪激烈或问题反复未解决",
            "NEEDS_HUMAN": "需人工审核后处理",
        }
        urgent_reasons = {"duplicate_charge", "account_security_risk", "personal_injury"}
        high_reasons = {
            "signed_not_received", "counterfeit_dispute", "refund_overdue", "退款超期",
            "verify_failed", "order_not_found", "shipped_address_change", "shipped_cannot_cancel",
            "关键词触发",
        }
        if reason in urgent_reasons:
            priority, sla = "P0 紧急", "建议 10 分钟内首次响应"
        elif reason in high_reasons or reason.startswith("tone="):
            priority, sla = "P1 高", "建议 30 分钟内首次响应"
        else:
            priority, sla = "P2 常规", "建议 2 小时内首次响应"

        guidance: dict[str, tuple[str, list[str]]] = {
            "signed_not_received": (
                "核查签收照片、签收人和驿站记录，必要时联系承运方发起查件。",
                ["核对订单与运单", "调取签收凭证", "确认补发、退款或理赔路径"],
            ),
            "logistics_stale_72h": (
                "联系承运方确认异常节点，并给顾客明确的下次反馈时间。",
                ["核对最后轨迹", "登记承运方工单", "约定下一次反馈时间"],
            ),
            "duplicate_charge": (
                "核对平台支付流水与订单数量，区分重复扣款、预授权和银行延迟入账。",
                ["不要索要银行卡号或验证码", "核对平台流水号", "需要时升级支付渠道"],
            ),
            "account_security_risk": (
                "优先提醒顾客停止操作，并引导其联系平台与银行官方渠道冻结风险。",
                ["确认是否已泄露验证码或转账", "提示修改密码/联系银行", "记录冒充客服线索"],
            ),
            "personal_injury": (
                "先确认顾客人身安全，再登记商品批次和不适情况，升级售后负责人。",
                ["建议立即停止使用", "严重不适时建议及时就医", "不要远程诊断或承诺赔偿金额"],
            ),
            "counterfeit_dispute": (
                "登记商品批次、订单和质疑依据，交由商品或合规负责人复核。",
                ["保留商品与包装照片", "核查进货和质检记录", "避免未经核实直接下结论"],
            ),
            "refund_overdue": (
                "核对退款发起时间、渠道状态和失败原因，提供下一次明确反馈节点。",
                ["核对退款流水", "确认原支付渠道", "需要时升级财务或平台"],
            ),
            "退款超期": (
                "核对退款发起时间、渠道状态和失败原因，提供下一次明确反馈节点。",
                ["核对退款流水", "确认原支付渠道", "需要时升级财务或平台"],
            ),
            "verify_failed": (
                "通过平台安全核验流程确认订单归属，核验前不得披露订单详情。",
                ["只核对必要信息", "不得回显完整手机号或地址", "核验通过后再处理诉求"],
            ),
            "shipped_address_change": (
                "确认承运方是否支持改址或拦截，并提示改址不保证成功。",
                ["核对订单与运单状态", "联系承运方", "在平台安全入口收集新地址"],
            ),
            "shipped_cannot_cancel": (
                "评估快递拦截；无法拦截时说明拒收或到货退货路径。",
                ["确认仓库是否出库", "尝试承运方拦截", "告知顾客备选售后方案"],
            ),
            "address_change_approval": (
                "确认订单尚未锁单，并通过平台安全入口核验和修改收货信息。",
                ["再次确认订单状态", "避免在聊天中收集完整地址", "修改后向顾客回执结果"],
            ),
            "cancel_order_approval": (
                "确认仓库尚未锁单后执行取消，并核对是否已产生退款。",
                ["确认订单和仓库状态", "执行取消或说明失败原因", "告知退款渠道和预计时效"],
            ),
            "tone=needs_human": (
                "尽快接手当前会话，先确认顾客诉求，再根据上下文继续处理。",
                ["不要让顾客重复完整描述", "确认核心诉求", "说明接手后的下一步"],
            ),
            "tone=negative": (
                "先安抚情绪并复述问题，再给出可执行方案和明确反馈时间。",
                ["避免争辩或机械重复", "核对历史处理记录", "必要时升级值班负责人"],
            ),
        }
        default_action = (
            "阅读完整上下文，核实事实后接手回复；不要重复索取已提供的信息。",
            ["确认顾客核心诉求", "核对订单与店铺规则", "给出明确下一步和反馈时间"],
        )
        next_action, checklist = guidance.get(reason, default_action)
        is_approval = disposition == Disposition.REQUIRE_APPROVAL.value
        customer_notice = (
            "您的申请已进入人工审核。审核完成前请勿重复操作，结果以平台消息为准。"
            if is_approval
            else "已为您创建人工服务单。请保持平台消息畅通，无需重复发送相同问题；切勿发送验证码、支付密码或完整银行卡号。"
        )
        return {
            "title": "需要人工审核" if is_approval else "已转人工专员",
            "customer_notice": customer_notice,
            "reason": reason,
            "reason_label": reason_labels.get(reason, "复杂或高风险问题需人工处理"),
            "priority": priority,
            "sla": sla,
            "next_action": next_action,
            "checklist": checklist,
        }

    def _attach_human_prompt(self, response: dict[str, Any]) -> dict[str, Any]:
        disposition = response.get("disposition", Disposition.AUTO_REPLY.value)
        if disposition not in {
            Disposition.HANDOFF_HUMAN.value,
            Disposition.REQUIRE_APPROVAL.value,
        }:
            return response
        reason = str(
            response.get("handoff_reason")
            or (response.get("safety") or {}).get("reason_code")
            or "NEEDS_HUMAN"
        )
        response["human_prompt"] = self.build_human_prompt(
            intent=str(response.get("intent") or "handoff"),
            reason=reason,
            disposition=disposition,
        )
        return response

    def _handoff_result(
        self,
        *,
        reply: str,
        intent: str,
        reason: str,
        tool_calls: list[dict] | None = None,
    ) -> dict:
        response = self._base_result(reply=reply, intent=intent, tool_calls=tool_calls)
        response.update({
            "disposition": Disposition.HANDOFF_HUMAN.value,
            "needs_human": True,
            "moderation_id": f"mod_{uuid.uuid4().hex}",
            "handoff_reason": reason,
        })
        return response

    def _handle_logistics(self, text: str) -> dict:
        """确定性处理查件，避免模型猜测核验信息或编造相对日期。"""
        order_id = _extract_order_id(text)
        phone_last4 = _extract_phone_last4(text)
        args = {"order_id": order_id, "phone_last4": phone_last4}
        if not order_id:
            return self._base_result(
                intent="logistics_query",
                reply="请提供演示订单号和收货手机号后四位，我核验后帮您查询物流。",
            )

        result = self.tools.logistics_lookup(order_id, phone_last4)
        failed = result is None or (isinstance(result, dict) and bool(result.get("error")))
        tool_call = {
            "name": "logistics_lookup",
            "status": "error" if failed else "ok",
            "args": args,
            "result": result,
        }
        if result is None:
            response = self._base_result(
                intent="logistics_query",
                reply="暂未查询到该订单，已提交人工专员复核。",
                tool_calls=[tool_call],
            )
            response.update({
                "disposition": Disposition.HANDOFF_HUMAN.value,
                "needs_human": True,
                "moderation_id": f"mod_{uuid.uuid4().hex}",
                "handoff_reason": "order_not_found",
            })
            return response
        if result.get("error") == "verification_required":
            return self._base_result(
                intent="logistics_query",
                reply="为保护订单信息，请再提供收货手机号后四位进行核验。",
                tool_calls=[tool_call],
            )
        if result.get("error") == "verify_failed":
            response = self._base_result(
                intent="logistics_query",
                reply="订单号与手机号后四位核验不一致，已提交人工专员复核。",
                tool_calls=[tool_call],
            )
            response.update({
                "disposition": Disposition.HANDOFF_HUMAN.value,
                "needs_human": True,
                "moderation_id": f"mod_{uuid.uuid4().hex}",
                "handoff_reason": "verify_failed",
            })
            return response

        trace = result.get("trace") or []
        latest = trace[-1] if trace else {"time": "", "desc": result.get("latest_event", "暂无物流节点")}
        now = datetime.now()
        stale_hours = 0.0
        try:
            stale_hours = (now - datetime.strptime(latest["time"], "%Y-%m-%d %H:%M")).total_seconds() / 3600
        except (KeyError, TypeError, ValueError):
            pass
        eta_dates = re.findall(r"\d{4}-\d{2}-\d{2}", result.get("eta", ""))
        eta_expired = bool(eta_dates and now.date() > date.fromisoformat(eta_dates[-1]))
        delivered = "签收" in result.get("status", "") or "签收" in latest.get("desc", "")

        lines = [
            f"订单 {order_id} 当前状态：{result.get('status', '查询中')}。",
            f"最新记录：{latest.get('time', '')}，{latest.get('desc', '')}。",
        ]
        if result.get("eta"):
            lines.append(f"原预计时段：{result['eta']}。")
        reported_missing = delivered and any(
            phrase in text for phrase in ("没收到", "未收到", "找不到", "不是我签收")
        )
        needs_human = (stale_hours >= 72 and not delivered) or reported_missing
        if reported_missing:
            lines.append("物流显示签收但您未收到，请先核对家人、物业和驿站；已同时提交人工复核签收凭证。")
        elif needs_human:
            lines.append("物流已连续超过 72 小时未更新，已提交人工专员复核。")
        elif eta_expired and not delivered:
            lines.append("该预计时段已经过去；如仍未收到，请联系承运方确认，或由人工客服继续跟进。")
        elif not delivered:
            lines.append("请以承运方最新扫描记录为准，并留意派件联系。")

        response = self._base_result(
            intent="logistics_query",
            reply="\n".join(lines),
            tool_calls=[tool_call],
        )
        if needs_human:
            response.update({
                "disposition": Disposition.HANDOFF_HUMAN.value,
                "needs_human": True,
                "moderation_id": f"mod_{uuid.uuid4().hex}",
                "handoff_reason": "signed_not_received" if reported_missing else "logistics_stale_72h",
            })
        return response

    def _promotion_reply(self, text: str) -> dict | None:
        compact_text = re.sub(r"\s+", "", text)
        if not (
            "西装" in compact_text
            and "阔腿裤" in compact_text
            and "95折" in compact_text
            and "满减" in compact_text
        ):
            return None
        jacket = next((p for p in self.tools.products if "西装" in p.get("name", "")), None)
        trousers = next((p for p in self.tools.products if "阔腿裤" in p.get("name", "")), None)
        if not jacket or not trousers:
            return None
        subtotal = jacket["price_cents"] + trousers["price_cents"]
        after_discount = (subtotal * 95 + 50) // 100
        coupon = 6000 if after_discount >= 49900 else 3000 if after_discount >= 29900 else 0
        payable = after_discount - coupon
        calculation = {
            "items": [jacket["sku"], trousers["sku"]],
            "subtotal_cents": subtotal,
            "discount_95_cents": subtotal - after_discount,
            "coupon_cents": coupon,
            "payable_cents": payable,
        }
        reply = (
            f"短款西装外套（{jacket['sku']}）¥{jacket['price_cents'] / 100:.2f} + "
            f"垂感高腰阔腿裤（{trousers['sku']}）¥{trousers['price_cents'] / 100:.2f}，"
            f"原价合计 ¥{subtotal / 100:.2f}。\n"
            f"两件 95 折后是 ¥{after_discount / 100:.2f}，再用满 ¥499 减 ¥60 店铺券，"
            f"到手价为 ¥{payable / 100:.2f}（未计积分抵扣）。"
        )
        return self._base_result(
            intent="promotion_calculation",
            reply=reply,
            tool_calls=[{
                "name": "promotion_calculate",
                "status": "ok",
                "args": {"query": text},
                "result": calculation,
            }],
        )

    def _size_reply(self, text: str) -> dict | None:
        if "阔腿裤" not in text or "码" not in text:
            return None
        size_match = re.search(r"(XL|S|M|L)\s*码", text, re.IGNORECASE)
        if not size_match:
            return None
        size = size_match.group(1).upper()
        trousers = next((p for p in self.tools.products if "阔腿裤" in p.get("name", "")), None)
        if not trousers or size not in trousers.get("waist_cm", {}):
            return None
        waist_match = re.search(r"腰围\s*(\d{2,3})", text)
        hip_match = re.search(r"臀围\s*(\d{2,3})", text)
        waist = int(waist_match.group(1)) if waist_match else None
        hip = int(hip_match.group(1)) if hip_match else None
        garment_hip = trousers.get("hip_cm", {}).get(size)
        facts = (
            f"{trousers['name']}（{trousers['sku']}）{size} 码腰围 "
            f"{trousers['waist_cm'][size]} cm、成衣臀围 {garment_hip} cm。"
        )
        if waist is None or hip is None:
            advice = "请再提供腰围和臀围；这款腰臀差较大时要优先按臀围选择。"
        else:
            ease = garment_hip - hip if isinstance(garment_hip, (int, float)) else None
            if ease is not None and ease <= 1:
                advice = (
                    f"您的腰围 {waist} cm 在该码范围内，但臀围 {hip} cm 与成衣臀围只差 {ease:g} cm，"
                    f"{size} 码可能较贴并影响阔腿裤垂感；建议对比更宽松尺码或版型，"
                    "并以实际试穿为准，不能直接保证合身。"
                )
            else:
                advice = "从现有数据看可作为参考尺码，但仍需结合松紧偏好并以实际试穿为准。"
        return self._base_result(
            intent="size_recommendation",
            reply=facts + advice,
            tool_calls=[{
                "name": "product_lookup",
                "status": "ok",
                "args": {"sku": trousers["sku"]},
                "result": [trousers],
            }],
        )

    def _white_shirt_reply(self, text: str) -> dict | None:
        if not ("白衬衫" in text or ("白" in text and "衬衫" in text)):
            return None
        shirt = next((p for p in self.tools.products if "衬衫" in p.get("name", "")), None)
        if not shirt:
            return None
        reply = (
            f"现有白色衬衫是 {shirt['name']}（{shirt['sku']}，¥{shirt['price_cents'] / 100:.2f}）。"
            f"{shirt['tips']}，因此暂时没有可以承诺完全不透的白衬衫；更在意防透时可搭配肤色打底。"
        )
        return self._base_result(
            intent="product_query",
            reply=reply,
            tool_calls=[{
                "name": "product_lookup",
                "status": "ok",
                "args": {"sku": shirt["sku"]},
                "result": [shirt],
            }],
        )

    def _handle_order_action(self, text: str) -> dict | None:
        """确定性处理取消订单/改地址，核验后仍必须进入人工审批。"""
        is_cancel = any(word in text for word in ("取消订单", "取消这单", "不想要了", "不要了"))
        is_modify = any(word in text for word in ("改地址", "修改地址", "换地址", "改收货信息"))
        if not (is_cancel or is_modify):
            return None
        intent = "cancel_order" if is_cancel else "address_change"
        order_id = _extract_order_id(text)
        phone_last4 = _extract_phone_last4(text)
        if not order_id:
            return self._base_result(
                intent=intent,
                reply="请提供演示订单号和收货手机号后四位，我先核验订单状态。",
            )
        order = self.tools.order_lookup(order_id, phone_last4)
        lookup_call = {
            "name": "order_lookup",
            "status": "error" if order and order.get("error") else "ok",
            "args": {"order_id": order_id, "phone_last4": phone_last4},
            "result": order,
        }
        if order and order.get("error") == "verification_required":
            return self._base_result(
                intent=intent,
                reply="为保护订单信息，请再提供收货手机号后四位进行核验。",
                tool_calls=[lookup_call],
            )
        if not order or order.get("error") == "verify_failed":
            return self._handoff_result(
                intent=intent,
                reply="订单号与手机号后四位核验不一致，已提交人工专员复核。",
                reason="verify_failed",
                tool_calls=[lookup_call],
            )

        if is_modify:
            if "已发" in order.get("status", "") or "签收" in order.get("status", ""):
                return self._handoff_result(
                    intent=intent,
                    reply="该订单已经发货，不能直接修改地址；已转人工协助联系承运方或评估拦截。",
                    reason="shipped_address_change",
                    tool_calls=[lookup_call],
                )
            response = self._base_result(
                intent=intent,
                reply=(
                    "订单仍处于待发货状态，可以提交改址申请。为保护隐私，请不要在演示聊天中发送完整真实地址；"
                    "已生成待审批申请，由人工在平台安全流程中处理。"
                ),
                tool_calls=[lookup_call],
            )
            response.update({
                "disposition": Disposition.REQUIRE_APPROVAL.value,
                "needs_human": True,
                "moderation_id": f"mod_{uuid.uuid4().hex}",
                "handoff_reason": "address_change_approval",
            })
            return response

        cancellation = self.tools.cancel_order(order_id, phone_last4)
        cancel_call = {
            "name": "cancel_order",
            "status": "ok" if cancellation.get("ok") else "error",
            "args": {"order_id": order_id, "phone_last4": phone_last4},
            "result": cancellation,
        }
        if not cancellation.get("ok"):
            return self._handoff_result(
                intent=intent,
                reply="订单已经进入发货环节，无法直接取消；已转人工评估快递拦截或到货后退货。",
                reason="shipped_cannot_cancel",
                tool_calls=[lookup_call, cancel_call],
            )
        response = self._base_result(
            intent=intent,
            reply="订单仍未发货，已生成取消订单申请；该操作需要人工审批，仓库锁单后仍可能取消失败。",
            tool_calls=[lookup_call, cancel_call],
        )
        response.update({
            "disposition": Disposition.REQUIRE_APPROVAL.value,
            "needs_human": True,
            "moderation_id": f"mod_{uuid.uuid4().hex}",
            "handoff_reason": "cancel_order_approval",
        })
        return response

    def _commerce_rule_reply(self, text: str) -> dict | None:
        """覆盖真实网购中的高频规则问题与支付安全异常。"""
        compact = re.sub(r"\s+", "", text)
        if "发票" in text:
            return self._base_result(
                intent="invoice_query",
                reply="支持电子普通发票，可开个人或企业抬头；订单完成后 1～3 个工作日发送。请在平台订单页填写抬头和税号，不要在聊天中发送敏感开票资料。",
            )
        if "包邮" in text or "运费" in text:
            return self._base_result(
                intent="shipping_fee_query",
                reply="单笔实付满 ¥99 包邮，未满 ¥99 收取 ¥8 运费；新疆、西藏等偏远地区以结算页实际显示为准。",
            )
        if ("优惠券" in text and "新客券" in text) or "券不能用" in text or "优惠券用不了" in text:
            return self._base_result(
                intent="coupon_query",
                reply="店铺券与新客券不能叠加，每单只能选其中一张；指定商品的两件 95 折可以与其中一张券叠加，积分在券后抵扣。若仍不可用，请核对门槛、适用商品和有效期。",
            )
        if "预售" in text and any(word in text for word in ("急用", "提前", "来得及", "赶得上")):
            product = next((p for p in self.tools.products if p.get("preorder")), None)
            eta = product.get("ship_eta", "以商品页为准") if product else "以商品页为准"
            return self._base_result(
                intent="preorder_urgent",
                reply=f"预售商品预计 {eta}。预售批次不能承诺提前发货；如果有明确使用日期，建议改选现货款。",
            )
        if ("积分" in text or "成长值" in text) and any(word in text for word in ("过期", "有效期", "多久")):
            return self._base_result(
                intent="points_policy",
                reply="会员积分自获得之日起 365 天有效；实付 ¥1 得 1 积分，100 积分抵 ¥1，每单最高抵扣实付金额的 10%。",
            )
        if any(word in text for word in ("包装破损", "箱子破了", "快递袋破了", "外包装破")):
            return self._base_result(
                intent="package_damage",
                reply="请先拍摄外包装、面单和商品整体留存。若商品完好可正常保留；若商品也受损，请补充问题部位和洗标照片，按质量问题提交审核。",
            )
        if any(word in text for word in ("开线", "破洞", "严重起球", "严重掉色", "拉链坏", "质量问题")):
            return self._base_result(
                intent="quality_issue",
                reply="很抱歉影响使用。请提供商品整体、问题部位和洗标照片；核实后可审核退货退款、换货或补发，在证据确认前客服不能承诺赔偿金额。",
            )
        if any(word in text for word in ("发错", "错发", "少发", "漏发", "少一件", "少了")):
            return self._base_result(
                intent="wrong_or_missing_item",
                reply="请在签收后 48 小时内提供外包装面单、开箱情况和收到的实物照片。核实为错发或少件后，可安排补发或退款。",
            )
        if any(word in text for word in ("换货", "换个尺码", "换颜色", "换同款", "换大一码", "换小一码")):
            return self._base_result(
                intent="exchange_query",
                reply="支持同款更换颜色或尺码，需目标规格有库存。仓库收到原商品并验收后，通常 1～3 个工作日发出换货；请保持商品未穿洗且不影响二次销售。",
            )
        if "价保" in text or "保价" in text or "补差价" in text:
            return self._base_result(
                intent="price_protection",
                reply="活动期内，同款同色同尺码发生店铺直接降价，可在签收后 7 天内申请补差；秒杀、直播间赠品和平台红包不参与价保。",
            )
        if "穿过" in text and "退" in text:
            return self._base_result(
                intent="return_policy",
                reply="七天无理由要求商品未穿洗、无污渍异味、吊牌和包装完整且不影响二次销售。已经穿着一天通常不符合条件；若存在质量问题，可按质量售后提交照片审核。",
            )
        if "退款" in text and any(word in text for word in ("多久", "几天", "什么时候")):
            return self._base_result(
                intent="refund_timeline",
                reply="退件到仓后通常 48 小时内验收；验收通过后原路退款，到账一般需要 1～5 个工作日。超过 5 个工作日仍未到账会转人工复核。",
            )
        if any(word in text for word in ("过敏", "受伤", "烫伤", "划伤", "身体不适")):
            return self._handoff_result(
                intent="personal_injury",
                reply="请立即停止使用该商品；如症状明显或持续，请优先及时就医。已按紧急情况转人工专员登记商品批次和售后信息，客服不会远程诊断或提前承诺赔偿金额。",
                reason="personal_injury",
            )
        if any(word in text for word in ("假货", "不是正品", "真假", "冒牌")):
            return self._handoff_result(
                intent="counterfeit_dispute",
                reply="已记录您的商品真实性疑问并转人工复核。请保留商品、吊牌、包装和订单凭证；在完成批次及质检记录核查前，客服不会未经核实直接下结论。",
                reason="counterfeit_dispute",
            )
        security_loss = any(word in text for word in ("已经提供", "已经给了", "已经转账", "被骗了", "钱被骗", "账户异常"))
        if security_loss and any(word in text for word in ("验证码", "支付密码", "冒充客服", "陌生账户", "二维码")):
            return self._handoff_result(
                intent="account_security_risk",
                reply="请立即停止继续操作，并通过平台与银行官方入口处理；如已转账或泄露验证码，请尽快联系银行冻结风险。已按紧急情况转人工专员跟进。",
                reason="account_security_risk",
            )
        if any(word in text for word in ("验证码", "支付密码", "银行卡密码")):
            return self._base_result(
                intent="payment_security",
                reply="请不要向任何客服提供验证码、支付密码或银行卡密码。正规客服不会索要这些信息；如已提供，请立即联系银行和平台官方客服处理。",
            )
        if any(word in text for word in ("重复扣款", "扣了两次", "重复支付")):
            return self._handoff_result(
                intent="duplicate_charge",
                reply="请不要发送银行卡号或验证码。请先核对平台订单与支付账单，已为您转人工核查是否为重复扣款或预授权占用。",
                reason="duplicate_charge",
            )
        if any(word in text for word in ("付款失败", "支付失败", "付不了", "无法付款")):
            return self._base_result(
                intent="payment_failed",
                reply="请先确认网络、支付限额和订单是否已生成，再通过平台收银台重试或更换官方支付方式。不要扫码私下转账；若已扣款但订单未生成，请保留平台支付记录并联系人工核查。",
            )
        if any(word in compact.lower() for word in ("加微信", "私下转账", "线下付款", "vx")):
            return self._base_result(
                intent="off_platform_contact",
                reply="为保护交易和售后权益，请只通过平台聊天与收银台处理，不添加私人联系方式，也不要进行平台外转账。",
            )
        return None

    def _rule_reply(self, text: str) -> tuple[str, list[dict]]:
        """无 LLM 或 LLM 暂时失败时的可用降级回复。"""
        if any(word in text for word in ("你好", "您好", "在吗")):
            return "您好，我是栀夏女装客服小栀 🌿 想选衣服、问尺码，还是查询订单/售后呢？", []
        if detect_topic(text) == "product":
            products = self.tools.search_products(text, top_k=3) or self.tools.products[:3]
            lines = [
                f"{p['name']}（{p['sku']}）¥{p['price_cents'] / 100:.2f}：{p.get('detail', '')}"
                for p in products[:3]
            ]
            return "按您的需求，优先可看：\n" + "\n".join(lines), [
                {"name": "product_lookup", "status": "ok", "args": {"sku": ""}, "result": products[:3]}
            ]
        if "会员" in text or "积分" in text:
            return "请提供演示会员绑定手机号后四位，我核验后帮您查询等级和积分。", []
        if "穿过" in text and "退" in text:
            return "七天无理由要求商品未穿洗、吊牌和包装完整且不影响二次销售。已经穿着一天通常不符合条件；若存在质量问题，请提供商品整体、问题部位和洗标照片后申请审核。", []
        return "我暂时无法准确确认这个问题，已为您保留当前消息，请稍后重试或转人工客服处理。", []

    async def handle(self, *, text: str, history: list[dict[str, str]] | None = None) -> dict:
        response = await self._handle_once(text=text, history=history)
        return self._attach_human_prompt(response)

    async def _handle_once(self, *, text: str, history: list[dict[str, str]] | None = None) -> dict:
        history = history or []
        # 1. 入站护栏（注入检测）
        inbound = check_inbound(text)
        if not inbound.ok:
            return {
                "intent": "security_rejected", "tone": "normal",
                "disposition": Disposition.REJECT.value,
                "needs_human": False, "reply": "抱歉，这个请求我无法处理。请咨询商品、订单或售后问题。",
                "tool_calls": [], "moderation_id": None,
                "safety": inbound.to_dict(),
            }

        if inbound.redacted and inbound.redacted != text:
            response = self._base_result(
                intent="pii_protection",
                reply=(
                    "检测到您发送了手机号、证件号、银行卡号、验证码或个人联系方式等敏感信息，"
                    "系统已脱敏且不会用于回复。请勿继续发送完整信息；订单核验只需要手机号后四位。"
                ),
            )
            response["safety"] = {
                "ok": True,
                "reason_code": "PII_REDACTED",
                "detail": "敏感信息已在进入会话存储和模型前脱敏",
            }
            return response

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
            reason = "refund_overdue" if handoff_reason == "退款超期" else handoff_reason
            return {
                "intent": "handoff_human", "tone": tone.value,
                "disposition": Disposition.HANDOFF_HUMAN.value,
                "needs_human": True, "reply": "这个问题需要人工进一步核实，已为您创建服务单。",
                "tool_calls": [], "moderation_id": f"mod_{uuid.uuid4().hex}",
                "handoff_reason": reason,
            }

        # 4. 写操作、支付/规则问题和物流均走确定性链路
        order_action = self._handle_order_action(text)
        if order_action:
            return order_action

        commerce_reply = self._commerce_rule_reply(text)
        if commerce_reply:
            return commerce_reply

        if any(word in text for word in _LOGISTICS_WORDS):
            return self._handle_logistics(text)

        # 5. 常见优惠计算走确定性规则，避免金额误算
        promotion = self._promotion_reply(text)
        if promotion:
            return promotion

        size_advice = self._size_reply(text)
        if size_advice:
            return size_advice

        white_shirt = self._white_shirt_reply(text)
        if white_shirt:
            return white_shirt

        # 6. LLM 回复（历史先做话题裁剪，避免旧话题污染）
        if self.llm_agent is not None:
            try:
                llm_history = self._build_llm_history(text, history)
                is_first = len(history) == 0
                trusted_context = None
                topic = detect_topic(text)
                if topic == "product":
                    trusted_context = {
                        "products": self.tools.products,
                        "shop_rules": self.tools.shop_rules,
                    }
                elif topic in {"order", "aftersale", "member", "logistics"}:
                    trusted_context = {"shop_rules": self.tools.shop_rules}
                result = await self.llm_agent.run(
                    message_text=text, history=llm_history,
                    tool_executor=self._tool_executor(text),
                    is_first_turn=is_first,
                    trusted_context=trusted_context,
                )
                reply = result["reply"]
                tool_calls = result["tool_calls"]
            except Exception:  # noqa: BLE001
                # 不泄露内部错误；回落到确定性规则，保持演示可用
                reply, tool_calls = self._rule_reply(text)
        else:
            reply, tool_calls = self._rule_reply(text)

        # 7. 出站护栏
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
