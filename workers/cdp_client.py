"""千帆桌面端（Electron）—— 轻量 CDP 客户端工具函数。

直接通过 websocket 连 CDP 的 page target，用 Runtime.evaluate 抓取/操作 DOM，
绕开 Playwright connect_over_cdp 枚举所有 target 时挂起的问题。

关键 CDP 端点：http://127.0.0.1:9222/json/list 列出所有 target，
其中客服工作台页面 URL 为 https://walle.xiaohongshu.com/cstools/seller/dashboard。
"""
from __future__ import annotations

import json
import os
import urllib.request
from urllib.parse import urlsplit
from typing import Any

import websocket  # type: ignore

def cdp_http() -> str:
    """返回可配置的本地 CDP 地址。"""
    legacy_base = os.environ.get("XHS_CDP_BASE", "").strip()
    if legacy_base and not os.environ.get("XHS_QIANFAN_CDP_PORT"):
        parsed = urlsplit(legacy_base)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("XHS_CDP_BASE 只允许本机 http://127.0.0.1 或 localhost 地址")
        raw = str(parsed.port or 9222)
    else:
        raw = os.environ.get("XHS_QIANFAN_CDP_PORT", "9222")
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError("XHS_QIANFAN_CDP_PORT 必须是有效端口号") from exc
    if not 1 <= port <= 65535:
        raise ValueError("XHS_QIANFAN_CDP_PORT 必须在 1～65535 之间")
    return f"http://127.0.0.1:{port}"


def list_targets() -> list[dict]:
    with urllib.request.urlopen(f"{cdp_http()}/json/list", timeout=5) as resp:
        return json.load(resp)


def find_cstools_page() -> dict | None:
    """找到已登录的客服工作台 page target。"""
    for t in list_targets():
        if t.get("type") == "page" and "cstools" in t.get("url", ""):
            if "login" not in t.get("url", ""):
                return t
    return None


class CdpSession:
    """一个 page target 的 CDP 会话，支持 evaluate 与 DOM 查询。"""

    def __init__(self, web_socket_url: str) -> None:
        self.web_socket_url = web_socket_url
        self.ws = websocket.create_connection(web_socket_url, timeout=10)
        self._id = 0
        self._pending: dict[int, Any] = {}

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        msg_id = self._id
        payload = {"id": msg_id, "method": method, "params": params or {}}
        self.ws.send(json.dumps(payload))
        while True:
            raw = self.ws.recv()
            data = json.loads(raw)
            if data.get("id") == msg_id:
                return data
            # 忽略事件消息

    def evaluate(self, expression: str) -> Any:
        """在页面上下文执行 JS，返回 result.value。"""
        resp = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        if "error" in resp:
            raise RuntimeError(f"CDP error: {resp['error']}")
        result = resp.get("result", {})
        if result.get("exceptionDetails"):
            raise RuntimeError(
                f"evaluate 异常: {json.dumps(result['exceptionDetails'], ensure_ascii=False)[:500]}"
            )
        return result.get("result", {}).get("value")

    def is_alive(self) -> bool:
        """检测连接是否仍可用（发一个轻量 ping 命令）。"""
        try:
            self.call("Runtime.evaluate", {"expression": "true", "returnByValue": True})
            return True
        except Exception:  # noqa: BLE001
            return False

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass
