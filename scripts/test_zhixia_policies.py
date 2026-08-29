"""Regression checks for store-policy routing and fulfillment boundaries."""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "src")

from xhs_kefu.zhixia_rules import ZhixiaRuleAgent
from xhs_kefu.zhixia_tools import ZhixiaTools


def main() -> None:
    tools = ZhixiaTools()
    agent = ZhixiaRuleAgent(tools)

    policy = tools.policy_lookup("现货和预售一起下单会拆包吗")
    assert any(section["title"] == "发货与履约" for section in policy["sections"])

    shipping = agent.run("你好，现货一般什么时候发？")
    assert shipping["intent"] == "shipping_policy"
    assert "24 小时" in shipping["reply"]
    assert any(call["name"] == "shop_policy_lookup" for call in shipping["tool_calls"])

    mixed = agent.run("现货和预售一起买，会先发现货吗？")
    assert mixed["intent"] == "shipping_policy"
    assert "最晚预售日期" in mixed["reply"]

    late_stock = tools.order_lookup("ZX202608210083", "1654")
    assert late_stock is not None
    assert late_stock["fulfillment_freshness"]["over_48h_unshipped"] is True

    preorder = tools.order_lookup("ZX202608190066", "3387")
    assert preorder is not None
    assert preorder["fulfillment_freshness"]["is_preorder"] is True
    assert preorder["fulfillment_freshness"]["over_48h_unshipped"] is False

    assert tools.order_lookup("ZX202608200147")["error"] == "verify_required"
    assert tools.cancel_order("ZX202608210083", "0000")["error"] == "verify_failed"
    assert tools.cancel_order("ZX202608200147", "7319")["error"] == "shipped_cannot_cancel"

    class FakeExceptionAgent:
        async def run(self, **_: object) -> dict:
            logistics = tools.logistics_lookup("ZX202608200147", "7319")
            return {
                "reply": "目前包裹**正在派送**。我建议为您登记催件并提交人工复核。您看需要我帮您处理吗？这种情况我会为您提交人工专员复核，帮您跟进。",
                "tool_calls": [{"name": "logistics_lookup", "result": logistics}],
            }

    from xhs_kefu.zhixia_runtime import ZhixiaRuntime

    exception = asyncio.run(
        ZhixiaRuntime(llm_agent=FakeExceptionAgent(), tools=tools).handle(
            text="订单 ZX202608200147，手机号后四位 7319 到哪里了？"
        )
    )
    assert exception["send_before_handoff"] is True
    assert "已提交人工专员复核" in exception["reply"]
    assert "需要我帮您处理吗" not in exception["reply"]
    assert "我会为您提交" not in exception["reply"]
    assert "**" not in exception["reply"]

    print("zhixia policy checks: ok")


if __name__ == "__main__":
    main()
