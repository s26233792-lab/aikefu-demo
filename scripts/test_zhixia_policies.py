"""Regression checks for store-policy routing and fulfillment boundaries."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, "src")

from xhs_kefu.zhixia_rules import ZhixiaRuleAgent
from xhs_kefu.zhixia_agent import load_agent_rules
from xhs_kefu.zhixia_tools import ZhixiaTools


def main() -> None:
    tools = ZhixiaTools()
    agent = ZhixiaRuleAgent(tools)

    agent_rules = load_agent_rules()
    assert agent_rules == Path("agent.md").read_text(encoding="utf-8").strip()
    assert "request_human_review" in agent_rules
    assert "发送前强制自检" in agent_rules

    policy = tools.policy_lookup("现货和预售一起下单会拆包吗")
    assert any(section["title"] == "发货与履约" for section in policy["sections"])
    budget_policy = tools.policy_lookup("预算800，实付多少钱")
    assert any(section["title"] == "优惠、价保与优惠计算" for section in budget_policy["sections"])

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
    demo_order = tools.order_lookup("123456", "7658")
    assert demo_order is not None and demo_order.get("order_id") == "123456"
    assert demo_order["paid_cents"] == 60310
    assert tools.cancel_order("ZX202608210083", "0000")["error"] == "verify_failed"
    assert tools.cancel_order("ZX202608200147", "7319")["error"] == "shipped_cannot_cancel"

    class FakeExceptionAgent:
        async def run(self, **_: object) -> dict:
            logistics = tools.logistics_lookup("ZX202608200147", "7319")
            return {
                "reply": "目前包裹**正在派送**。我建议为您登记催件并提交人工复核。您看需要我帮您处理吗？这种情况我会为您提交人工专员复核，帮您跟进。我也会一并提交给专员核实。",
                "tool_calls": [{"name": "logistics_lookup", "result": logistics}],
            }

    from xhs_kefu.zhixia_runtime import ZhixiaRuntime, naturalize_customer_reply

    naturalized = naturalize_customer_reply("我可以帮您留意发货情况，有需要随时找我。")
    assert "我可以帮您留意发货情况" not in naturalized
    assert "订单页留意发货状态" in naturalized
    assert "\n- " not in naturalize_customer_reply("推荐：\n- 白衬衫\n- 西装")
    assert "· 白衬衫" in naturalize_customer_reply("推荐：\n- 白衬衫")
    assert "1、白衬衫" in naturalize_customer_reply("推荐：\n1. 白衬衫")
    assert "这个" not in naturalize_customer_reply("明天到货，这个我无法保证。")

    exception = asyncio.run(
        ZhixiaRuntime(llm_agent=FakeExceptionAgent(), tools=tools).handle(
            text="订单 ZX202608200147，手机号后四位 7319 到哪里了？"
        )
    )
    assert exception["send_before_handoff"] is True
    assert "已提交人工专员复核" in exception["reply"]
    assert "需要我帮您处理吗" not in exception["reply"]
    assert "我会为您提交" not in exception["reply"]
    assert "我也会一并提交" not in exception["reply"]
    assert "**" not in exception["reply"]

    class FakeHumanReviewAgent:
        async def run(self, **_: object) -> dict:
            logistics = tools.logistics_lookup("ZX202608200147", "7319")
            return {
                "reply": "相关情况需要人工核实，已提交人工专员复核。",
                "tool_calls": [
                    {"name": "logistics_lookup", "result": logistics},
                    {
                        "name": "request_human_review",
                        "args": {"reason": "少件争议"},
                        "result": {"ok": True, "queued": True},
                    },
                ],
            }

    human_review = asyncio.run(
        ZhixiaRuntime(llm_agent=FakeHumanReviewAgent(), tools=tools).handle(
            text="订单信息好像对不上，麻烦帮我核实一下"
        )
    )
    assert human_review["needs_human"] is True
    assert human_review["intent"] == "human_review"
    assert human_review["handoff_reason"] == "少件争议"
    assert human_review["send_before_handoff"] is False

    class FakeLookupRetryAgent:
        async def run(self, **_: object) -> dict:
            return {
                "reply": "暂未查询到这笔订单，请核对订单号和手机号后四位后重新发送。",
                "tool_calls": [{"name": "order_lookup", "result": None}],
            }

    lookup_retry = asyncio.run(
        ZhixiaRuntime(llm_agent=FakeLookupRetryAgent(), tools=tools).handle(
            text="订单号 654321，手机号后四位 7658"
        )
    )
    assert lookup_retry["needs_human"] is False
    assert lookup_retry["intent"] == "order_verification_retry"
    assert lookup_retry["disposition"] == "auto_reply"

    print("zhixia policy checks: ok")


if __name__ == "__main__":
    main()
