"""MVP 多 Agent 链路的离线冒烟测试（无需 LLM、无需千帆）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from xhs_kefu.mvp.adapter import PlatformMessage
from xhs_kefu.mvp.pipeline import MVPPipeline

CASES = [
    # (平台, 文案, 期望路由)
    ("qianfan", "多久能发货", "faq"),
    ("qianfan", "可以开发票吗", "faq"),
    ("douyin", "有什么外套推荐", "product"),
    ("qianniu", "这件外套多少钱", "product"),
    ("qianfan", "SKU-MUG-BLUE 什么材质", "product"),
    ("qianfan", "我的订单 XHS-20260101-001 物流到哪了", "aftersale"),
    ("qianfan", "我要退款", "aftersale"),
    ("qianfan", "衣服破了，要补偿", "aftersale"),
    ("qianfan", "我要投诉你们", "handoff"),
    ("douyin", "气死我了，太差了", "handoff"),
]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    pipeline = MVPPipeline()

    for platform, text, expect_route in CASES:
        msg = PlatformMessage(
            platform=platform, store_id="STORE-001",
            customer_id=f"CUST-{expect_route}", message_id="m1", text=text,
        )
        r = pipeline.process(msg)
        ok = "✅" if r.route == expect_route else f"❌(期望{expect_route})"
        print(f"{ok} [{r.route} → {r.agent}] {text}")
        print(f"   处置={r.disposition} 回复={r.reply[:50].replace(chr(10), ' ')}")
        if r.facts:
            print(f"   facts={r.facts}")
        print()


if __name__ == "__main__":
    main()
