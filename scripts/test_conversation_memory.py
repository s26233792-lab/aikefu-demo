"""对话记忆隔离、消息时序和顾客可见语气回归测试。"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, "src")

from xhs_kefu.api import create_app
from xhs_kefu.config import BASE_DIR, Settings
from xhs_kefu.storage import SQLiteStore
from xhs_kefu.zhixia_agent import looks_like_internal_analysis
from xhs_kefu.zhixia_runtime import ZhixiaRuntime, detect_topic


def _settings(database_path: str) -> Settings:
    return Settings(
        base_dir=BASE_DIR,
        data_dir=BASE_DIR / "src" / "xhs_kefu" / "data",
        policy_path=BASE_DIR / "config" / "policy.toml",
        database_path=database_path,
        llm_mode="rules",
        llm_base_url="https://api.deepseek.com",
        llm_model="deepseek-chat",
        llm_api_key=None,
        store_id="STORE-001",
        tenant_id="demo",
        api_key=None,
    )


def test_topic_memory_isolation() -> None:
    history = [
        {"role": "user", "content": "我想买通勤西装，有推荐吗？"},
        {"role": "assistant", "content": "可以看看云感通勤西装，SKU 是 ZX-B226。"},
    ]

    assert detect_topic("退款怎么还没到账？") == "aftersale"
    assert detect_topic("这个活动什么时候结束？") == "campaign"
    assert detect_topic("这个呢？") == "reference"
    assert detect_topic("这款有M码吗？") == "product"

    # 新会员、活动、售后和未知问题都不应携带上一轮商品答案。
    for message in (
        "会员积分怎么查？",
        "这个活动什么时候结束？",
        "退款怎么还没到账？",
        "我还有一个全新的问题",
    ):
        assert ZhixiaRuntime._build_llm_history(message, history) == [], message

    # 明确指代、短确认和同话题追问只保留必要的最近上下文。
    assert ZhixiaRuntime._build_llm_history("这个呢？", history) == history
    assert ZhixiaRuntime._build_llm_history("好的", history) == history
    assert ZhixiaRuntime._build_llm_history("这款有M码吗？", history) == history

    longer_history = history + [
        {"role": "user", "content": "黑色西装是什么面料？"},
        {"role": "assistant", "content": "面料信息我帮您查到了。"},
    ]
    assert ZhixiaRuntime._build_llm_history("黑色还有货吗？", longer_history) == longer_history[-2:]


def test_stable_turn_order() -> None:
    with tempfile.TemporaryDirectory(prefix="aikefu-memory-order-") as directory:
        store = SQLiteStore(str(Path(directory) / "order.db"))
        timestamp = "2026-09-01T12:00:00+00:00"
        store.save_turn(
            dedupe_key="turn-1|user", session_key="session", role="user",
            content="新问题", created_at=timestamp,
        )
        store.save_turn(
            dedupe_key="turn-1|assistant", session_key="session", role="assistant",
            content="新答案", created_at=timestamp,
        )
        store.save_turn(
            dedupe_key="turn-2|user", session_key="session", role="user",
            content="第二个问题", created_at=timestamp,
        )
        store.save_turn(
            dedupe_key="turn-2|assistant", session_key="session", role="assistant",
            content="第二个答案", created_at=timestamp,
        )
        assert store.recent_turns("session") == [
            {"role": "user", "content": "新问题"},
            {"role": "assistant", "content": "新答案"},
            {"role": "user", "content": "第二个问题"},
            {"role": "assistant", "content": "第二个答案"},
        ]


def test_repeated_text_and_unsent_draft_storage() -> None:
    with tempfile.TemporaryDirectory(prefix="aikefu-memory-api-") as directory:
        app = create_app(_settings(str(Path(directory) / "api.db")))
        store = app.state.runtime.store
        with TestClient(app) as client:
            for message_id in ("same-1", "same-2"):
                response = client.post(
                    "/zhixia/decide",
                    json={
                        "text": "好的",
                        "customer_id": "repeat-customer",
                        "message_id": message_id,
                        "suppress_intro": True,
                    },
                )
                assert response.status_code == 200
                assert response.json()["status"] == "resolved"

            repeated_session = "zhixia|demo|xhs_qianfan_desktop|STORE-001|repeat-customer"
            repeated_rows = store.connection.execute(
                "SELECT role FROM messages WHERE session_key = ?", (repeated_session,)
            ).fetchall()
            assert len(repeated_rows) == 4

            pending = client.post(
                "/zhixia/decide",
                json={
                    "text": "我收到的衣服有明显色差，做工也很差，我很失望",
                    "customer_id": "pending-customer",
                    "message_id": "pending-1",
                    "suppress_intro": True,
                },
            )
            assert pending.status_code == 200
            assert pending.json()["needs_approval"] is True
            pending_session = "zhixia|demo|xhs_qianfan_desktop|STORE-001|pending-customer"
            pending_roles = [
                row["role"]
                for row in store.connection.execute(
                    "SELECT role FROM messages WHERE session_key = ?", (pending_session,)
                ).fetchall()
            ]
            assert pending_roles == ["user"]


def test_internal_analysis_never_reaches_customer() -> None:
    assert looks_like_internal_analysis("顾客提到衣服有色差，我需要订单信息来核实。")
    assert not looks_like_internal_analysis("这款面料比较轻薄，夏天穿会更舒服。")

    class InternalNoteAgent:
        async def run(self, **_: object) -> dict:
            return {
                "reply": "顾客询问裙子材质，我需要先查询商品信息。",
                "tool_calls": [],
            }

    result = asyncio.run(
        ZhixiaRuntime(llm_agent=InternalNoteAgent()).handle(
            text="这条裙子是什么面料？",
            suppress_intro=True,
        )
    )
    assert not looks_like_internal_analysis(result["reply"])
    assert "顾客" not in result["reply"]
    assert "我需要" not in result["reply"]


def main() -> None:
    test_topic_memory_isolation()
    test_stable_turn_order()
    test_repeated_text_and_unsent_draft_storage()
    test_internal_analysis_never_reaches_customer()
    print("对话记忆隔离、重复消息时序、待审草稿和拟人语气防泄漏测试通过。")


if __name__ == "__main__":
    main()
