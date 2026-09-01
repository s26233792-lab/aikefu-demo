"""抖店飞鸽客服 —— 本地 CDP 收发桥接。

把已登录的飞鸽网页接到同一套栀夏 DeepSeek Agent：

1. 只读取当前激活会话中“方向可明确判定为顾客”的新消息；
2. 普通咨询自动回填飞鸽；退款、赔偿、改址、投诉等继续走人工审批；
3. 只拉取 ``douyin_feige`` 且属于当前顾客的 outbox，避免跨平台/跨会话误发；
4. DOM 无法确认时失败关闭（不发送），并可用 ``douyin-dump`` 采集脱敏结构做校准。

这不是伪造抖店开放平台的回调协议。获得“客服机器人/商家 AI 客服”场景权限后，
官方回调网关完成验签并转换到 ``/platforms/douyin/decide`` 即可复用同一 Agent。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import subprocess
from pathlib import Path
from typing import Any

import httpx

try:
    from .cdp_client import CdpSession, find_page
except ImportError:  # 允许直接运行本文件
    from cdp_client import CdpSession, find_page


DECISION_URL = "http://127.0.0.1:18081"
CDP_URL = "http://127.0.0.1:19223"
CHANNEL = "douyin_feige"


def _csv_env(name: str, default: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in os.environ.get(name, default).split(",") if item.strip())


URL_KEYWORDS = _csv_env(
    "DOUYIN_PAGE_URL_KEYWORDS", "feige,im.jinritemai.com,customer-service"
)
TITLE_KEYWORDS = _csv_env("DOUYIN_PAGE_TITLE_KEYWORDS", "飞鸽,抖店客服")
ROW_SELECTORS = _csv_env(
    "DOUYIN_MESSAGE_ROW_SELECTORS",
    "[data-qa-id='qa-message-warpper'],.msgItemWrap,.message-item,.msg-item,[class*='messageItem'],[class*='message-item'],[class*='msg-item']",
)
INPUT_SELECTORS = _csv_env(
    "DOUYIN_INPUT_SELECTORS",
    "textarea[data-qa-id='qa-send-message-textarea'],textarea,[contenteditable='true'][role='textbox'],[contenteditable='true']",
)
CONTACT_SELECTORS = _csv_env(
    "DOUYIN_CONTACT_SELECTORS",
    "[class*='chat-header'] [class*='name'],[class*='conversation-header'] [class*='name'],[class*='session'][class*='active'] [class*='name'],[class*='contact'][class*='active'] [class*='name']",
)
SESSION_ITEM_SELECTORS = _csv_env(
    "DOUYIN_SESSION_ITEM_SELECTORS",
    ".conversation-item,.session-item,.chat-item,[class*='conversation-item'],[class*='session-item'],[class*='chat-item']",
)
UNREAD_SELECTORS = _csv_env(
    "DOUYIN_UNREAD_SELECTORS",
    ".unread-count,.unread-badge,[class*='unreadCount'],[class*='unread-count'],[class*='unread-badge']",
)


def find_feige_page(cdp_url: str = CDP_URL) -> dict | None:
    """寻找已打开的飞鸽会话页；普通抖店后台首页不会被当作客服页。"""
    return find_page(
        cdp_http=cdp_url,
        url_keywords=URL_KEYWORDS,
        title_keywords=TITLE_KEYWORDS,
        exclude_keywords=("login", "passport", "登录"),
        target_types=("page", "iframe"),
    )


def _last_customer_js() -> str:
    """生成方向判定脚本；方向不明确的消息不会进入 Agent。"""
    return r"""
