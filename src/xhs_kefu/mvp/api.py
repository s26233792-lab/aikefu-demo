"""MVP 多 Agent 客服链路的 HTTP API。

与单 Agent 的 /v1/decide 并存，新增 /mvp/decide 端点，
输入统一的 PlatformMessage，返回多 Agent 链路的处置结果。
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from .adapter import PlatformMessage
from .pipeline import MVPPipeline

router = APIRouter(prefix="/mvp", tags=["mvp"])
_pipeline = MVPPipeline()


class MVPRequest(BaseModel):
    platform: str = "qianfan"  # qianfan | douyin | qianniu
    store_id: str = "STORE-001"
    customer_id: str
    message_id: str = "m1"
    text: str
    attachments: list[str] = Field(default_factory=list)


@router.post("/decide")
async def mvp_decide(req: MVPRequest, request: Request, x_api_key: str | None = Header(default=None)):
    settings = getattr(request.app.state, "settings", None)
    if settings is not None and settings.api_key and x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="unauthorized")
    msg = PlatformMessage(
        platform=req.platform,
        store_id=req.store_id,
        customer_id=req.customer_id,
        message_id=req.message_id,
        text=req.text,
        attachments=tuple(req.attachments),
    )
    result = _pipeline.process(msg)
    return {
        "platform": result.platform,
        "customer_id": result.customer_id,
        "route": result.route,
        "agent": result.agent,
        "disposition": result.disposition,
        "reply": result.reply,
        "reason_code": result.reason_code,
        "facts": result.facts,
    }


@router.get("/health")
async def mvp_health():
    return {"status": "ok", "architecture": "multi-agent (router + faq/product/aftersale + rag)"}
