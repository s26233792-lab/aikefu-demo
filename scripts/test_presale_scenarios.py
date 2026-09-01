"""Regression checks for the complete pre-sale conversation flow."""
from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "src")

from xhs_kefu.zhixia_rules import ZhixiaRuleAgent
from xhs_kefu.zhixia_runtime import ZhixiaRuntime
from xhs_kefu.zhixia_tools import ZhixiaTools
from xhs_kefu.storage import SQLiteStore


def main() -> None:
    tools = ZhixiaTools()
    agent = ZhixiaRuleAgent(tools)

    # 1. 商品参数介绍：必须来自指定 SKU 的商品数据。
    parameters = agent.run("ZX-T315 的材质、版型和洗护是什么？")
    assert parameters["intent"] == "product_question"
    assert "桑蚕丝 30%" in parameters["reply"]
    assert "略宽松直身版" in parameters["reply"]
    assert "不可拧绞" in parameters["reply"]
    assert parameters["tool_calls"][0]["name"] == "product_lookup"

    # 2. 引导下单：缺规格时补问，规格齐全时核库存并引导官方入口。
    clarify = agent.run("ZX-S208 怎么下单？")
    assert clarify["intent"] == "place_order_clarify"
    assert "颜色" in clarify["reply"] and "尺码" in clarify["reply"]
    place_order = agent.run("ZX-S208 黑色 M 码 2 件，怎么买？")
    assert place_order["intent"] == "place_order"
    assert "黑色" in place_order["reply"] and "M码" in place_order["reply"]
    assert "2件" in place_order["reply"]
    assert "当前平台商品页" in place_order["reply"]
    assert "付款成功" in place_order["reply"]

    # 3. 活动告知：按查询时间区分生效与过期活动。
    campaign_policy = tools.policy_lookup(
        "现在有什么活动和满减",
        now=datetime.fromisoformat("2026-09-01T12:00:00+08:00"),
    )
    campaign_status = {item["name"]: item["status"] for item in campaign_policy["campaigns"]}
    assert campaign_status["秋季通勤焕新"] == "active"
    assert campaign_status["夏末通勤季"] == "expired"
    fixed_campaign_lookup = tools.policy_lookup
    tools.policy_lookup = lambda topic: fixed_campaign_lookup(  # type: ignore[method-assign]
        topic, now=datetime.fromisoformat("2026-09-01T12:00:00+08:00")
    )
    campaign_reply = agent.run("现在有什么活动和满减？")
    assert campaign_reply["intent"] == "campaign_active"
    assert "秋季通勤焕新" in campaign_reply["reply"]
    assert "适用商品" in campaign_reply["reply"]
    assert "满 ¥499 减 ¥60" in campaign_reply["reply"]

    # 4. 核对订单：商品、数量、金额、脱敏地址和状态应一次给全。
    summary = agent.run("核对订单 ZX202609010026，手机号后四位 2468")
    assert summary["intent"] == "order_summary"
    for expected in ("轻氧针织开衫", "奶杏M码", "1件", "¥179.00", "文三路***号", "待付款"):
        assert expected in summary["reply"], expected

    # 5. 催付：只计算资格，不发送；验证间隔、次数与状态边界。
    pending_order = tools.order_lookup("ZX202609010026", "2468")
    assert pending_order is not None
    eligible_state = tools._payment_reminder_state(
        pending_order,
        now=datetime.fromisoformat("2026-09-01T12:00:00+08:00"),
    )
    assert eligible_state["eligible"] is True
    limited = dict(pending_order)
    limited["payment_reminder"] = {
        "count_24h": 2,
        "last_at": None,
        "min_interval_minutes": 120,
        "max_24h": 2,
    }
    reminder_state = tools._payment_reminder_state(
        limited,
        now=datetime.fromisoformat("2026-09-01T12:00:00+08:00"),
    )
    assert reminder_state == {
        "eligible": False,
        "reason": "frequency_limit",
        "count_24h": 2,
        "max_24h": 2,
        "min_interval_minutes": 120,
        "payment_deadline": "2026-09-03T23:59:59+08:00",
    }

    # 6. 订单备注：未确认不能写，敏感信息不能写，明确确认后才写入。
    unconfirmed = tools.add_order_note(
        "ZX202609010026", "2468", "周末方便收货", confirmed=False
    )
    assert unconfirmed["error"] == "confirm_required"
    sensitive = tools.add_order_note(
        "ZX202609010026", "2468", "联系手机号 13800138000", confirmed=True
    )
    assert sensitive["error"] == "sensitive_content"
    written = tools.add_order_note(
        "ZX202609010026", "2468", "周末方便收货", confirmed=True
    )
    assert written["ok"] is True
    assert written["fulfillment_guaranteed"] is False
    with tempfile.TemporaryDirectory(prefix="presale-note-") as temp_dir:
        store = SQLiteStore(str(Path(temp_dir) / "notes.db"))
        persisted_tools = ZhixiaTools(store=store)
        persisted = persisted_tools.add_order_note(
            "ZX202609010026", "2468", "包装简洁一些", confirmed=True
        )
        saved_action = store.get_action(persisted["id"])
        assert saved_action is not None
        assert saved_action["action_type"] == "order_note"
        assert saved_action["state"] == "completed"
        assert saved_action["payload"]["note_summary"] == "包装简洁一些"

    # 即使模型错误地把 confirmed 设为 true，运行时也要求顾客当前明确确认。
    runtime = ZhixiaRuntime(tools=ZhixiaTools())
    guarded = runtime._tool_executor(current_text="帮我备注周末方便收货")(
        "add_order_note",
        {
            "order_id": "ZX202609010026",
            "phone_last4": "2468",
            "note": "周末方便收货",
            "confirmed": True,
        },
    )
    assert guarded["error"] == "confirm_required"
    confirmed = runtime._tool_executor(
        current_text="确认",
        history=[{"role": "assistant", "content": "确认将‘周末方便收货’写入订单 ZX202609010026 的备注吗？"}],
    )(
        "add_order_note",
        {
            "order_id": "ZX202609010026",
            "phone_last4": "2468",
            "note": "周末方便收货",
            "confirmed": True,
        },
    )
    assert confirmed["ok"] is True

    class FakeBadNoteAgent:
        async def run(self, **_: object) -> dict:
            return {
                "reply": "订单备注已记录。",
                "tool_calls": [
                    {
                        "name": "add_order_note",
                        "args": {"note": "周末方便收货"},
                        "result": {
                            "ok": False,
                            "error": "confirm_required",
                            "note_summary": "周末方便收货",
                        },
                    }
                ],
            }

    guarded_reply = asyncio.run(
        ZhixiaRuntime(llm_agent=FakeBadNoteAgent(), tools=ZhixiaTools()).handle(
            text="确认",
            history=[{"role": "assistant", "content": "确认将这段内容写入订单备注吗？"}],
            suppress_intro=True,
        )
    )
    assert guarded_reply["intent"] == "order_note_confirm"
    assert "已记录" not in guarded_reply["reply"]
    assert "确认将这段内容写入订单备注吗" in guarded_reply["reply"]

    rules = Path("agent.md").read_text(encoding="utf-8")
    for heading in (
        "商品参数与疑虑解答",
        "引导下单",
        "待付款与催付",
        "核对订单",
        "订单备注",
        "活动告知",
    ):
        assert heading in rules

    print("售前八类场景、活动时效、催付频控和订单备注确认测试通过。")


if __name__ == "__main__":
    main()