(() => {
  const rowSelectors = __ROW_SELECTORS__;
  const contactSelectors = __CONTACT_SELECTORS__;
  const visible = el => {
    if (!el) return false;
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  };
  const firstVisible = selectors => {
    for (const selector of selectors) {
      for (const el of document.querySelectorAll(selector)) {
        if (visible(el) && (el.innerText || el.textContent || '').trim()) return el;
      }
    }
    return null;
  };
  const contactEl = firstVisible(contactSelectors);
  let customerId = contactEl ? (contactEl.innerText || contactEl.textContent || '').split('\n')[0].trim() : '';
  if (contactEl) {
    const context = contactEl.closest('[data-conversation-id],[data-session-id],[data-user-id],[data-id]');
    const stableId = context && (
      context.getAttribute('data-conversation-id') || context.getAttribute('data-session-id')
      || context.getAttribute('data-user-id') || context.getAttribute('data-id')
    );
    if (stableId) customerId = stableId;
  }
  const uniqueRows = [];
  const seenRows = new Set();
  for (const selector of rowSelectors) {
    for (const row of document.querySelectorAll(selector)) {
      if (!seenRows.has(row) && visible(row)) { seenRows.add(row); uniqueRows.push(row); }
    }
  }
  uniqueRows.sort((a, b) => {
    if (a === b) return 0;
    return (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1;
  });
  const sysTokens = ['系统消息','会话已结束','接入会话','以上为历史消息','机器人转人工'];
  const outboundTokens = ['outbound','out-going','outgoing','message-right','msg-right','self','seller','service'];
  const inboundTokens = ['inbound','in-coming','incoming','message-left','msg-left','other','buyer','customer'];
  const direction = row => {
    let el = row;
    let blob = '';
    let sawStart = false;
    let sawEnd = false;
    for (let depth = 0; el && depth < 4; depth++, el = el.parentElement) {
      blob += ' ' + String(el.className || '') + ' ' + String(el.getAttribute('data-direction') || '')
        + ' ' + String(el.getAttribute('data-side') || '') + ' ' + String(el.getAttribute('data-from') || '');
      const jc = getComputedStyle(el).justifyContent;
      if (jc === 'flex-end') sawEnd = true;
      if (jc === 'flex-start' && depth < 3) sawStart = true;
    }
    blob = blob.toLowerCase();
    if (outboundTokens.some(token => blob.includes(token))) return 'out';
    if (inboundTokens.some(token => blob.includes(token))) return 'in';
    if (sawEnd) return 'out';
    if (sawStart) return 'in';
    return 'unknown';
  };
  for (let i = uniqueRows.length - 1; i >= 0; i--) {
    const row = uniqueRows[i];
    if (direction(row) !== 'in') continue;
    const raw = (row.innerText || row.textContent || '').trim();
    if (!raw || sysTokens.some(token => raw.includes(token))) continue;
    const parts = raw.split('\n').map(s => s.trim()).filter(Boolean).filter(s =>
      !/^\d{1,2}:\d{2}(:\d{2})?$/.test(s) && !/^(已读|未读|送达|发送失败)$/.test(s)
    );
    const text = parts.join(' ').trim();
    if (!text) continue;
    const domId = row.getAttribute('data-message-id') || row.getAttribute('data-msg-id')
      || row.getAttribute('data-id') || row.id || String(i);
    let hash = 2166136261;
    for (const c of (customerId + '|' + domId + '|' + text)) {
      hash ^= c.charCodeAt(0); hash = Math.imul(hash, 16777619);
    }
    return { text, customer_id: customerId, hash: 'dy' + (hash >>> 0).toString(16), dom_id: domId };
  }
  return null;
})()
""".replace("__ROW_SELECTORS__", json.dumps(ROW_SELECTORS, ensure_ascii=False)).replace(
        "__CONTACT_SELECTORS__", json.dumps(CONTACT_SELECTORS, ensure_ascii=False)
    )


def _fill_js(reply: str) -> str:
    return r"""
(() => {
  const selectors = __INPUT_SELECTORS__;
  const reply = __REPLY__;
  const visible = el => {
    const s = getComputedStyle(el); const r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  };
  let input = null;
  for (const selector of selectors) {
    input = Array.from(document.querySelectorAll(selector)).find(visible);
    if (input) break;
  }
  if (!input) return false;
  input.focus();
  if (input instanceof HTMLTextAreaElement || input instanceof HTMLInputElement) {
    const proto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(input, reply);
  } else {
    input.textContent = reply;
  }
  input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: reply }));
  input.dispatchEvent(new Event('change', { bubbles: true }));
  return true;
})()
""".replace("__INPUT_SELECTORS__", json.dumps(INPUT_SELECTORS, ensure_ascii=False)).replace(
        "__REPLY__", json.dumps(reply, ensure_ascii=False)
    )


def _open_unread_js() -> str:
    """只在明确的会话列表项内部点击未读标记，不触碰页面其他通知角标。"""
    return r"""
