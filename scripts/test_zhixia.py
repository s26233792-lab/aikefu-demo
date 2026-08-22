"""栀夏 ZHIXIA Agent 冒烟测试（用 agent.md 第 13 节演示问题）。"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "src")

from xhs_kefu.config import Settings
from xhs_kefu.zhixia_agent import ZhixiaLLMAgent
from xhs_kefu.zhixia_runtime import ZhixiaRuntime

CASES = [
    "您好",
    "我 158 cm、52 kg，梨形身材，想买一套面试穿的，预算 800 元。",
    "阔腿裤 M 码我能穿吗？腰围 70、臀围 95。",
    "我想要不透的白衬衫，有推荐吗？",
    "订单 ZX202608200147，手机号后四位 7319，帮我查物流。",
    "订单 ZX202608210083 想改地址。",
    "订单 ZX202608170219 的退款怎么还没到？",
    "裙子穿过一天了，不喜欢，能退吗？",
    "两件 95 折和满减一起用，西装加阔腿裤多少钱？",
]


async def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    s = Settings.from_env()
    llm = ZhixiaLLMAgent(base_url=s.llm_base_url, model=s.llm_model, api_key=s.llm_api_key)
    rt = ZhixiaRuntime(llm_agent=llm)

    history: list[dict[str, str]] = []
    for text in CASES:
        r = await rt.handle(text=text, history=history)
        mark = "🔴人工" if r["needs_human"] else "🟢自动"
        print(f"{mark} [{r['tone']}] {text}")
        if r["reply"]:
            print(f"    → {r['reply'][:120].replace(chr(10), ' ')}")
        else:
            print(f"    → (转人工: {r.get('handoff_reason', '')})")
        # 记录到历史（模拟会话）
        if r["reply"] and r["tone"] == "normal":
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": r["reply"]})
        print()


if __name__ == "__main__":
    asyncio.run(main())
