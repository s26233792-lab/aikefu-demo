"""千帆 CDP Worker 的消息去重、重试和防错发回归测试。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import MethodType


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from workers.qianfan_cdp_worker import (  # noqa: E402
    QianfanCdpWorker,
    _CLICK_SEND_JS,
    _CLEAR_INPUT_JS,
    _CURRENT_CONTACT_JS,
    _FILL_JS,
    _LAST_CUSTOMER_JS,
)
from workers.qianfan_browser import QianfanBrowserWorker, SELECTORS  # noqa: E402


class FakeSession:
    def __init__(self, contact: str = "顾客A") -> None:
        self.contact = contact
        self.last_customer = {"text": "你好", "hash": "same-hash"}
        self.fill_ok = True
        self.click_ok = True
        self.clear_count = 0

    def evaluate(self, expression: str):
        if expression == _CURRENT_CONTACT_JS:
            return self.contact
        if expression == _LAST_CUSTOMER_JS:
            return self.last_customer
        if expression == _CLEAR_INPUT_JS:
            self.clear_count += 1
            return True
        if expression == _CLICK_SEND_JS:
            return self.click_ok
        if "textarea.reply-textarea" in expression and "__REPLY__" not in expression:
            return self.fill_ok
        return True


class FakeAsyncElement:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.filled: list[str] = []
        self.clicks = 0

    async def inner_text(self) -> str:
        return self.text

    async def click(self) -> None:
        self.clicks += 1

    async def fill(self, value: str) -> None:
        self.filled.append(value)


class FakeAsyncPage:
    def __init__(self, contact: str) -> None:
        self.contact = FakeAsyncElement(contact)
        self.input_box = FakeAsyncElement()
        self.send_button = FakeAsyncElement()

    async def query_selector(self, selector: str):
        if selector == SELECTORS["current_contact"]:
            return self.contact
        if selector == SELECTORS["input_box"]:
            return self.input_box
        if selector == SELECTORS["send_btn"]:
            return self.send_button
        return None

    async def wait_for_timeout(self, timeout_ms: int) -> None:
        return None


async def check_customer_scoped_deduplication() -> None:
    worker = QianfanCdpWorker()
    session = FakeSession("顾客A")
    decisions: list[str] = []

    async def decide(self, text: str, customer_id: str):
        decisions.append(customer_id)
        return {"status": "resolved", "reply": "您好"}

    async def send(self, session, reply: str, *, expected_customer_id=None):
        return True

    worker.decide = MethodType(decide, worker)
    worker._send = MethodType(send, worker)
    await worker._poll_once(session)
    session.contact = "顾客B"
    await worker._poll_once(session)
    assert decisions == ["顾客A", "顾客B"]


async def check_transient_failure_retries() -> None:
    worker = QianfanCdpWorker()
    session = FakeSession("顾客A")
    attempts = 0

    async def decide(self, text: str, customer_id: str):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("temporary")
        return {"status": "resolved", "reply": "已恢复"}

    async def send(self, session, reply: str, *, expected_customer_id=None):
        return True

    worker.decide = MethodType(decide, worker)
    worker._send = MethodType(send, worker)
    try:
        await worker._poll_once(session)
    except ConnectionError:
        pass
    else:
        raise AssertionError("首次临时错误应向上抛出供主循环记录")
    assert not worker.seen
    await worker._poll_once(session)
    assert attempts == 2 and worker.seen


async def check_failed_send_retries() -> None:
    worker = QianfanCdpWorker()
    session = FakeSession("顾客A")
    send_results = iter((False, True))
    decisions = 0

    async def decide(self, text: str, customer_id: str):
        nonlocal decisions
        decisions += 1
        return {"status": "resolved", "reply": "稍后重试"}

    async def send(self, session, reply: str, *, expected_customer_id=None):
        return next(send_results)

    worker.decide = MethodType(decide, worker)
    worker._send = MethodType(send, worker)
    await worker._poll_once(session)
    assert not worker.seen
    await worker._poll_once(session)
    assert decisions == 2 and worker.seen


async def check_outbox_customer_guard() -> None:
    worker = QianfanCdpWorker()
    session = FakeSession("顾客B")
    item = {"id": "out-1", "customer_id": "顾客A", "content": "仅发给顾客A"}
    sends: list[tuple[str, str]] = []
    acks: list[str] = []

    async def pull(self):
        return [item]

    async def send(self, session, reply: str, *, expected_customer_id=None):
        sends.append((reply, expected_customer_id))
        return True

    async def ack(self, oid: str):
        acks.append(oid)

    worker._pull_outbox_items = MethodType(pull, worker)
    worker._send = MethodType(send, worker)
    worker._ack_outbox_item = MethodType(ack, worker)
    await worker._poll_outbox(session)
    assert sends == [] and acks == []

    session.contact = "顾客A"
    await worker._poll_outbox(session)
    assert sends == [("仅发给顾客A", "顾客A")]
    assert acks == ["out-1"]

    # ACK 重试不能造成第二次发送。
    await worker._poll_outbox(session)
    assert len(sends) == 1 and acks == ["out-1", "out-1"]


async def check_send_revalidates_contact() -> None:
    worker = QianfanCdpWorker()
    session = FakeSession("顾客B")
    assert await worker._send(session, "不能错发", expected_customer_id="顾客A") is False

    session.contact = "顾客A"
    assert await worker._send(session, "正确回复", expected_customer_id="顾客A") is True


async def check_browser_worker_contact_guard() -> None:
    worker = QianfanBrowserWorker(decision_base_url="http://127.0.0.1:18081")
    page = FakeAsyncPage("顾客B")
    assert await worker._send_reply(page, "不能错发", expected_customer_id="顾客A") is False
    assert page.input_box.filled == [] and page.send_button.clicks == 0

    page.contact.text = "顾客A"
    assert await worker._send_reply(page, "正确回复", expected_customer_id="顾客A") is True
    assert page.input_box.filled == ["正确回复"] and page.send_button.clicks == 1


async def main_async() -> None:
    await check_customer_scoped_deduplication()
    await check_transient_failure_retries()
    await check_failed_send_retries()
    await check_outbox_customer_guard()
    await check_send_revalidates_contact()
    await check_browser_worker_contact_guard()


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main_async())
    print("✅ 千帆 Worker 防丢失、防错发测试通过")


if __name__ == "__main__":
    main()