(() => {
  const itemSelectors = __ITEM_SELECTORS__;
  const unreadSelectors = __UNREAD_SELECTORS__;
  const visible = el => {
    const s = getComputedStyle(el); const r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  };
  const items = [];
  const seen = new Set();
  for (const selector of itemSelectors) {
    for (const item of document.querySelectorAll(selector)) {
      if (!seen.has(item) && visible(item)) { seen.add(item); items.push(item); }
    }
  }
  for (const item of items) {
    const cls = String(item.className || '').toLowerCase();
    if (cls.includes('active') || item.getAttribute('aria-selected') === 'true') continue;
    for (const selector of unreadSelectors) {
      const badge = item.querySelector(selector);
      if (!badge || !visible(badge)) continue;
      const marker = ((badge.innerText || badge.textContent || '') + ' '
        + (badge.getAttribute('aria-label') || '') + ' ' + String(badge.className || '')).toLowerCase();
      if (!marker.trim()) continue;
      item.click();
      return true;
    }
  }
  return false;
})()
""".replace("__ITEM_SELECTORS__", json.dumps(SESSION_ITEM_SELECTORS, ensure_ascii=False)).replace(
        "__UNREAD_SELECTORS__", json.dumps(UNREAD_SELECTORS, ensure_ascii=False)
    )


_CLICK_SEND_JS = r"""
(() => {
  const visible = el => {
    const s = getComputedStyle(el); const r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  };
  const buttons = Array.from(document.querySelectorAll('button,[role="button"]')).filter(visible);
  const send = buttons.find(el => {
    const text = (el.innerText || el.textContent || '').trim();
    const label = (el.getAttribute('aria-label') || '').trim();
    return (text === '发送' || label === '发送') && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
  });
  if (!send) return false;
  send.click();
  return true;
})()
"""


def _structure_dump_js() -> str:
    """只保留节点结构/类名/短文本，不导出 Cookie、Storage 或完整聊天记录。"""
    return r"""
