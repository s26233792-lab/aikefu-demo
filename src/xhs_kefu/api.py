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
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .config import Settings
from .domain import IncomingMessage
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

    app = FastAPI(title="小红书千帆客服 Agent", version="1.0.0")
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
    zhixia_llm = None
    if settings.llm_api_key:
        zhixia_llm = ZhixiaLLMAgent(
            base_url=settings.llm_base_url, model=settings.llm_model, api_key=settings.llm_api_key,
        )
    zhixia_runtime = ZhixiaRuntime(llm_agent=zhixia_llm)

    @app.post("/zhixia/decide")
    async def zhixia_decide(req: ZhixiaRequest, x_api_key: str | None = Header(default=None)):
        _auth(x_api_key)
        # 从 store 读最近会话（复用单 Agent 的存储）
        session_key = f"zhixia|{req.session_key}"
        history = store.recent_turns(session_key, 8)
        result = await zhixia_runtime.handle(text=req.text, history=history)
        # 持久化会话
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        store.save_turn(
            dedupe_key=f"{session_key}|{hashlib.sha1(req.text.encode()).hexdigest()[:12]}",
            session_key=session_key, role="user", content=req.text, created_at=now,
        )
        if result.get("reply"):
            store.save_turn(
                dedupe_key=f"{session_key}|r{hashlib.sha1(req.text.encode()).hexdigest()[:12]}",
                session_key=session_key, role="assistant", content=result["reply"], created_at=now,
            )
        # 对齐 Worker 期望的兼容字段（status / needs_approval）
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
        # 需人工/待审批时，入审批队列（供审批台 + Worker 弹提醒）
        mod_id = None
        if needs_approval:
            mod_id = result.get("moderation_id") or f"mod_{hashlib.sha1(f'{req.text}{req.session_key}'.encode()).hexdigest()[:16]}"
            store.add_moderation(
                id=mod_id, session_key=session_key, customer_id=req.session_key,
                kind="handoff" if disposition == "handoff_human" else "reply",
                content=result.get("reply") or req.text, intent=result.get("intent", "handoff"),
                reason_code=result.get("handoff_reason") or "NEEDS_HUMAN",
                created_at=now,
            )
        result["status"] = status
        result["needs_approval"] = needs_approval
        result["moderation_id"] = mod_id
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
        return await runtime.handle_message(message)

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
                    session_key=f"demo|xhs_qianfan|STORE-001|{result.get('customer_id', '')}",
                    customer_id=result.get("customer_id", ""),
                    content=content,
                    channel="xhs_qianfan",
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
            channel=body.get("channel", "xhs_qianfan"),
        )

    @app.get("/v1/outbox/pull")
    async def pull_outbox(x_api_key: str | None = Header(default=None)):
        _auth(x_api_key)
        return {"outbox": runtime.pull_outbox()}

    @app.post("/v1/outbox/{oid}/ack")
    async def ack_outbox(oid: str, body: dict[str, Any] | None = None, x_api_key: str | None = Header(default=None)):
        _auth(x_api_key)
        body = body or {}
        status = body.get("status", "sent")
        return runtime.ack_outbox(oid, status)

    return app


app = create_app()
