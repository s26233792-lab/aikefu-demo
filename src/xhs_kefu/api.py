"""小红书千帆客服 Agent —— FastAPI 决策 API。

对齐参考架构的 HTTP 边界：
- POST /v1/decide        顾客消息进入决策（去重/锁/记忆/规划/工具/风控/回复）
- POST /v1/tools/{name}  工具网关（后端再次校验，模型参数按不可信处理）
- GET  /v1/actions       列出待审批动作
- POST /v1/actions/{id}/approve   人工审批
- POST /v1/actions/{id}/execute   人工执行写操作
- GET  /v1/history       会话决策历史（供可视化面板）
- GET  /health           健康检查
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .config import Settings
from .domain import IncomingMessage
from .feedback import detect_negative_feedback
from .fixtures import Fixtures
from .planner import build_planner
from .policy import CompensationRule, PolicyEngine
from .runtime import AgentRuntime
from .storage import SQLiteStore


class DecideRequest(BaseModel):
    tenant_id: str = "demo"
    store_id: str = "STORE-001"
    channel: str = "xhs_qianfan"
    customer_id: str
    message_id: str
    text: str
    attachments: list[str] = Field(default_factory=list)


class ZhixiaRequest(BaseModel):
    text: str
    session_key: str = "zhixia-default"
    tenant_id: str = "demo"
    store_id: str = "STORE-001"
    channel: str = "xhs_qianfan_desktop"
    customer_id: str | None = None
    message_id: str | None = None
    # 外部客服工作台通常已有平台欢迎语或人工接入语，不应再次自我介绍。
    suppress_intro: bool = False


class DouyinBridgeRequest(BaseModel):
    """抖店飞鸽桥接层的稳定内部协议。

    这里不冒充抖店开放平台的原始回调格式；官方网关完成验签后，或本地
    飞鸽 Worker 读取消息后，都统一转换成这一结构再进入 Agent。
    """

    text: str
    customer_id: str
    message_id: str | None = None
    tenant_id: str = "demo"
    store_id: str = "STORE-001"
    suppress_intro: bool = True


class FeedbackCreateRequest(BaseModel):
    content: str
    customer_id: str = "manual"
    tenant_id: str = "demo"
    store_id: str = "STORE-001"
    channel: str = "manual"
    category: str | None = None
    severity: str | None = None
    message_id: str | None = None


class FeedbackStatusRequest(BaseModel):
    status: str


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    # 确保数据库目录存在
    import os
    os.makedirs(os.path.dirname(settings.database_path), exist_ok=True)

    store = SQLiteStore(settings.database_path)
    fixtures = Fixtures(settings.data_dir)
    rule = CompensationRule.from_file(settings.policy_path) if settings.policy_path.exists() else CompensationRule.defaults()
    policy = PolicyEngine(rule)
    planner = build_planner(
        settings.llm_mode,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
    )
    llm_agent = None
    intent_classifier = None
    if settings.llm_mode == "llm" and settings.llm_api_key:
        from .llm_agent import LLMAgent
        from .intent_classifier import IntentClassifier
        llm_agent = LLMAgent(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
        )
        intent_classifier = IntentClassifier(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
        )
    runtime = AgentRuntime(
        store=store,
        fixtures=fixtures,
        policy=policy,
        planner=planner,
        store_id=settings.store_id,
        tenant_id=settings.tenant_id,
        llm_agent=llm_agent,
        intent_classifier=intent_classifier,
    )

    app = FastAPI(title="栀夏多平台客服 Agent", version="1.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.runtime = runtime
    app.state.settings = settings

    from .web.router import router as web_router
    app.include_router(web_router)

    # MVP 多 Agent 架构（与单 Agent /v1/decide 并存）
    from .mvp.api import router as mvp_router
    app.include_router(mvp_router)

    # 栀夏 ZHIXIA 女装客服 Agent（agent.md 规格）
    from .zhixia_agent import ZhixiaLLMAgent
    from .zhixia_runtime import ZhixiaRuntime
    from .zhixia_tools import ZhixiaTools
    zhixia_llm = None
    if settings.llm_api_key:
        zhixia_llm = ZhixiaLLMAgent(
            base_url=settings.llm_base_url, model=settings.llm_model, api_key=settings.llm_api_key,
        )
    zhixia_runtime = ZhixiaRuntime(llm_agent=zhixia_llm, tools=ZhixiaTools(store=store))

    def _record_feedback(
        *,
        text: str,
        customer_id: str,
        session_key: str,
        tenant_id: str,
        store_id: str,
        channel: str,
        message_id: str | None,
        result: dict[str, Any] | None = None,
        category: str | None = None,
        severity: str | None = None,
        created_at: str | None = None,
    ) -> str | None:
        result = result or {}
        signal = detect_negative_feedback(
            text,
            tone=result.get("tone"),
            disposition=result.get("disposition"),
        )
        if signal is None and not category:
            return None
        identity = message_id or hashlib.sha1(
            f"{session_key}|{text}".encode("utf-8")
        ).hexdigest()[:20]
        feedback_id = f"fb_{hashlib.sha1(f'{channel}|{identity}'.encode()).hexdigest()[:18]}"
        now = created_at or datetime.now(timezone.utc).isoformat()
        store.add_feedback(
            id=feedback_id,
            message_id=message_id,
            session_key=session_key,
            customer_id=customer_id,
            tenant_id=tenant_id,
            store_id=store_id,
            channel=channel,
            category=category or signal.category,
            severity=severity or signal.severity,
            trigger_word=signal.trigger if signal else "manual",
            content=text,
            created_at=now,
        )
        return feedback_id

    @app.post("/zhixia/decide")
    async def zhixia_decide(req: ZhixiaRequest, x_api_key: str | None = Header(default=None)):
        _auth(x_api_key)
        # 从 store 读最近会话（复用单 Agent 的存储）
        customer_id = req.customer_id or req.session_key or "unknown"
        session_key = "|".join(
            ("zhixia", req.tenant_id, req.channel, req.store_id, customer_id)
        )
        handoff = store.get_handoff(session_key)
        if handoff and handoff.get("state") == "human_active":
            taken_over = {
                "status": "taken_over",
                "disposition": "handoff_human",
                "needs_approval": True,
                "reply": "",
                "session_key": session_key,
                "handoff_reason": handoff.get("reason") or "operator_takeover",
            }
            feedback_id = _record_feedback(
                text=req.text, customer_id=customer_id, session_key=session_key,
                tenant_id=req.tenant_id, store_id=req.store_id, channel=req.channel,
                message_id=req.message_id, result=taken_over,
            )
            if feedback_id:
                taken_over["feedback_id"] = feedback_id
            return taken_over
        history = store.recent_turns(session_key, 8)
        result = await zhixia_runtime.handle(
            text=req.text,
            history=history,
            suppress_intro=req.suppress_intro,
        )
        # 对齐 Worker 期望的兼容字段（status / needs_approval）。先确定这条回复
        # 是否真的会发给顾客，未发送的待审草稿不能混入后续对话记忆。
        disposition = result.get("disposition", "auto_reply")
        if disposition == "handoff_human":
            status = "taken_over"
            needs_approval = True
        elif disposition == "require_approval":
            status = "pending_approval"
            needs_approval = True
        else:
            status = "resolved"
            needs_approval = False

        # 持久化会话。消息 ID 才是一次事件的身份；用文本哈希会让顾客重复
        # 发送“好的/确认”时覆盖上一轮，破坏真实时序。
        now = datetime.now(timezone.utc).isoformat()
        event_id = req.message_id or f"local-{uuid.uuid4().hex}"
        event_key = f"{session_key}|{event_id}"
        store.save_turn(
            dedupe_key=f"{event_key}|user",
            session_key=session_key, role="user", content=req.text, created_at=now,
        )
        reply_will_be_sent = bool(
            result.get("reply")
            and (not needs_approval or result.get("send_before_handoff"))
        )
        if reply_will_be_sent:
            store.save_turn(
                dedupe_key=f"{event_key}|assistant",
                session_key=session_key, role="assistant", content=result["reply"], created_at=now,
            )
        # 需人工/待审批时，入审批队列（供审批台 + Worker 弹提醒）
        mod_id = None
        if needs_approval:
            identity = req.message_id or f"{req.text}{session_key}"
            deterministic_mod_id = f"mod_{hashlib.sha1(f'{session_key}|{identity}'.encode()).hexdigest()[:16]}"
            mod_id = deterministic_mod_id if req.message_id else (
                result.get("moderation_id") or deterministic_mod_id
            )
            store.add_moderation(
                id=mod_id, session_key=session_key, customer_id=customer_id,
                kind="handoff" if disposition == "handoff_human" else "reply",
                # 人工接管任务展示顾客原话；安抚回复本身不应覆盖投诉内容。
                content=(
                    req.text
                    if disposition == "handoff_human"
                    else (result.get("reply") or req.text)
                ),
                intent=result.get("intent", "handoff"),
                reason_code=result.get("handoff_reason") or "NEEDS_HUMAN",
                created_at=now,
                tenant_id=req.tenant_id,
                store_id=req.store_id,
                channel=req.channel,
            )
        if disposition == "handoff_human":
            # 一次性安抚发送后立刻锁定会话。后续顾客消息只提醒人工，
            # 不再交给 Agent 生成回复，直到客服明确释放会话。
            store.set_handoff(
                session_key=session_key,
                state="human_active",
                reason=result.get("handoff_reason") or "automatic_handoff",
                updated_at=now,
            )
        result["status"] = status
        result["needs_approval"] = needs_approval
        result["moderation_id"] = mod_id
        result["session_key"] = session_key
        feedback_id = _record_feedback(
            text=req.text, customer_id=customer_id, session_key=session_key,
            tenant_id=req.tenant_id, store_id=req.store_id, channel=req.channel,
            message_id=req.message_id, result=result, created_at=now,
        )
        if feedback_id:
            result["feedback_id"] = feedback_id
        return result

    @app.post("/platforms/douyin/decide")
    async def douyin_decide(
        req: DouyinBridgeRequest,
        x_api_key: str | None = Header(default=None),
    ):
        """飞鸽本地桥接/官方验签网关共用的抖店消息入口。"""
        return await zhixia_decide(
            ZhixiaRequest(
                text=req.text,
                session_key=req.customer_id,
                customer_id=req.customer_id,
                message_id=req.message_id,
                tenant_id=req.tenant_id,
                store_id=req.store_id,
                channel="douyin_feige",
                suppress_intro=req.suppress_intro,
            ),
            x_api_key,
        )

    @app.get("/zhixia/logistics")
    async def zhixia_logistics(
        order_id: str,
        phone_last4: str = "",
        x_api_key: str | None = Header(default=None),
    ):
        """查询模拟物流轨迹（规则生成）。"""
        _auth(x_api_key)
        result = zhixia_runtime.tools.logistics_lookup(order_id, phone_last4 or None)
        if result is None:
            raise HTTPException(status_code=404, detail="订单不存在，或核验信息不匹配")
        return result

    def _auth(x_api_key: str | None = Header(default=None)) -> None:
        if settings.api_key and x_api_key != settings.api_key:
            raise HTTPException(status_code=401, detail="invalid api key")

    @app.get("/health")
    async def health():
        return {
            "status": runtime.health()["status"],
            "llm_mode": settings.llm_mode,
            "llm_model": settings.llm_model,
            "llm_ready": settings.llm_mode == "llm" and bool(settings.llm_api_key),
        }

    @app.post("/v1/decide")
    async def decide(req: DecideRequest, x_api_key: str | None = Header(default=None)):
        _auth(x_api_key)
        message = IncomingMessage(
            tenant_id=req.tenant_id,
            channel=req.channel,
            store_id=req.store_id,
            customer_id=req.customer_id,
            message_id=req.message_id,
            text=req.text,
            attachments=tuple(req.attachments),
        )
        result = await runtime.handle_message(message)
        session_key = message.session_key
        feedback_id = _record_feedback(
            text=req.text, customer_id=req.customer_id, session_key=session_key,
            tenant_id=req.tenant_id, store_id=req.store_id, channel=req.channel,
            message_id=req.message_id, result=result,
        )
        if feedback_id:
            result["feedback_id"] = feedback_id
        return result

    @app.get("/v1/actions")
    async def list_actions(x_api_key: str | None = Header(default=None)):
        _auth(x_api_key)
        return {"actions": runtime.list_actions()}

    @app.post("/v1/actions/{action_id}/approve")
    async def approve_action(action_id: str, x_api_key: str | None = Header(default=None)):
        _auth(x_api_key)
        result = runtime.approve_action(action_id)
        if not result.get("ok"):
            raise HTTPException(status_code=404, detail=result.get("error"))
        return result

    @app.post("/v1/actions/{action_id}/execute")
    async def execute_action(
        action_id: str,
        body: dict[str, Any] | None = None,
        x_api_key: str | None = Header(default=None),
    ):
        _auth(x_api_key)
        body = body or {}
        idempotency_key = body.get("idempotency_key") or hashlib.sha256(action_id.encode()).hexdigest()[:16]
        result = runtime.execute_action(action_id, idempotency_key)
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("error"))
        return result

    @app.get("/v1/history")
    async def history(
        customer_id: str,
        store_id: str = "STORE-001",
        tenant_id: str = "demo",
        channel: str = "xhs_qianfan",
        x_api_key: str | None = Header(default=None),
    ):
        _auth(x_api_key)
        session_key = "|".join((tenant_id, channel, store_id, customer_id))
        return {"history": runtime.history(session_key)}

    # ----- 用户不良反馈与统计 -----

    @app.get("/v1/feedback")
    async def list_feedback(
        status: str | None = None,
        channel: str | None = None,
        category: str | None = None,
        severity: str | None = None,
        limit: int = 100,
        x_api_key: str | None = Header(default=None),
    ):
        _auth(x_api_key)
        return {
            "feedback": store.list_feedback(
                status=status, channel=channel, category=category,
                severity=severity, limit=limit,
            )
        }

    @app.get("/v1/feedback/stats")
    async def feedback_stats(
        days: int = 30,
        x_api_key: str | None = Header(default=None),
    ):
        _auth(x_api_key)
        return store.feedback_stats(days)

    @app.post("/v1/feedback")
    async def create_feedback(
        req: FeedbackCreateRequest,
        x_api_key: str | None = Header(default=None),
    ):
        _auth(x_api_key)
        content = req.content.strip()
        if not content:
            raise HTTPException(status_code=400, detail="content required")
        session_key = "|".join(
            ("feedback", req.tenant_id, req.channel, req.store_id, req.customer_id)
        )
        feedback_id = _record_feedback(
            text=content, customer_id=req.customer_id, session_key=session_key,
            tenant_id=req.tenant_id, store_id=req.store_id, channel=req.channel,
            message_id=req.message_id, category=req.category or "其他反馈",
            severity=req.severity or "medium",
        )
        return {"ok": True, "feedback_id": feedback_id}

    @app.post("/v1/feedback/{feedback_id}/status")
    async def update_feedback_status(
        feedback_id: str,
        req: FeedbackStatusRequest,
        x_api_key: str | None = Header(default=None),
    ):
        _auth(x_api_key)
        if req.status not in {"open", "processing", "resolved", "dismissed"}:
            raise HTTPException(status_code=400, detail="invalid feedback status")
        item = store.update_feedback_status(
            feedback_id, req.status, datetime.now(timezone.utc).isoformat()
        )
        if item is None:
            raise HTTPException(status_code=404, detail="feedback not found")
        return {"ok": True, "feedback": item}

    # ----- 人工审批 / 接管 -----

    @app.get("/v1/moderation")
    async def list_moderation(status: str | None = None, x_api_key: str | None = Header(default=None)):
        _auth(x_api_key)
        return {"moderation": runtime.list_moderation(status)}

    @app.post("/v1/moderation/{mod_id}/approve")
    async def approve_moderation(mod_id: str, x_api_key: str | None = Header(default=None)):
        _auth(x_api_key)
        result = runtime.approve_moderation(mod_id)
        if not result.get("ok"):
            raise HTTPException(status_code=404, detail=result.get("error"))
        # 审批通过后：仅「reply」类型的待审草稿（LLM 生成的可发送回复）才入发送队列。
        # 「handoff」类型内容是顾客原诉求（如"我要投诉"），绝不能当回复发回给顾客；
        # 「action」类型是写操作，需走写操作执行流程，不是直接发消息。
        if result.get("kind") == "reply":
            content = result.get("content", "")
            if content:
                runtime.enqueue_reply(
                    session_key=result.get("session_key", ""),
                    customer_id=result.get("customer_id", ""),
                    content=content,
                    channel=result.get("channel", "xhs_qianfan_desktop"),
                )
                result["enqueued"] = True
        return result

    @app.post("/v1/moderation/{mod_id}/reject")
    async def reject_moderation(mod_id: str, x_api_key: str | None = Header(default=None)):
        _auth(x_api_key)
        result = runtime.reject_moderation(mod_id)
        if not result.get("ok"):
            raise HTTPException(status_code=404, detail=result.get("error"))
        return result

    @app.post("/v1/handoff")
    async def take_over(
        body: dict[str, Any] | None = None,
        x_api_key: str | None = Header(default=None),
    ):
        _auth(x_api_key)
        body = body or {}
        session_key = body.get("session_key", "")
        if not session_key:
            raise HTTPException(status_code=400, detail="session_key required")
        action = body.get("action", "take_over")  # take_over | release
        reason = body.get("reason", "operator")
        if action == "release":
            return runtime.release_session(session_key)
        return runtime.take_over(session_key, reason)

    @app.get("/v1/handoff/status")
    async def get_handoff_status(
        session_key: str,
        x_api_key: str | None = Header(default=None),
    ):
        """查询会话当前模式（auto=自动回复 / human_active=人工接管）。"""
        _auth(x_api_key)
        return runtime.get_session_mode(session_key)

    # ----- 待发送队列（outbox）：审批台手写/审批通过的回复 → Worker 回填千帆 -----

    @app.post("/v1/outbox")
    async def enqueue_reply(body: dict[str, Any], x_api_key: str | None = Header(default=None)):
        _auth(x_api_key)
        content = body.get("content", "")
        if not content:
            raise HTTPException(status_code=400, detail="content required")
        return runtime.enqueue_reply(
            session_key=body.get("session_key", ""),
            customer_id=body.get("customer_id", ""),
            content=content,
            channel=body.get("channel", "xhs_qianfan_desktop"),
        )

    @app.get("/v1/outbox/pull")
    async def pull_outbox(
        channel: str | None = None,
        customer_id: str | None = None,
        x_api_key: str | None = Header(default=None),
    ):
        _auth(x_api_key)
        return {
            "outbox": runtime.pull_outbox(
                channel=channel, customer_id=customer_id
            )
        }

    @app.post("/v1/outbox/{oid}/ack")
    async def ack_outbox(oid: str, body: dict[str, Any] | None = None, x_api_key: str | None = Header(default=None)):
        _auth(x_api_key)
        body = body or {}
        status = body.get("status", "sent")
        return runtime.ack_outbox(oid, status)

    return app


app = create_app()