(() => {
  const selectors = __ROW_SELECTORS__;
  const rows = [];
  const seen = new Set();
  for (const selector of selectors) {
    for (const el of document.querySelectorAll(selector)) {
      if (seen.has(el)) continue; seen.add(el);
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) continue;
      rows.push({
        tag: el.tagName,
        class: String(el.className || '').slice(0, 300),
        dataDirection: el.getAttribute('data-direction'),
        dataSide: el.getAttribute('data-side'),
        justifyContent: getComputedStyle(el).justifyContent,
        textSample: (el.innerText || '').replace(/\s+/g, ' ').slice(0, 80)
      });
      if (rows.length >= 30) break;
    }
    if (rows.length >= 30) break;
  }
  const editables = Array.from(document.querySelectorAll('textarea,input,[contenteditable="true"]')).map(el => ({
    tag: el.tagName, class: String(el.className || '').slice(0, 300),
    placeholder: el.getAttribute('placeholder'), role: el.getAttribute('role')
  })).slice(0, 20);
  return { url: location.origin + location.pathname, title: document.title, rows, editables };
})()
""".replace("__ROW_SELECTORS__", json.dumps(ROW_SELECTORS, ensure_ascii=False))


class DouyinFeigeWorker:
    def __init__(
        self,
        *,
        decision_url: str = DECISION_URL,
        cdp_url: str = CDP_URL,
        api_key: str | None = None,
        store_id: str = "STORE-001",
        tenant_id: str = "demo",
        poll_interval: float = 2.0,
    ) -> None:
        self.decision_url = decision_url.rstrip("/")
        self.cdp_url = cdp_url.rstrip("/")
        self.api_key = api_key
        self.store_id = store_id
        self.tenant_id = tenant_id
        self.poll_interval = poll_interval
        self.channel = CHANNEL
        self.seen: set[str] = set()
        self.initialized_contacts: set[str] = set()
        self.sent_texts: set[str] = set()
        self.auto_open_unread = os.environ.get("DOUYIN_AUTO_OPEN_UNREAD", "1") not in (
            "0", "false", "False"
        )
        self._running = False
        self._last_log: dict[str, str] = {}

    @property
    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        return headers

    def _log_once(self, key: str, message: str) -> None:
        if self._last_log.get(key) != message:
            self._last_log[key] = message
            print(message)

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                target = find_feige_page(self.cdp_url)
            except Exception as exc:  # noqa: BLE001
                self._log_once(
                    "cdp", f"[douyin-worker] 飞鸽配对端口暂不可用（{type(exc).__name__}），等待重连。"
                )
                await asyncio.sleep(5)
                continue
            if target is None:
                self._log_once("page", "[douyin-worker] 请登录抖店并打开飞鸽客服会话页。")
                await asyncio.sleep(5)
                continue

            session: CdpSession | None = None
            try:
                session = CdpSession(target["webSocketDebuggerUrl"])
                print(f"[douyin-worker] 已连接飞鸽: {target.get('title', '')}")
                baseline = session.evaluate(_last_customer_js())
                if baseline:
                    contact = baseline.get("customer_id") or "feige-active"
                    self.initialized_contacts.add(contact)
                    self.seen.add(baseline.get("hash", ""))
                while self._running:
                    if not await asyncio.get_running_loop().run_in_executor(None, session.is_alive):
                        break
                    await self._poll_once(session)
                    await asyncio.sleep(self.poll_interval)
            except Exception as exc:  # noqa: BLE001
                self._log_once(
                    "session", f"[douyin-worker] 飞鸽连接中断（{type(exc).__name__}），准备重连。"
                )
            finally:
                if session is not None:
                    session.close()
                await asyncio.sleep(3)

    async def _poll_once(self, session: CdpSession) -> None:
        opened_unread = False
        if self.auto_open_unread:
            opened_unread = bool(session.evaluate(_open_unread_js()))
            if opened_unread:
                await asyncio.sleep(0.35)
        message = session.evaluate(_last_customer_js())
        if not message:
            return
        customer_id = (message.get("customer_id") or "feige-active").strip()
        fingerprint = message.get("hash", "")
        text = message.get("text", "").strip()
        if not text or not fingerprint:
            return

        # 第一次看到一个新会话时只建立基线，绝不回复加载出来的历史消息。
        if customer_id not in self.initialized_contacts:
            self.initialized_contacts.add(customer_id)
            self.seen.add(fingerprint)
            if not opened_unread:
                print(f"[douyin-worker] 已为会话 {customer_id[:20]} 建立历史消息基线。")
                return
            # 明确由未读角标打开的新会话：最后一条就是待处理消息，不作为历史吞掉。
            self.seen.discard(fingerprint)

        await self._poll_outbox(session, customer_id)
        if fingerprint in self.seen or text in self.sent_texts:
            return
        self.seen.add(fingerprint)
        print(f"[douyin-worker] 新顾客消息（{customer_id[:20]}）: {text[:50]}")

        payload = {
            "text": text,
            "customer_id": customer_id,
            "message_id": message.get("dom_id") or fingerprint,
            "tenant_id": self.tenant_id,
            "store_id": self.store_id,
            "suppress_intro": True,
        }
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self.decision_url}/platforms/douyin/decide",
                headers=self.headers,
                json=payload,
            )
            response.raise_for_status()
            decision = response.json()

        reply = (decision.get("reply") or "").strip()
        if decision.get("status") == "taken_over":
            await self._notify(session, f"需要人工接管：{text[:35]}", critical=True)
            return
        if decision.get("needs_approval") or decision.get("status") == "pending_approval":
            if decision.get("send_before_handoff") and reply:
                await self._send(session, reply)
            await self._notify(session, f"有回复待人工审批：{reply[:35] or text[:35]}")
            return
        if reply:
            if await self._send(session, reply):
                print(f"[douyin-worker] 已发送: {reply[:50]}")

    async def _poll_outbox(self, session: CdpSession, customer_id: str) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{self.decision_url}/v1/outbox/pull",
                headers=self.headers,
                params={"channel": self.channel, "customer_id": customer_id},
            )
            response.raise_for_status()
            items = response.json().get("outbox", [])
            for item in items:
                content = (item.get("content") or "").strip()
                if not content or item.get("customer_id") != customer_id:
                    continue
                if not await self._send(session, content):
                    continue
                await client.post(
                    f"{self.decision_url}/v1/outbox/{item['id']}/ack",
                    headers=self.headers,
                    json={"status": "sent"},
                )
                print(f"[douyin-worker] 已发送人工审批回复: {content[:50]}")

    async def _send(self, session: CdpSession, reply: str) -> bool:
        reply = reply.strip()
        if not reply or not session.evaluate(_fill_js(reply)):
            self._log_once("input", "[douyin-worker] 未找到飞鸽输入框，已停止发送。")
            return False
        await asyncio.sleep(0.15)
        if not session.evaluate(_CLICK_SEND_JS):
            self._log_once("send", "[douyin-worker] 未找到可用的“发送”按钮，已停止发送。")
            return False
        self.sent_texts.add(reply)
        return True

    async def _notify(self, session: CdpSession, message: str, critical: bool = False) -> None:
        overlay = r"""
