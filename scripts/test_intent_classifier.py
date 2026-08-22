"""测试 LLM 意图分类器（区分咨询 vs 诉求、情绪识别）。"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "src")

from xhs_kefu.config import Settings
from xhs_kefu.intent_classifier import IntentClassifier

CASES = [
    "退款规则是什么",          # 应 faq_query
    "我要退款",                # 应 refund_request
    "有什么外套推荐",          # 应 product_query
    "这外套多少钱",            # 应 product_query
    "我的快递到哪了",          # 应 logistics_query
    "衣服破了，你们得赔我",    # 应 compensation_request 或 complaint
    "我要投诉你们",            # 应 complaint
    "你好",                    # 应 chitchat
    "赔我5元",                # 应 compensation_request
    "少发了一件",              # 应 missing_item
    "帮我改下地址",            # 应 address_change
    "今天天气怎么样",          # 应 out_of_scope
]


async def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    s = Settings.from_env()
    cls = IntentClassifier(base_url=s.llm_base_url, model=s.llm_model, api_key=s.llm_api_key)

    for text in CASES:
        r = await cls.classify(text)
        human = "🔴人工" if r.needs_human else "🟢自动"
        print(f"{human} [{r.intent:<20}] conf={r.confidence:.2f}  \"{text}\"")


if __name__ == "__main__":
    asyncio.run(main())
