"""千帆桌面端（Electron）—— CDP 自动收发 Worker。

通过 --remote-debugging-port=9222 连接已登录的千帆客服工作台桌面端，
轮询当前会话的新顾客消息 → 调用决策 API → 回填回复到输入框并发送。

真实的千帆客服工作台 DOM 结构（已按 2026-08-19 实测校准）：
- 输入框：textarea.reply-textarea（placeholder「按Enter发送消息...」）
- 发送按钮：button（文本「发送」，空输入时为 disabled）
- 会话列表：.im-chat-list-box .contact-list .chat-item（active 表示当前会话）
- 消息区：.im-theme.msg-list-box，每条消息 .msg-row > .msg-wrap > .msg-wrap-content
- 顾客消息特征：短文本，无「客服机器人」「接入会话」等系统前缀

边界：
- 只读 + 自动回复；写操作（退款/改址/拦截）不上自动执行，上报审批队列；
- 只处理「当前激活会话」的新顾客消息，不跨会话误发。
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import json
import os
import time
from typing import Any

import httpx

try:
    from .cdp_client import CdpSession, find_cstools_page
    from .notifier import notify as system_notify
except ImportError:  # 兼容直接执行 python workers/qianfan_cdp_worker.py
    from cdp_client import CdpSession, find_cstools_page
    from notifier import notify as system_notify

# 决策 API 地址
DECISION_URL = "http://127.0.0.1:18081"

# 店铺名（客服消息开头会带店铺名 + 时间 + 已读；顾客消息不带）
STORE_NAME = os.environ.get("XHS_QIANFAN_STORE_NAME", "").strip()

# 系统消息特征（不是顾客消息，需过滤）
SYSTEM_PREFIXES = ("客服机器人", "接入会话", "匹配到主要问法", "会话长时间无新消息", "系统")

# 日志去重：避免同一错误无限刷屏
_log_state: dict[str, str] = {}


def _normalize_customer_id(value: Any) -> str:
    return str(value or "").strip()


def _message_key(customer_id: str, fingerprint: str) -> str:
    """消息去重必须包含顾客，避免不同会话的相同文本互相覆盖。"""
    return f"{_normalize_customer_id(customer_id) or 'unknown'}|{fingerprint}"


def _remember(cache: OrderedDict[str, None], key: str, *, limit: int = 5000) -> None:
    """记录近期项目并限制内存，适合长期运行的客服进程。"""
    cache[key] = None
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


def _once(msg: str, key: str | None) -> str:
    """返回消息，若与上次相同则返回空串（调用方决定是否打印）。"""
    k = key or msg
    if _log_state.get(k) == msg:
        return ""
    _log_state[k] = msg
    return msg


def _log_once(msg: str, key: str | None) -> None:
    """仅当消息变化时打印一次。"""
    out = _once(msg, key)
    if out:
        print(out)


def _is_customer_message(text: str) -> bool:
    """判断一条消息是否是顾客发的。

    真实千帆桌面端的消息规律（实测）：
    - 客服消息：`Icetea冻柠 | 23:44 | 已读 | 回复内容`（带店铺名 + 已读）
    - 顾客消息：`23:44 | 请问多少钱 | 23:44`（纯时间戳 + 内容，无店铺名）
    - 系统消息：`Icetea冻柠接入会话`、`匹配到主要问法...`
    """
    t = text.strip()
    if not t:
        return False
    # 排除带"已读"标记的（客服发出的消息）
    if "已读" in t:
        return False
    # 排除系统前缀
    store_prefixes = (STORE_NAME,) if STORE_NAME else ()
    for prefix in SYSTEM_PREFIXES + store_prefixes:
        if t.startswith(prefix):
            return False
    # 排除纯状态
    if t in ("已读", "发送", "") or (STORE_NAME and t == STORE_NAME):
        return False
    return True


class QianfanCdpWorker:
    """千帆桌面端 CDP Worker。"""

    def __init__(
        self,
        *,
        decision_url: str = DECISION_URL,
        api_key: str | None = None,
        store_id: str = "STORE-001",
        tenant_id: str = "demo",
        channel: str = "xhs_qianfan_desktop",
        poll_interval: float = 2.0,
    ) -> None:
        self.decision_url = decision_url
        self.api_key = api_key
        self.store_id = store_id
        self.tenant_id = tenant_id
        self.channel = channel
        self.poll_interval = poll_interval
        self.seen: OrderedDict[str, None] = OrderedDict()
        self.sent_texts: OrderedDict[str, None] = OrderedDict()
        self.delivered_outbox: OrderedDict[str, None] = OrderedDict()
        self._running = False

    async def decide(self, text: str, customer_id: str) -> dict | None:
        # 调用栀夏 ZHIXIA Agent（真实千帆消息 → 栀夏小栀回复）
        payload = {
            "text": text,
            "session_key": customer_id or "unknown",
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{self.decision_url.rstrip('/')}/zhixia/decide", headers=headers, json=payload
            )
            resp.raise_for_status()
            return resp.json()

    async def run(self) -> None:
        """主循环：连 CDP，轮询新顾客消息；断连时自动重连。"""
        self._running = True
        last_error: str | None = None  # 用于抑制重复错误刷屏

        while self._running:
            try:
                target = find_cstools_page()
            except Exception as e:  # noqa: BLE001
                # CDP 连不上（千帆客户端没带调试端口/已关闭），等待重试而非崩溃
                _log_once(f"[cdp-worker] 无法连接千帆 CDP（{type(e).__name__}），5 秒后重试...", "cdp_down")
                await asyncio.sleep(5)
                continue
            if target is None:
                _log_once("[cdp-worker] 未找到客服工作台页面，5 秒后重试...", None)
                await asyncio.sleep(5)
                continue

            ws_url = target["webSocketDebuggerUrl"]
            session = None
            try:
                session = CdpSession(ws_url)
                print(f"[cdp-worker] 连接客服工作台: {target.get('title')}")
                # 启动基线：把当前已有的顾客消息都标记为"已见"，只响应此后新消息
                baseline = session.evaluate(f"JSON.stringify({_LAST_CUSTOMER_JS})")
                if baseline:
                    obj = json.loads(baseline)
                    if obj and obj.get("hash"):
                        contact = _normalize_customer_id(session.evaluate(_CURRENT_CONTACT_JS)) or "unknown"
                        _remember(self.seen, _message_key(contact, obj["hash"]))
                        print("[cdp-worker] 已记录当前会话基线消息，仅响应后续新消息。")
                print(f"[cdp-worker] 开始轮询新顾客消息（每 {self.poll_interval}s）...")

                while self._running:
                    # 心跳检测：连接断了就跳出内层循环，触发重连
                    if not await self._session_alive(session):
                        print("[cdp-worker] CDP 连接已断开，尝试重新连接...")
                        break

                    try:
                        await self._poll_once(session)
                    except (ConnectionError, OSError) as e:
                        # 连接类错误：跳出重连
                        print(f"[cdp-worker] 连接异常，准备重连: {type(e).__name__}")
                        break
                    except Exception as e:  # noqa: BLE001
                        _log_once(f"[cdp-worker] 轮询异常: {type(e).__name__}", "poll_err")

                    # outbox 低频轮询（每 8 个 poll 周期一次，避免刷屏）
                    self._outbox_counter = getattr(self, "_outbox_counter", 0) + 1
                    if self._outbox_counter >= 8:
                        self._outbox_counter = 0
                        try:
                            await self._poll_outbox(session)
                        except (ConnectionError, OSError):
                            break
                        except Exception as e:  # noqa: BLE001
                            _log_once(f"[cdp-worker] outbox 异常: {type(e).__name__}", "outbox_err")

                    await asyncio.sleep(self.poll_interval)
            except Exception as e:  # noqa: BLE001
                print(f"[cdp-worker] 建立连接失败: {type(e).__name__}")
            finally:
                if session is not None:
                    session.close()
                await asyncio.sleep(4)  # 重连间隔，避免疯狂重试

    async def _session_alive(self, session: CdpSession) -> bool:
        """在事件循环里检测 CDP 会话是否还活着。"""
        try:
            return await asyncio.get_event_loop().run_in_executor(None, session.is_alive)
        except Exception:  # noqa: BLE001
            return False

    async def _poll_outbox(self, session: CdpSession) -> None:
        """轮询待发送队列，把人工审批通过/手写的回复回填到千帆。"""
        items = await self._pull_outbox_items()
        for item in items:
            content = item.get("content", "")
            if not content:
                continue
            oid = str(item.get("id") or "")
            target_customer = _normalize_customer_id(item.get("customer_id"))
            if not oid or not target_customer or target_customer == "unknown":
                _log_once("[cdp-worker] outbox 缺少有效的消息 ID 或顾客标识，已保留待人工核对。", "outbox_invalid_target")
                continue

            # 已成功填入千帆但确认回执失败时，只重试 ACK，避免同一进程内重复发送。
            if oid not in self.delivered_outbox:
                current_customer = _normalize_customer_id(session.evaluate(_CURRENT_CONTACT_JS))
                if current_customer != target_customer:
                    _log_once(
                        f"[cdp-worker] 待发回复属于「{target_customer}」，当前会话为「{current_customer or '未知'}」，暂不发送。",
                        f"outbox_wait:{oid}",
                    )
                    continue
                sent = await self._send(session, content, expected_customer_id=target_customer)
                if not sent:
                    _log_once(f"[cdp-worker] outbox {oid} 回填失败，将稍后重试。", f"outbox_send:{oid}")
                    continue
                _remember(self.delivered_outbox, oid)
                print(f"[cdp-worker] 已回填人工回复到千帆: {content[:40]}...")

            await self._ack_outbox_item(oid)

    async def _pull_outbox_items(self) -> list[dict[str, Any]]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{self.decision_url.rstrip('/')}/v1/outbox/pull", headers=headers
            )
            resp.raise_for_status()
            return list(resp.json().get("outbox", []))

    async def _ack_outbox_item(self, oid: str) -> None:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        async with httpx.AsyncClient(timeout=10.0) as client:
            ack = await client.post(
                f"{self.decision_url.rstrip('/')}/v1/outbox/{oid}/ack",
                json={"status": "sent"},
                headers=headers,
            )
            ack.raise_for_status()

    async def _poll_once(self, session: CdpSession) -> None:
        # 1. 读当前会话的顾客昵称
        contact = session.evaluate(_CURRENT_CONTACT_JS)
        # 2. 读消息区最后一条真正的顾客消息（方向判定版）
        last_customer = session.evaluate(_LAST_CUSTOMER_JS)
        if not last_customer:
            return
        text = last_customer.get("text", "").strip()
        if not text or not _is_customer_message(text):
            return
        fingerprint = str(last_customer.get("hash", ""))
        customer_id = _normalize_customer_id(contact) or "unknown"
        message_key = _message_key(customer_id, fingerprint)
        if message_key in self.seen:
            return
        stripped = text.strip()
        # 核心防复读：如果这条文本是我自己刚发出去过的，跳过（不是顾客消息）
        if _message_key(customer_id, stripped) in self.sent_texts:
            _remember(self.seen, message_key)
            return
        print(f"[cdp-worker v2] 检测到新顾客消息: {text[:40]}")

        # 决策接口失败时不要提前标记为已处理；下一轮会重试，不会静默丢消息。
        decision = await self.decide(text, customer_id)
        if not decision:
            return
        # 人工接管中的会话：不自动回复，但在真实千帆里弹提醒 + 系统级强制提醒
        if decision.get("status") == "taken_over":
            print(f"[cdp-worker v2] 会话已人工接管，不自动回复: {text[:30]}...")
            await self._notify_in_qianfan(session, f"🙋 需要人工接管：{text[:30]}")
            system_notify("critical")  # 置顶 + 任务栏持续闪烁 + 声音
            _remember(self.seen, message_key)
            return
        reply = decision.get("reply", "")
        # 需人工审批（高风险写操作/敏感内容）：不自动发送，进入待审队列，并弹提醒
        if decision.get("needs_approval") or decision.get("status") == "pending_approval":
            print(f"[cdp-worker v2] 回复需人工审批，已入待审队列（{decision.get('moderation_id', '?')}）: {reply[:40]}...")
            await self._notify_in_qianfan(session, f"⚠️ 需人工审批：{reply[:40]}")
            system_notify("warning")  # 置顶 + 闪烁 + 声音
            _remember(self.seen, message_key)
            return
        if not reply:
            print("[cdp-worker v2] 无可直接发送的回复，跳过。")
            _remember(self.seen, message_key)
            return
        sent = await self._send(session, reply, expected_customer_id=customer_id)
        if sent:
            _remember(self.seen, message_key)
            print(f"[cdp-worker v2] 已发送回复: {reply[:40]}...")
        else:
            print("[cdp-worker v2] 回复未成功发送，将在下一轮重试。")

    async def _notify_in_qianfan(self, session: CdpSession, message: str) -> None:
        """在真实千帆窗口内注入醒目浮层提醒（不破坏千帆 DOM，可自动消失）。"""
        js = f"""