(() => {
  let el = document.getElementById('__douyin_agent_alert__');
  if (!el) { el = document.createElement('div'); el.id = '__douyin_agent_alert__'; document.body.appendChild(el); }
  el.style.cssText = 'position:fixed;top:14px;right:18px;z-index:2147483647;background:#ff2c55;color:white;padding:12px 18px;border-radius:10px;font-weight:600;box-shadow:0 4px 18px #0005;max-width:380px';
  el.textContent = __MESSAGE__; setTimeout(() => el.remove(), 10000); return true;
})()
""".replace("__MESSAGE__", json.dumps(message, ensure_ascii=False))
        try:
            session.evaluate(overlay)
        except Exception:  # noqa: BLE001
            pass
        title = "抖店飞鸽需要人工处理" if critical else "抖店飞鸽有待审批回复"
        if platform.system() == "Darwin":
            safe_title = title.replace('"', "")
            safe_message = message.replace('"', "")
            subprocess.run(
                ["osascript", "-e", f'display notification "{safe_message}" with title "{safe_title}" sound name "Glass"'],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def stop(self) -> None:
        self._running = False


def dump_structure(output_path: str = "data/douyin-feige-structure.json") -> Path:
    """采集有限的 DOM 结构供页面版本校准；不采集登录态和完整消息。"""
    cdp_url = os.environ.get("DOUYIN_CDP_URL", CDP_URL)
    target = find_feige_page(cdp_url)
    if target is None:
        raise RuntimeError("未找到飞鸽客服页，请先登录并打开一个会话")
    session = CdpSession(target["webSocketDebuggerUrl"])
    try:
        data: dict[str, Any] = session.evaluate(_structure_dump_js()) or {}
    finally:
        session.close()
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


async def main() -> None:
    worker = DouyinFeigeWorker(
        decision_url=os.environ.get("DOUYIN_DECISION_URL", DECISION_URL),
        cdp_url=os.environ.get("DOUYIN_CDP_URL", CDP_URL),
        api_key=os.environ.get("XHS_API_KEY"),
        store_id=os.environ.get("DOUYIN_STORE_ID", "STORE-001"),
        tenant_id=os.environ.get("DOUYIN_TENANT_ID", "demo"),
        poll_interval=float(os.environ.get("DOUYIN_POLL_INTERVAL", "2")),
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
