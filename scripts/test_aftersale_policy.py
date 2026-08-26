"""售后风控引擎的离线测试（验证移植的 SKILL.md 规则）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from xhs_kefu.aftersale_policy import AftersalePolicyEngine, NEGOTIATED_REASON_PROMPT

_ORDERS = json.loads(
    (Path("src/xhs_kefu/data/orders.json")).read_text(encoding="utf-8")
)
_orders_by_id = {o["order_id"]: o for o in _ORDERS}


def order(oid: str) -> dict | None:
    return _orders_by_id.get(oid)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    eng = AftersalePolicyEngine()

    cases = [
        # (订单号, 问题类型, 文本, 有证据, 期望verdict)
        ("XHS-20260102-002", "damaged", "衣服破了要补偿", False, "need_evidence"),
        ("XHS-20260102-002", "damaged", "衣服破了要补偿", True, "small_compensate"),
        ("XHS-20260102-002", "wrong_item", "发错货了", True, "small_compensate"),
        ("XHS-20260119-004", "missing_item", "少发了一件", True, "full_refund_gift"),
        ("XHS-20260119-004", "missing_item", "包裹空的没收到货", False, "full_refund_gift"),
        (None, "other", "我要退款", False, "need_clarify"),
        ("XHS-20260103-003", "quality", "质量不行要退", True, "small_compensate"),
    ]

    for oid, issue, text, has_evidence, expect in cases:
        o = order(oid) if oid else None
        d = eng.evaluate(order=o, issue_type=issue, user_text=text, has_evidence=has_evidence)
        ok = "✅" if d.verdict == expect else f"❌(期望{expect})"
        print(f"{ok} [{oid or '无订单'}] {text} → {d.verdict}")
        print(f"    金额=¥{d.amount_cents/100:.2f} 话术={d.message[:60].replace(chr(10),' ')}")

    # 单独验证 3元→5元 阶梯
    print("\n=== 3元→5元 赔偿阶梯 ===")
    low = eng.rule.allowed_amount(1299, upgrade=False)  # 实付12.99 < 15
    mid_default = eng.rule.allowed_amount(3999, upgrade=False)  # 实付39.99
    mid_upgrade = eng.rule.allowed_amount(3999, upgrade=True)
    print(f"实付¥12.99 默认建议: ¥{low/100:.2f}（应≤3元且≤实付）")
    print(f"实付¥39.99 默认建议: ¥{mid_default/100:.2f}（应3元）")
    print(f"实付¥39.99 升级建议: ¥{mid_upgrade/100:.2f}（应5元）")

    # 验证「与商家协商一致」话术存在
    print("\n=== 与商家协商一致话术 ===")
    print("含关键词'与商家协商一致':", "与商家协商一致" in NEGOTIATED_REASON_PROMPT)
    print("含'系统自动审批':", "系统自动审批" in NEGOTIATED_REASON_PROMPT)


if __name__ == "__main__":
    main()
