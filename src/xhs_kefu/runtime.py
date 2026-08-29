"""小红书千帆客服 Agent —— 运行时。

串联：去重 → 短时记忆 → 意图规划 → 工具查事实 → 风控 → 写操作审批
→ 生成回复 → 回执。忠实还原参考架构 AgentRuntime 的职责与安全边界。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from .domain import (
    ActionState,
    DecisionPlan,
    IncomingMessage,
    Intent,
    PolicyOutcome,
    TraceStep,
)
from .fixtures import Fixtures
from .decision import Decision, Disposition, decide, detect_write_intent, is_explicit_handoff
from .llm_agent import LLMAgent
from .planner import Planner, build_planner, extract_amount_cents, extract_order_ids
from .policy import PolicyEngine
from .safety import check_inbound, check_outbound, should_require_approval
from .storage import SQLiteStore
from .tools import CommerceTools


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentRuntime:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        fixtures: Fixtures,
        policy: PolicyEngine,
        planner: Planner,
        store_id: str = "STORE-001",
        tenant_id: str = "demo",
        max_history_turns: int = 8,
        llm_agent: LLMAgent | None = None,
        intent_classifier=None,
    ) -> None:
        self.store = store
        self.fixtures = fixtures
        self.policy = policy
        self.planner = planner
        self.store_id = store_id
        self.tenant_id = tenant_id
        self.max_history_turns = max_history_turns
        self.tools = CommerceTools(fixtures)
        self.llm_agent = llm_agent
        self.intent_classifier = intent_classifier
        self._locks: defaultdict = defaultdict(asyncio.Lock)

    async def handle_message(self, message: IncomingMessage) -> dict[str, Any]:
        """处理一条顾客消息，返回带完整链路追踪的决策结果。"""
        lock = self._locks[message.session_key]
        async with lock:
            # 1. 去重
            prior = self.store.dedupe_hit(message.dedupe_key)
            trace_id = f"trace_{uuid.uuid4().hex}"
            if prior is not None:
                return {
                    "deduplicated": True,
                    "trace_id": trace_id,
                    "intent": None,
                    "status": "deduplicated",
                    "reply": prior.get("content", ""),
                    "tool_calls": [],
                    "policy": None,
                    "pending_action": None,
                    "facts_used": [],
                }

            steps: list[TraceStep] = [TraceStep("runtime", "message_received", "ok")]
            history = self.store.recent_turns(message.session_key, self.max_history_turns)

            # 1.5 接管检查：人工接管中的会话不自动回复（最高优先级）
            handoff = self.store.get_handoff(message.session_key)
            if handoff and handoff.get("state") == "human_active":
                steps.append(TraceStep("handoff", "human_active", "parked"))
                return {
                    "trace_id": trace_id,
                    "session_key": message.session_key,
                    "intent": None,
                    "status": "taken_over",
                    "reply": "",
                    "tool_calls": [],
                    "policy": None,
                    "pending_action": None,
                    "facts_used": [],
                    "needs_approval": False,
                }

            # 1.6 意图判断：LLM 分类（理解语义）+ 确定性规则安全兜底
            intent_result = None
            if self.intent_classifier is not None:
                try:
                    intent_result = await self.intent_classifier.classify(message.text)
                    steps.append(TraceStep(
                        "intent", "llm_classify", intent_result.intent.value,
                        {"confidence": round(intent_result.confidence, 2), "needs_human": intent_result.needs_human},
                    ))
                except Exception as e:  # noqa: BLE001
                    steps.append(TraceStep("intent", "llm_classify", f"error:{type(e).__name__}"))

            decision = decide(message.text)
            steps.append(TraceStep("decision", "rules_fallback", decision.disposition.value, {"reason": decision.reason_code, "tone": decision.tone.value}))
            tone = decision.tone.value

            # 融合：确定性规则 REJECT 优先级最高；LLM 判需人工与规则结果取最严
            if decision.disposition == Disposition.REJECT:
                final_disposition = Disposition.REJECT
            elif intent_result is not None and intent_result.needs_human:
                # LLM 判定需人工：投诉→接管，写操作→审批
                final_disposition = (
                    Disposition.HANDOFF_HUMAN if intent_result.intent.value == "complaint"
                    else Disposition.REQUIRE_APPROVAL
                )
            else:
                final_disposition = decision.disposition

            if final_disposition == Disposition.REJECT:
                response = {
                    "trace_id": trace_id,
                    "session_key": message.session_key,
                    "intent": "security_rejected",
                    "status": "rejected",
                    "reply": "抱歉，这个请求我无法处理。请咨询订单、物流、商品或售后相关问题。",
                    "tool_calls": [],
                    "policy": None,
                    "pending_action": None,
                    "facts_used": [],
                    "needs_approval": False,
                    "disposition": Disposition.REJECT.value,
                    "tone": tone,
                }
                self._persist(message, response, trace_id)
                return response

            if final_disposition == Disposition.HANDOFF_HUMAN:
                # 转人工：入待审队列 + 弹提醒。
                # 只有顾客「明确要求转人工」才设置会话级接管锁；投诉/情绪只本条转人工，
                # 不锁会话，后续普通消息仍自动回复。
                reason = decision.detail or (intent_result.reason if intent_result else "handoff")
                if is_explicit_handoff(message.text):
                    self.store.set_handoff(message.session_key, "human_active", reason, _iso_now())
                mid = f"mod_{uuid.uuid4().hex}"
                self.store.add_moderation(
                    id=mid, session_key=message.session_key, customer_id=message.customer_id,
                    kind="handoff", content=message.text, intent="handoff_human",
                    reason_code=reason, created_at=_iso_now(), tenant_id=message.tenant_id,
                    store_id=message.store_id, channel=message.channel,
                )
                steps.append(TraceStep("handoff", "handoff_human", reason))
                response = {
                    "trace_id": trace_id,
                    "session_key": message.session_key,
                    "intent": "handoff_human",
                    "status": "taken_over",
                    "reply": "",
                    "tool_calls": [],
                    "policy": None,
                    "pending_action": None,
                    "facts_used": [],
                    "needs_approval": True,
                    "moderation_id": mid,
                    "disposition": Disposition.HANDOFF_HUMAN.value,
                    "tone": tone,
                }
                self._persist(message, response, trace_id)
                return response

            # 2. LLM 路径：完整 Agent Loop（理解 → 工具查事实 → 生成回复）
            if self.llm_agent is not None:
                try:
                    response = await self._handle_llm(message, history, steps, trace_id)
                except Exception as e:  # noqa: BLE001
                    steps.append(TraceStep("llm", "agent_loop", f"error:{type(e).__name__}"))
                    # LLM 失败降级到规则路径
                    response = await self._handle_rules(message, history, steps, trace_id)
            else:
                response = await self._handle_rules(message, history, steps, trace_id)

            # 2.5 安全护栏 + 分级发送：高风险写操作 / 敏感回复 → 待审队列，不自动发
            response = self._apply_safety_gate(message, response, steps)
            response["disposition"] = (
                Disposition.REQUIRE_APPROVAL.value if response.get("needs_approval")
                else Disposition.AUTO_REPLY.value
            )
            response["tone"] = tone  # 语气结论，供审批台展示

            # 3. 持久化
            self._persist(message, response, trace_id)
            return response

    def _persist(self, message: IncomingMessage, response: dict[str, Any], trace_id: str) -> None:
        """持久化会话与决策（抽出的公共逻辑）。"""
        self.store.save_turn(
            dedupe_key=message.dedupe_key,
            session_key=message.session_key,
            role="user",
            content=message.text,
            created_at=_iso_now(),
        )
        if response["reply"]:
            ag_dedupe = f"{message.dedupe_key}|assistant"
            self.store.save_turn(
                dedupe_key=ag_dedupe,
                session_key=message.session_key,
                role="assistant",
                content=response["reply"],
                created_at=_iso_now(),
            )
        self.store.save_decision(
            session_key=message.session_key,
            trace_id=trace_id,
            decision=response,
            created_at=_iso_now(),
        )

    async def _handle_rules(
        self, message: IncomingMessage, history: list[dict[str, str]], steps: list[TraceStep], trace_id: str
    ) -> dict[str, Any]:
        """规则/降级路径：意图规划 → 硬编码执行。"""
        started = time.perf_counter()
        plan = await self.planner.plan(message, history)
        steps.append(
            TraceStep(
                "planner",
                "intent_classification",
                "ok",
                {"intent": plan.intent.value},
                round((time.perf_counter() - started) * 1000),
            )
        )
        return self._execute_plan(message, plan, steps, trace_id)

    async def _handle_llm(
        self, message: IncomingMessage, history: list[dict[str, str]], steps: list[TraceStep], trace_id: str
    ) -> dict[str, Any]:
        """LLM 路径：完整 Agent Loop，回复由 DeepSeek 结合上下文生成。"""
        # 入站护栏：提示词注入检测
        inbound_check = check_inbound(message.text)
        if not inbound_check.ok:
            steps.append(TraceStep("safety", "inbound_rejected", inbound_check.reason_code))
            return {
                "trace_id": trace_id,
                "session_key": message.session_key,
                "intent": "security_rejected",
                "status": "rejected",
                "reply": "抱歉，这个请求我无法处理。请咨询订单、物流、商品或售后相关问题。",
                "tool_calls": [],
                "policy": None,
                "pending_action": None,
                "facts_used": [],
                "deduplicated": False,
            }

        started = time.perf_counter()
        result = await self.llm_agent.run(
            message_text=message.text,
            history=history,
            tool_executor=self._tool_executor(message),
        )
        steps.append(
            TraceStep(
                "llm",
                "agent_loop",
                "ok",
                {"intent": result.intent or "unknown", "tool_calls": len(result.tool_calls)},
                round((time.perf_counter() - started) * 1000),
            )
        )
        for tc in result.tool_calls:
            steps.append(TraceStep("tool", tc["name"], tc["status"], {"args": tc["args"]}, 0))
        return {
            "trace_id": trace_id,
            "session_key": message.session_key,
            "intent": result.intent or "unknown",
            "status": "resolved",
            "reply": result.reply,
            "tool_calls": result.tool_calls,
            "policy": None,
            "pending_action": None,
            "facts_used": [],
            "deduplicated": False,
        }

    def _tool_executor(self, message: IncomingMessage):
        """包一层工具执行器，供 LLM Agent 调用，注入可信上下文 + 后端校验。"""

        def execute(name: str, args: dict) -> Any:
            tenant_id = message.tenant_id
            store_id = message.store_id
            customer_id = message.customer_id
            if name == "product_lookup":
                sku = args.get("sku")
                return self.tools.product_lookup(
                    tenant_id=tenant_id, store_id=store_id, sku=sku
                )
            if name == "order_lookup":
                order_id = args.get("order_id")
                return self.tools.order_lookup(
                    tenant_id=tenant_id, store_id=store_id,
                    customer_id=customer_id, order_id=order_id,
                )
            if name == "logistics_lookup":
                order_id = args.get("order_id")
                return self.tools.logistics_lookup(order_id=order_id) if order_id else {"error": "missing order_id"}
            # 写操作：沙箱处理，返回"需人工审批"提示，不让模型直接执行
            if name == "modify_address":
                return {
                    "note": "修改地址为高风险写操作，需人工审批。已记录申请。",
                    "new_address": args.get("new_address"),
                    "receiver_name": args.get("receiver_name", ""),
                }
            if name == "intercept_express":
                return {
                    "note": "快递拦截为高风险写操作，需人工审批。已记录申请。",
                    "order_id": args.get("order_id"),
                }
            if name == "compensate":
                return {
                    "note": f"补偿 ¥{(args.get('amount_cents') or 0)/100:.2f} 需风控校验与人工审批。",
                    "order_id": args.get("order_id"),
                    "amount_cents": args.get("amount_cents"),
                    "reason": args.get("reason"),
                }
            return {"error": f"unknown tool: {name}"}

        return execute

    def _apply_safety_gate(
        self, message: IncomingMessage, response: dict[str, Any], steps: list[TraceStep]
    ) -> dict[str, Any]:
        """对决策结果施加安全护栏 + 分级发送。

        - 普通咨询：needs_approval=False，Worker 可自动发送；
        - 高风险写操作 / 包含敏感信息 / 内容校验不过：needs_approval=True，
          进入待审队列，Worker 不自动发送，等待人工审批。

        写操作判定不依赖 LLM 是否调用工具——用 decision.detect_write_intent
        从顾客原始消息兜底检测，命中即强制转人工审批。
        """
        reply = response.get("reply", "")
        intent = response.get("intent")

        # 出站内容校验 + 高风险意图判定（should_require_approval 内部已含 check_outbound）
        out_check = check_outbound(reply)
        needs_approval, reason = should_require_approval(intent, reply)

        # 兜底：基于顾客原始消息检测写操作意图（退款/赔偿/改址/拦截等）
        if not needs_approval:
            write_reason = detect_write_intent(message.text)
            if write_reason:
                needs_approval = True
                reason = write_reason
                intent = intent or "write_action"

        # 售后风控兜底：补偿/少发/退款类，用 AftersalePolicyEngine 校正金额与话术
        if needs_approval and reason in ("COMPENSATION_REQUEST", "REFUND_REQUEST", "MISSING_ITEM_REQUEST"):
            corrected = self._apply_aftersale_policy(message, response, steps)
            if corrected:
                reply = corrected  # 用风控引擎校正后的话术替换 LLM 可能说错的

        response["needs_approval"] = needs_approval
        response["safety"] = out_check.to_dict()

        if needs_approval:
            mid = f"mod_{uuid.uuid4().hex}"
            self.store.add_moderation(
                id=mid,
                session_key=message.session_key,
                customer_id=message.customer_id,
                kind="reply" if reply else "action",
                content=reply or message.text,
                intent=intent,
                reason_code=reason,
                created_at=_iso_now(),
                tenant_id=message.tenant_id,
                store_id=message.store_id,
                channel=message.channel,
            )
            response["moderation_id"] = mid
            response["status"] = "pending_approval"
            response["intent"] = intent
            steps.append(TraceStep("safety", "require_approval", reason))
        else:
            steps.append(TraceStep("safety", "auto_send_allowed", "ok"))
        return response

    def _apply_aftersale_policy(
        self, message: IncomingMessage, response: dict[str, Any], steps: list[TraceStep]
    ) -> str | None:
        """用售后风控引擎校正补偿/少发/退款的金额与话术。

        返回校正后应展示给人工审批的话术；若无需校正返回 None。
        """
        from .aftersale_policy import AftersalePolicyEngine
        engine = AftersalePolicyEngine()
        issue_type = engine.classify_issue(message.text)
        has_evidence = bool(message.attachments)
        order_id = extract_order_ids(message.text)
        order_id = order_id[0] if order_id else None

        order = None
        if order_id:
            orders = self.tools.order_lookup(
                tenant_id=message.tenant_id, store_id=message.store_id,
                customer_id=message.customer_id, order_id=order_id,
            )
            if orders:
                order = orders[0]

        decision = engine.evaluate(
            order=order, issue_type=issue_type, user_text=message.text,
            has_evidence=has_evidence, upgrade_compensation=False,
        )

        # 把风控决策写成给人工审批看的"事实 + 正确方案"
        order_info = ""
        if order:
            order_info = (
                f"\n[订单事实] {order['order_id']} 实付 ¥{order.get('paid_amount_cents',0)/100:.2f}，"
                f"状态 {order.get('status','')}"
            )
        steps.append(TraceStep("safety", "aftersale_policy", decision.verdict.value, {"amount": decision.amount_cents}))
        return f"{decision.message}{order_info}"

    def approve_moderation(self, mod_id: str) -> dict:
        """人工审批通过一条待审项，返回其内容（供发送）。"""
        mod = self.store.get_moderation(mod_id)
        if mod is None:
            return {"ok": False, "error": "moderation_not_found"}
        self.store.update_moderation_status(mod_id, "approved")
        return {
            "ok": True,
            "id": mod_id,
            "kind": mod["kind"],
            "content": mod["content"],
            "customer_id": mod["customer_id"],
            "session_key": mod["session_key"],
            "tenant_id": mod.get("tenant_id", "demo"),
            "store_id": mod.get("store_id", "STORE-001"),
            "channel": mod.get("channel", "xhs_qianfan_desktop"),
        }

    def reject_moderation(self, mod_id: str) -> dict:
        mod = self.store.get_moderation(mod_id)
        if mod is None:
            return {"ok": False, "error": "moderation_not_found"}
        self.store.update_moderation_status(mod_id, "rejected")
        return {"ok": True, "id": mod_id}

    def list_moderation(self, status: str | None = None) -> list[dict]:
        return self.store.list_moderation(status)

    def take_over(self, session_key: str, reason: str = "operator_takeover") -> dict:
        """人工接管指定会话：后续消息不再自动回复。"""
        self.store.set_handoff(session_key, "human_active", reason, _iso_now())
        return {"ok": True, "session_key": session_key, "state": "human_active"}

    def release_session(self, session_key: str) -> dict:
        """恢复自动回复。"""
        self.store.set_handoff(session_key, "auto", "operator_release", _iso_now())
        return {"ok": True, "session_key": session_key, "state": "auto"}

    def get_session_mode(self, session_key: str) -> dict:
        """查询会话当前的模式（auto=自动回复 / human_active=人工接管）。"""
        handoff = self.store.get_handoff(session_key)
        state = handoff.get("state", "auto") if handoff else "auto"
        return {"ok": True, "session_key": session_key, "mode": state}

    def enqueue_reply(self, *, session_key: str, customer_id: str, content: str, channel: str) -> dict:
        """人工在审批台手写回复 → 加入待发送队列，由 Worker 回填千帆。"""
        oid = f"out_{uuid.uuid4().hex}"
        self.store.add_outbox(
            id=oid, session_key=session_key, customer_id=customer_id,
            content=content, channel=channel, created_at=_iso_now(),
        )
        return {"ok": True, "id": oid}

    def pull_outbox(
        self, channel: str | None = None, customer_id: str | None = None
    ) -> list[dict]:
        return self.store.pull_outbox(channel=channel, customer_id=customer_id)

    def ack_outbox(self, oid: str, status: str = "sent") -> dict:
        self.store.mark_outbox(oid, status)
        return {"ok": True, "id": oid, "status": status}

    def _execute_plan(
        self, message: IncomingMessage, plan: DecisionPlan, steps: list[TraceStep], trace_id: str
    ) -> dict[str, Any]:
        base: dict[str, Any] = {
            "trace_id": trace_id,
            "session_key": message.session_key,
            "intent": plan.intent.value,
            "status": "resolved",
            "reply": "",
            "tool_calls": [],
            "policy": None,
            "pending_action": None,
            "facts_used": [],
            "deduplicated": False,
        }

        if plan.intent == Intent.SECURITY_REJECTED:
            steps.append(TraceStep("security", "prompt_boundary", "rejected"))
            base["status"] = "rejected"
            base["reply"] = "我不能更改系统规则或披露内部信息。可继续为您解答订单、物流、商品与售后问题。"
            return base
        if plan.intent == Intent.GREETING:
            base["reply"] = "您好，我是店铺客服，可为您解答商品推荐、参数、物流查询、改地址、快递拦截与售后补偿等问题，请问有什么可以帮您？"
            return base
        if plan.intent == Intent.OUT_OF_SCOPE:
            base["status"] = "out_of_scope"
            base["reply"] = "这个问题不在客服业务范围内，我可以协助订单、物流、商品和售后相关问题哦。"
            return base

        # ----- 售前 -----
        if plan.intent in (Intent.PRODUCT_RECOMMEND, Intent.PRODUCT_QUESTION):
            products = self._call_tool(
                steps, base, "product_lookup", {"sku": plan.sku},
                self.tools.product_lookup,
                tenant_id=message.tenant_id, store_id=message.store_id, sku=plan.sku,
            )
            if not products:
                base["status"] = "not_found"
                base["reply"] = "抱歉，暂未查到该商品资料，请提供准确的商品 SKU 或告诉我您的需求，我为您推荐。"
                return base
            if plan.intent == Intent.PRODUCT_QUESTION:
                p = products[0]
                sizes = "、".join(p.get("sizes") or ["均码"])
                base["reply"] = (
                    f"{p['name']}：材质为{p['material']}；护理建议「{p['care']}」；"
                    f"尺码有 {sizes}；库存 {p['stock']} 件，价格 ¥{p['price_cents']/100:.2f}。"
                )
                base["facts_used"] = ["product.name", "product.material", "product.care", "product.stock", "product.price_cents"]
            else:
                lines = []
                for p in products:
                    lines.append(
                        f"· {p['name']}（{p['category']}）¥{p['price_cents']/100:.2f}，"
                        f"{'、'.join(p['selling_points'])}"
                    )
                base["reply"] = (
                    "亲，为您列出店内商品及价格：\n" + "\n".join(lines)
                    + "\n请问您想了解哪一款的详情呢？回复商品名即可，我为您详细介绍～"
                )
                base["facts_used"] = ["product.name", "product.price_cents", "product.selling_points"]
            return base

        if plan.intent == Intent.PLACE_ORDER:
            products = self._call_tool(
                steps, base, "product_lookup", {"sku": plan.sku},
                self.tools.product_lookup,
                tenant_id=message.tenant_id, store_id=message.store_id, sku=plan.sku,
            )
            if plan.sku and products:
                p = products[0]
                base["reply"] = (
                    f"已为您核对 {p['name']} 库存 {p['stock']} 件、¥{p['price_cents']/100:.2f}。"
                    "请在商品页选择尺码后点击「立即购买」并完成支付；付款后系统会自动同步订单，若超时未付款库存可能被释放，建议尽快下单哦。"
                )
            else:
                base["reply"] = "请在商品页选择心仪商品与规格后下单；付款完成后可随时找我查订单和物流进度。若提示库存紧张请尽快付款哦~"
            return base

        # ----- 售中 / 售后：需要订单 -----
        order_ids = extract_order_ids(message.text) or ([plan.order_id] if plan.order_id else [])
        if not order_ids:
            base["status"] = "needs_clarification"
            base["reply"] = "请提供订单号（形如 XHS-20260101-001），我好为您准确查询。"
            return base

        order_id = order_ids[0]
        orders = self._call_tool(
            steps, base, "order_lookup", {"order_id": order_id},
            self.tools.order_lookup,
            tenant_id=message.tenant_id, store_id=message.store_id,
            customer_id=message.customer_id, order_id=order_id,
        )
        if not orders:
            base["status"] = "not_found"
            base["reply"] = "暂未查到该订单，请核对订单号是否正确，或从「我的订单」中复制完整单号。"
            return base
        order = orders[0]

        if plan.intent == Intent.LOGISTICS_STATUS or plan.intent == Intent.LOGISTICS_EXCEPTION:
            shipment = self._call_tool(
                steps, base, "logistics_lookup", {"order_id": order_id},
                self.tools.logistics_lookup, order_id=order_id,
            )
            if not shipment:
                base["status"] = "not_available"
                base["reply"] = f"订单 {order_id} 暂时没有可用的物流轨迹，请稍后再试。"
                base["facts_used"] = ["order.order_id"]
                return base
            if plan.intent == Intent.LOGISTICS_EXCEPTION or shipment["status"] == "exception":
                base["reply"] = (
                    f"您的订单 {order_id}（{shipment['carrier']} {shipment['tracking_id']}）当前物流异常："
                    f"{shipment['latest_event']}。我已为您登记跟进，会持续关注并催件，请耐心等待；"
                    "如后续仍无更新，可随时找我，我会为您进一步处理直至解决。"
                )
            else:
                base["reply"] = (
                    f"您的订单 {order_id} 物流状态为「{shipment['status']}」，最新进度："
                    f"{shipment['latest_event']}（{shipment['updated_at']}）。"
                )
            base["facts_used"] = ["order.order_id", "shipment.carrier", "shipment.tracking_id", "shipment.status", "shipment.latest_event"]
            return base

        if plan.intent == Intent.MODIFY_ADDRESS:
            new_address = plan.address or self._extract_address(message.text)
            if not new_address:
                base["status"] = "needs_clarification"
                base["reply"] = "请提供您需要修改后的完整收货地址（含省市区、街道、门牌号），并确认收货人姓名。"
                return base
            decision = self.policy.evaluate_high_risk_action(action="modify_address", order=order)
            base["policy"] = decision.to_dict()
            steps.append(TraceStep("policy", "modify_address", decision.outcome.value, {"reason_code": decision.reason_code}))
            if decision.outcome == PolicyOutcome.DENY:
                base["status"] = "denied"
                base["reply"] = f"暂无法修改地址：{decision.explanation}"
                return base
            # 高风险写操作：创建待审批动作
            action = self._create_action(
                steps, base, "modify_address", order_id,
                {"order_id": order_id, "new_address": new_address, "receiver_name": order["receiver_name"]},
                decision,
            )
            base["pending_action"] = action
            base["status"] = "pending_approval"
            base["reply"] = (
                f"已为您登记修改收货地址申请（新地址：{new_address}）。"
                "该操作需人工审批确认后执行，我会尽快为您处理，谢谢。"
            )
            base["facts_used"] = ["order.order_id", "order.shipping_address"]
            return base

        if plan.intent == Intent.INTERCEPT_EXPRESS:
            decision = self.policy.evaluate_high_risk_action(action="intercept_express", order=order)
            base["policy"] = decision.to_dict()
            steps.append(TraceStep("policy", "intercept_express", decision.outcome.value, {"reason_code": decision.reason_code}))
            if decision.outcome == PolicyOutcome.DENY:
                base["status"] = "denied"
                base["reply"] = f"暂无法拦截：{decision.explanation}"
                return base
            shipment = self._call_tool(
                steps, base, "logistics_lookup", {"order_id": order_id},
                self.tools.logistics_lookup, order_id=order_id,
            )
            action = self._create_action(
                steps, base, "intercept_express", order_id,
                {"order_id": order_id, "tracking_id": shipment["tracking_id"] if shipment else "", "reason": plan.reason or "顾客申请拦截"},
                decision,
            )
            base["pending_action"] = action
            base["status"] = "pending_approval"
            base["reply"] = (
                f"已为您登记快递拦截申请（订单 {order_id}）。拦截需人工审批并通知快递执行，"
                "请留意后续状态更新，谢谢。"
            )
            base["facts_used"] = ["order.order_id", "shipment.tracking_id"]
            return base

        if plan.intent == Intent.COMPENSATION:
            amount_cents = plan.amount_cents or extract_amount_cents(message.text)
            reason = plan.reason or self._classify_reason(message.text)
            has_evidence = bool(message.attachments)
            if amount_cents is None:
                base["status"] = "needs_clarification"
                base["reply"] = "请明确您期望的补偿金额，我好按规则为您核实处理。"
                return base
            decision = self.policy.evaluate_compensation(
                order=order, amount_cents=amount_cents, reason=reason, has_evidence=has_evidence
            )
            base["policy"] = decision.to_dict()
            steps.append(TraceStep("policy", "compensation", decision.outcome.value, {"reason_code": decision.reason_code}))
            if decision.outcome == PolicyOutcome.DENY:
                base["status"] = "denied"
                base["reply"] = f"抱歉，该补偿申请暂无法通过：{decision.explanation}"
                # 引导小额赔偿挽留（参考架构：默认建议 3 元、可提至 5 元）
                if decision.reason_code == "EVIDENCE_REQUIRED":
                    base["reply"] += " 如为商品问题，请补充破损/错发/少发照片以便核实；如接受小额安抚，我可为您申请 ¥3 元补偿留货。"
                return base
            # 创建动作
            action = self._create_action(
                steps, base, "compensate", order_id,
                {"order_id": order_id, "amount_cents": amount_cents, "reason": reason},
                decision,
            )
            base["pending_action"] = action
            base["facts_used"] = ["order.order_id", "order.paid_amount_cents"]
            if decision.outcome == PolicyOutcome.REQUIRE_APPROVAL:
                base["status"] = "pending_approval"
                base["reply"] = (
                    f"已为您登记 ¥{amount_cents/100:.2f} 补偿申请，金额超过自动审批限额，需人工审批后发放，请耐心等待。"
                )
            else:
                base["status"] = "action_ready"
                base["reply"] = (
                    f"已为您申请 ¥{amount_cents/100:.2f} 补偿，将在确认后发放。感谢您的理解与支持！"
                )
            return base

        # 兜底
        base["status"] = "needs_clarification"
        base["reply"] = "请补充您要咨询的是订单、物流、商品还是售后问题，我好为您处理。"
        return base

    # ----- 辅助 -----

    def _call_tool(self, steps, response, name, public_args, function, **kwargs) -> Any:
        started = time.perf_counter()
        try:
            result = function(**kwargs)
            status = "ok"
        except Exception as e:  # noqa: BLE001
            result = None
            status = f"error:{type(e).__name__}"
        latency_ms = round((time.perf_counter() - started) * 1000)
        response["tool_calls"].append({"name": name, "status": status, "latency_ms": latency_ms, "args": public_args})
        steps.append(TraceStep("tool", name, status, public_args, latency_ms))
        return result

    def _create_action(self, steps, response, action_type, order_id, payload, decision) -> dict:
        import uuid as _uuid
        action_id = f"action_{_uuid.uuid4().hex}"
        state = (
            ActionState.PENDING_APPROVAL.value
            if decision.outcome == PolicyOutcome.REQUIRE_APPROVAL
            else ActionState.APPROVED.value
        )
        business_key = f"{self.tenant_id}|{action_type}|{order_id}"
        self.store.save_action(
            action_id=action_id,
            business_key=business_key,
            action_type=action_type,
            payload=payload,
            state=state,
            created_at=_iso_now(),
        )
        steps.append(TraceStep("action", f"{action_type}_proposal", state, {"action_id": action_id}))
        return {"action_id": action_id, "type": action_type, "state": state, **payload}

    @staticmethod
    def _classify_reason(text: str) -> str:
        if any(w in text for w in ("破损", "破了", "碎了")):
            return "damaged"
        if any(w in text for w in ("质量", "瑕疵", "坏了")):
            return "quality_issue"
        if any(w in text for w in ("错发", "发错")):
            return "wrong_item"
        if any(w in text for w in ("漏发", "少发", "少件")):
            return "missing_item"
        if any(w in text for w in ("物流", "快递", "延误", "慢")):
            return "logistics_exception"
        if any(w in text for w in ("不喜欢", "不想要")):
            return "change_of_mind"
        return "unspecified"

    @staticmethod
    def _extract_address(text: str) -> str | None:
        # 简单启发式：冒号/逗号分段中含省市区关键字的部分
        import re
        m = re.search(r"([\u4e00-\u9fa5]{0,2}(?:省|市|区|县|街道|路|号|栋|室|镇|乡).{4,60})", text)
        return m.group(0).strip() if m else None

    # ----- 动作执行 / 审批 -----

    def approve_action(self, action_id: str) -> dict:
        action = self.store.get_action(action_id)
        if action is None:
            return {"ok": False, "error": "action_not_found"}
        self.store.update_action_state(action_id, ActionState.APPROVED.value)
        action["state"] = ActionState.APPROVED.value
        return {"ok": True, **action}

    def execute_action(self, action_id: str, idempotency_key: str) -> dict:
        action = self.store.get_action(action_id)
        if action is None:
            return {"ok": False, "error": "action_not_found"}
        if action["state"] != ActionState.APPROVED.value:
            return {"ok": False, "error": "action_not_approved"}
        if action["state"] == ActionState.SUCCEEDED.value:
            return {"ok": True, "already_succeeded": True, **action}
        # 沙箱执行：记录成功
        self.store.update_action_state(action_id, ActionState.SUCCEEDED.value)
        action["state"] = ActionState.SUCCEEDED.value
        action["idempotency_key"] = idempotency_key
        return {"ok": True, **action}

    def list_actions(self) -> list[dict]:
        return self.store.list_pending_actions()

    def history(self, session_key: str) -> list[dict]:
        return self.store.history_decisions(session_key)

    def health(self) -> dict:
        return {"status": "ok" if self.store.health() else "degraded"}
