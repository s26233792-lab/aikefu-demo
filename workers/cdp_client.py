"""千帆桌面端（Electron）—— 轻量 CDP 客户端工具函数。

直接通过 websocket 连 CDP 的 page target，用 Runtime.evaluate 抓取/操作 DOM，
绕开 Playwright connect_over_cdp 枚举所有 target 时挂起的问题。

关键 CDP 端点：由 XHS_CDP_URL / XHS_CDP_PORT 配置（默认 19222），
其 /json/list 列出所有 target，
其中客服工作台页面 URL 为 https://walle.xiaohongshu.com/cstools/seller/dashboard。
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

import websocket  # type: ignore

CDP_HTTP = os.environ.get(
    "XHS_CDP_URL",
    f"http://127.0.0.1:{os.environ.get('XHS_CDP_PORT', '19222')}",
).rstrip("/")


def list_targets(cdp_http: str = CDP_HTTP) -> list[dict]:
    with urllib.request.urlopen(f"{cdp_http.rstrip('/')}/json/list", timeout=5) as resp:
        return json.load(resp)


def find_page(
    *,
    cdp_http: str = CDP_HTTP,
    url_keywords: tuple[str, ...] = (),
    title_keywords: tuple[str, ...] = (),
    exclude_keywords: tuple[str, ...] = ("login",),
    target_types: tuple[str, ...] = ("page",),
) -> dict | None:
    """按 URL/标题找到页面，供千帆、抖店等不同平台复用。"""
    for target in list_targets(cdp_http):
        if target.get("type") not in target_types:
            continue
        url = str(target.get("url", "")).lower()
        title = str(target.get("title", "")).lower()
        haystack = f"{url}\n{title}"
        if any(word.lower() in haystack for word in exclude_keywords):
            continue
        if url_keywords and any(word.lower() in url for word in url_keywords):
            return target
        if title_keywords and any(word.lower() in title for word in title_keywords):
            return target
    return None


def find_cstools_page() -> dict | None:
    """找到已登录的客服工作台 page target。"""
    return find_page(cdp_http=CDP_HTTP, url_keywords=("cstools",))


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