(() => {{
  const id = '__xhs_cs_alert__';
  let el = document.getElementById(id);
  if (!el) {{
    el = document.createElement('div');
    el.id = id;
    el.style.cssText = 'position:fixed;top:12px;right:16px;z-index:99999;'
      + 'background:#ff2442;color:#fff;padding:12px 18px;border-radius:10px;'
      + 'font-size:14px;font-weight:600;box-shadow:0 4px 16px rgba(0,0,0,0.3);'
      + 'max-width:360px;line-height:1.5;transition:opacity 0.3s;';
    document.body.appendChild(el);
  }}
  el.textContent = {json.dumps(message, ensure_ascii=False)};
  el.style.opacity = '1';
  // 8 秒后淡出
  setTimeout(() => {{ el.style.opacity = '0'; }}, 8000);
  // 标题闪烁提醒
  const orig = document.title;
  let n = 0;
  const t = setInterval(() => {{
    document.title = (n++ % 2 === 0) ? '⚠️ 有消息待处理' : orig;
    if (n > 10) {{ clearInterval(t); document.title = orig; }}
  }}, 600);
  return true;
}})()
"""
        try:
            session.evaluate(js)
            print("[cdp-worker] 已在真实千帆窗口内弹提醒。")
        except Exception as e:  # noqa: BLE001
            print(f"[cdp-worker] 千帆弹提醒失败（非致命）: {type(e).__name__}")

    async def _send(
        self,
        session: CdpSession,
        reply: str,
        *,
        expected_customer_id: str | None = None,
    ) -> bool:
        """把回复填入 textarea.reply-textarea 并触发发送。"""
        reply = reply.strip()
        if not reply:
            print("[cdp-worker] 回复为空，跳过发送。")
            return False
        expected = _normalize_customer_id(expected_customer_id)
        if expected:
            active = _normalize_customer_id(session.evaluate(_CURRENT_CONTACT_JS))
            if active != expected:
                print(f"[cdp-worker] 会话已切换为「{active or '未知'}」，取消向「{expected}」发送。")
                return False
        # 填入文本（textarea 用 value + input 事件触发框架响应）
        ok = session.evaluate(
            _FILL_JS.replace("__REPLY__", json.dumps(reply, ensure_ascii=False))
        )
        if not ok:
            print("[cdp-worker] 填入输入框失败，跳过发送。")
            return False
        if expected:
            active = _normalize_customer_id(session.evaluate(_CURRENT_CONTACT_JS))
            if active != expected:
                session.evaluate(_CLEAR_INPUT_JS)
                print(f"[cdp-worker] 填入后检测到会话切换，已清空输入框并取消发送。")
                return False
        # 点击发送按钮（非 disabled）
        clicked = session.evaluate(_CLICK_SEND_JS)
        if not clicked:
            # 兜底：模拟回车
            clicked = bool(session.evaluate(
                "(() => { const ta = document.querySelector('textarea.reply-textarea'); "
                "if(!ta) return false; ta.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', bubbles:true})); return true; })()"
            ))
            if clicked:
                print("[cdp-worker] 已用回车兜底发送。")
        if not clicked:
            print("[cdp-worker] 未找到可用的发送按钮或输入框。")
            return False
        _remember(self.sent_texts, _message_key(expected or "unknown", reply), limit=1000)
        return True

    def stop(self) -> None:
        self._running = False


_CURRENT_CONTACT_JS = r"""
(() => {
  const el = document.querySelector('.current-contact, .user-info');
  return el ? (el.innerText || '').split('\n')[0].trim() : '';
})()
"""


# 读取最后一条真正的顾客消息（用 flex 方向判定，可靠区分客服/顾客）
# 实测千帆 DOM：顾客消息 msg-wrap-status-row 的 justify-content=flex-start（左）
#                 客服/机器人消息 justify-content=flex-end（右）+ 含 status-box
_LAST_CUSTOMER_JS = r"""
(() => {
  const rows = Array.from(document.querySelectorAll('.msg-list-box .msg-row'));
  if (!rows.length) return null;
  const sysPrefix = ['客服机器人','接入会话','匹配到主要问法','会话长时间无新消息','系统'];
  // 从后往前找顾客消息
  for (let i = rows.length - 1; i >= 0; i--) {
    const row = rows[i];
    const statusRow = row.querySelector('.msg-wrap-status-row');
    if (statusRow) {
      const jc = getComputedStyle(statusRow).justifyContent;
      // 右对齐(flex-end)=客服/机器人发；左对齐(flex-start)=顾客发
      if (jc === 'flex-end') continue;
    }
    const raw = (row.innerText || '').trim();
    if (!raw) continue;
    if (sysPrefix.some(p => raw.startsWith(p))) continue;
    if (raw.includes('已读')) continue;  // 双保险
    // 顾客消息格式：`时间 | 内容 | 时间` 或 `内容 | 时间`，提取内容
    const parts = raw.split('\n').map(s => s.trim()).filter(s => s);
    const contentParts = parts.filter(s => !/^\d{1,2}:\d{1,2}$/.test(s) && s !== '已读');
    const text = contentParts.join(' ').trim();
    if (!text) continue;
    // 优先采用千帆节点的稳定 ID；缺失时把完整原文和消息位置纳入指纹，
    // 允许同一顾客在不同时间重复发送“你好”等相同文本。
    const stableId = row.getAttribute('data-id') || row.id || (row.dataset && (row.dataset.messageId || row.dataset.id)) || '';
    const fingerprintSource = stableId || `${raw}|${i}`;
    let hash = 0; for (let c of fingerprintSource) { hash = (hash * 31 + c.charCodeAt(0)) >>> 0; }
    return { text, hash: 'm' + hash.toString(16) };
  }
  return null;
})()
"""

# 填入输入框
_FILL_JS = r"""
(() => {
  const ta = document.querySelector('textarea.reply-textarea');
  if (!ta) return false;
  ta.focus();
  // 用原生 setter 触发 React/Vue 响应
  const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
  setter.call(ta, __REPLY__);
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  return true;
})()
"""


_CLEAR_INPUT_JS = r"""
(() => {
  const ta = document.querySelector('textarea.reply-textarea');
  if (!ta) return false;
  const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
  setter.call(ta, '');
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  return true;
})()
"""

# 点击发送按钮（找非 disabled 的"发送"按钮）
_CLICK_SEND_JS = r"""
(() => {
  const btns = Array.from(document.querySelectorAll('button'));
  const send = btns.find(b => (b.innerText || '').trim() === '发送' && !b.className.includes('disabled') && !b.disabled);
  if (!send) return false;
  send.click();
  return true;
})()
"""


async def main() -> None:
    worker = QianfanCdpWorker(
        decision_url=os.environ.get("XHS_DECISION_URL", DECISION_URL),
        api_key=os.environ.get("XHS_API_KEY"),
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
