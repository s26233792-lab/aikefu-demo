"""验证 AftersaleAgent 接入售后风控引擎后的完整决策。"""
from __future__ import annotations

import sys

sys.path.insert(0, "src")

from xhs_kefu.mvp.agents import AftersaleAgent
from xhs_kefu.mvp.rag import RAG
from xhs_kefu.mvp.tools_api import ToolRegistry

agent = AftersaleAgent(RAG(), ToolRegistry())


def run(text, customer_id="CUST-9004", order_id="", attachments=False):
    ctx = {"customer_id": customer_id, "order_id": order_id, "attachments": attachments}
    return agent.answer(text, ctx)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    cases = [
        ("我要退款", "CUST-9002", "", False),
        ("衣服破了要补偿", "CUST-9003", "XHS-20260102-002", False),
        ("衣服破了要补偿", "CUST-9003", "XHS-20260102-002", True),
        ("少发了一件", "CUST-9004", "XHS-20260119-004", True),
        ("帮我改地址", "CUST-9002", "XHS-20260101-001", False),
        ("物流到哪了", "CUST-9002", "XHS-20260101-001", False),
    ]
    for text, cid, oid, att in cases:
        r = run(text, cid, oid, att)
        print(f"[{oid or '无订单'}] {text} → {r.disposition} ({r.reason_code})")
        print(f"    {r.text[:70].replace(chr(10), ' ')}")
        print()


if __name__ == "__main__":
    main()
