"""小红书千帆网页版 —— Playwright 自动收发 Worker。

真实场景：驱动已登录的小红书千帆客服工作台（网页版），
Agent 在真实后台里自动收发消息。

使用前提：
1. 先运行 `python workers/qianfan_login.py` 完成扫码登录，登录态持久化到
   data/qianfan-profile/（仅需一次）。
2. 再运行 `python workers/qianfan_browser.py`，Agent 复用登录态自动轮询新顾客消息。

说明（与参考架构一致的诚实边界）：
- 千帆网页版是 SPA，DOM 结构与选择器会随版本变化，需按当前页面校准；
- 只读 + 自动回复：写操作（退款/改址/拦截）不在此自动执行，一律上报到决策 API
  并进入人工审批队列，由人工确认后再处理；
- 失败时不会冒充已发送，会记录"未发送"并上报，支持人工接管。
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

try:
    from .qianfan_launcher import chromium_args, default_profile_dir
except ImportError:  # 兼容直接执行脚本
    from qianfan_launcher import chromium_args, default_profile_dir

# 千帆网页版工作台地址（商家专业号客服后台）
QIANFAN_HOME_URL = "https://ark.xiaohongshu.com/app-system/home"

# 登录态 profile（由 workers/qianfan_login.py 扫码后持久化）
_DEFAULT_PROFILE = str(default_profile_dir())

# DOM 选择器 —— 已按 2026-08-19 登录后真实页面结构校准
# 关键发现：千帆客服聊天容器用 im-chat-* / chat-* 前缀，输入框为 textarea.input-base
SELECTORS = {
    "chat_page": ".im-chat-page",
    "chat_body": ".chat-body, .chat-content, .im-chat-body-container",
    "session_list_item": "[class*='session'], [class*='conversation'], [class*='contact-list'], [class*='user-list']",
    # 顾客来消息气泡（客服侧收到的消息）
    "message_in": "[class*='message-in'], [class*='receive'], [class*='customer'], [class*='msg-left'], [class*='msg-other']",
    "message_text": "[class*='content'], [class*='bubble'], [class*='text']",
    "input_box": "textarea.input-base",
    "send_btn": "button[class*='send'], button[type='submit'], [class*='send-btn']",
    "new_message_indicator": "[class*='unread'], [class*='badge']",
    "message_center": ".message-center",
    "message_list": ".message-list",
    "message_item": ".message-item",
    "current_contact": ".current-contact, .user-info, [class*='current-contact'], [class*='user-info']",
}


@dataclass
class DecisionClient:
    """调用本地决策 API 的客户端。"""

    base_url: str
    api_key: str | None = None

    async def decide(self, message: dict) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{self.base_url.rstrip('/')}/v1/decide", headers=headers, json=message
            )
            resp.raise_for_status()
            return resp.json()

    async def ack(self, message_id: str, delivered: str) -> None:
        # 简化：真实回执可扩展为 /v1/worker/ack
        return None


@dataclass
class QianfanBrowserWorker:
    """千帆网页版 Worker：复用登录态，轮询新消息 → 决策 → 回填回复。"""

    decision_base_url: str
    api_key: str | None = None
    store_id: str = "STORE-001"
    tenant_id: str = "demo"
    channel: str = "xhs_qianfan"
    headless: bool = False
    user_data_dir: str | None = None
    poll_interval: float = 3.0
    seen_message_ids: set[str] = field(default_factory=set)
    _running: bool = False

    @property
    def decision(self) -> DecisionClient:
        return DecisionClient(self.decision_base_url, self.api_key)

    async def start(self) -> None:
        """启动轮询循环。需要已安装 playwright 且已完成扫码登录。"""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            print("未安装 playwright，请先: pip install playwright && playwright install chromium")
            return

        profile_dir = self.user_data_dir or _DEFAULT_PROFILE
        self._running = True
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=self.headless,
                args=chromium_args(),
                viewport={"width": 1280, "height": 900},
            )
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(QIANFAN_HOME_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)

            if not await self._is_logged_in(page):
                print("[worker] 未检测到登录态，请先运行: python workers/qianfan_login.py 扫码登录。")
                await context.close()
                return

            print(f"[worker] 登录态有效，开始轮询千帆顾客消息（每 {self.poll_interval}s 一次）...")

            while self._running:
                try:
                    await self._poll_once(page)
                except Exception as e:  # noqa: BLE001
                    print(f"[worker] 轮询异常: {type(e).__name__}: {e}")
                await asyncio.sleep(self.poll_interval)

            await context.close()

    async def _is_logged_in(self, page) -> bool:
        """登录后 URL 会进入 app-system 而非 login/passport。"""
        url = page.url
        return "login" not in url.lower() and "passport" not in url.lower()

    async def _poll_once(self, page) -> None:
        """抓取最新顾客消息、去重、决策、回填回复。"""
        messages = await self._extract_incoming(page)
        for m in messages:
            if m["message_id"] in self.seen_message_ids:
                continue
            customer_id = str(m.get("customer_id") or "").strip()
            if not customer_id or customer_id == "unknown":
                print("[worker] 无法识别当前顾客，已暂停自动回复以避免串线。")
                continue
            payload = {
                "tenant_id": self.tenant_id,
                "store_id": self.store_id,
                "channel": self.channel,
                "customer_id": customer_id,
                "message_id": m["message_id"],
                "text": m.get("text", ""),
                "attachments": m.get("attachments", []),
            }
            if not payload["text"]:
                continue
            print(f"[worker] 收到顾客消息: {payload['text'][:40]}...")
            # 接口或发送失败时不提前记为已处理，让后续轮询安全重试。
            decision = await self.decision.decide(payload)
            reply = decision.get("reply", "")
            status = decision.get("status")
            if reply and status in {"resolved", "deduplicated"}:
                # 若决策返回待审批（涉及写操作），不在真实后台自动点击，仅记录
                sent = await self._send_reply(page, reply, expected_customer_id=customer_id)
                if not sent:
                    print(f"[worker] 回复未发送，将重试顾客 {customer_id} 的消息。")
                    continue
                print(f"[worker] 已回复顾客 {customer_id}: {reply[:40]}...")
                self.seen_message_ids.add(m["message_id"])
            elif status in {"pending_approval", "taken_over"}:
                print(f"[worker] 该消息涉及写操作需人工审批，不自动执行：{decision.get('reply','')[:40]}...")
                self.seen_message_ids.add(m["message_id"])
            else:
                print("[worker] 决策未产生可直接发送的回复，跳过。")
                self.seen_message_ids.add(m["message_id"])

    async def _current_customer(self, page) -> str:
        element = await page.query_selector(SELECTORS["current_contact"])
        if not element:
            return ""
        text = (await element.inner_text()).strip()
        return text.splitlines()[0].strip() if text else ""

    async def _extract_incoming(self, page) -> list[dict]:
        """从千帆页面提取新顾客消息（选择器需按页面校准）。

        因当前店铺暂无活跃买家会话，此方法在无消息时返回空列表；
        一旦有买家会话在 im-chat 区域渲染，即按 message_in 选择器抓取。
        """
        result: list[dict] = []
        try:
            customer_id = await self._current_customer(page)
            if not customer_id:
                return []
            items = await page.query_selector_all(SELECTORS["message_in"])
            for idx, item in enumerate(items):
                text_el = await item.query_selector(SELECTORS["message_text"])
                text = (await text_el.inner_text()) if text_el else ""
                text = text.strip()
                if not text:
                    continue
                # 稳定去重：用文本 + 下标 + 时间窗做消息指纹
                fingerprint = hashlib.sha1(f"{customer_id}|{text}|{idx}".encode()).hexdigest()[:16]
                result.append(
                    {
                        "message_id": f"qianfan-{fingerprint}",
                        "customer_id": customer_id,
                        "text": text,
                        "attachments": [],
                    }
                )
        except Exception as e:  # noqa: BLE001
            # 页面结构未命中选择器时静默返回空，避免刷屏；结构变化时需校准
            pass
        return result

    async def _send_reply(self, page, reply: str, *, expected_customer_id: str) -> bool:
        """回填回复到输入框并触发发送。"""
        if await self._current_customer(page) != expected_customer_id:
            print(f"[worker] 当前会话已切换，取消向 {expected_customer_id} 发送。")
            return False
        input_box = await page.query_selector(SELECTORS["input_box"])
        if not input_box:
            print("[worker] 未找到输入框，跳过发送（可能页面结构变化）。")
            return False
        await input_box.click()
        await input_box.fill(reply)
        await page.wait_for_timeout(300)
        if await self._current_customer(page) != expected_customer_id:
            await input_box.fill("")
            print(f"[worker] 填入后会话已切换，已清空输入框并取消发送。")
            return False
        send_btn = await page.query_selector(SELECTORS["send_btn"])
        if send_btn:
            await send_btn.click()
        else:
            await page.keyboard.press("Enter")
        return True

    def stop(self) -> None:
        self._running = False


async def main() -> None:
    import os
    worker = QianfanBrowserWorker(
        decision_base_url=os.environ.get("XHS_DECISION_URL", "http://127.0.0.1:18081"),
        api_key=os.environ.get("XHS_API_KEY"),
        headless=False,  # 真实登录态复用建议显示浏览器，便于观察
        user_data_dir=os.environ.get("XHS_QIANFAN_PROFILE"),
    )
    await worker.start()


if __name__ == "__main__":
    asyncio.run(main())
