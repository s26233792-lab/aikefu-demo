"""小红书千帆客服 Demo —— Web 界面。

一个单页：左侧模拟小红书千帆客服聊天窗口（售前/售中/售后三个场景切换），
右侧实时展示 Agent 决策链路（意图 → 工具 → 风控 → 审批/执行 → 回复）。
通过 /v1/decide 走真实后端决策链路。
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from pathlib import Path

router = APIRouter()

_WEB_DIR = Path(__file__).resolve().parent


@router.get("/", response_class=HTMLResponse)
async def index() -> str:
    html = (_WEB_DIR / "index.html").read_text(encoding="utf-8")
    return html
