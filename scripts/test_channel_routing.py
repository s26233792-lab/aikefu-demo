"""多平台审批/outbox 隔离回归测试；无需网络和 LLM Key。"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from xhs_kefu.api import create_app
from xhs_kefu.config import BASE_DIR, Settings
from xhs_kefu.storage import SQLiteStore


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


def test_legacy_database_migration(tmp: Path) -> None:
    db = tmp / "legacy.db"
    connection = sqlite3.connect(db)
    connection.execute(
        """
        CREATE TABLE moderation (
            id TEXT PRIMARY KEY, session_key TEXT NOT NULL, customer_id TEXT,
            kind TEXT NOT NULL, content TEXT NOT NULL, intent TEXT,
            reason_code TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL
        )
        """
    )
    connection.commit()
    connection.close()

    store = SQLiteStore(str(db))
    columns = {
        row["name"] for row in store.connection.execute("PRAGMA table_info(moderation)")
    }
    assert {"tenant_id", "store_id", "channel"}.issubset(columns)


def test_channel_and_customer_isolation(tmp: Path) -> None:
    store = SQLiteStore(str(tmp / "store.db"))
    store.add_outbox(
        id="dy-1", session_key="dy", customer_id="buyer-a", content="抖店回复",
        channel="douyin_feige", created_at="2026-01-01T00:00:00Z",
    )
    store.add_outbox(
        id="xhs-1", session_key="xhs", customer_id="buyer-a", content="千帆回复",
        channel="xhs_qianfan_desktop", created_at="2026-01-01T00:00:01Z",
    )
    store.add_outbox(
        id="dy-2", session_key="dy2", customer_id="buyer-b", content="另一顾客回复",
        channel="douyin_feige", created_at="2026-01-01T00:00:02Z",
    )
    assert [row["id"] for row in store.pull_outbox(channel="douyin_feige", customer_id="buyer-a")] == ["dy-1"]
    assert [row["id"] for row in store.pull_outbox(channel="xhs_qianfan_desktop")] == ["xhs-1"]


def test_approval_keeps_origin_platform(tmp: Path) -> None:
    app = create_app(_settings(str(tmp / "api.db")))
    store = app.state.runtime.store
    store.add_moderation(
        id="mod-dy", session_key="zhixia|demo|douyin_feige|STORE-001|buyer-a",
        customer_id="buyer-a", tenant_id="demo", store_id="STORE-001",
        channel="douyin_feige", kind="reply", content="已为您登记处理。",
        intent="refund", reason_code="REFUND_REQUEST", created_at="2026-01-01T00:00:00Z",
    )
    with TestClient(app) as client:
        approved = client.post("/v1/moderation/mod-dy/approve")
        assert approved.status_code == 200
        assert approved.json()["enqueued"] is True
        douyin = client.get(
            "/v1/outbox/pull", params={"channel": "douyin_feige", "customer_id": "buyer-a"}
        ).json()["outbox"]
        qianfan = client.get(
            "/v1/outbox/pull", params={"channel": "xhs_qianfan_desktop"}
        ).json()["outbox"]
        session_key = "zhixia|demo|douyin_feige|STORE-001|buyer-blocked"
        taken = client.post(
            "/v1/handoff",
            json={"session_key": session_key, "action": "take_over"},
        )
        assert taken.status_code == 200
        blocked = client.post(
            "/platforms/douyin/decide",
            json={"text": "这件有货吗", "customer_id": "buyer-blocked"},
        ).json()
        assert blocked["status"] == "taken_over"
        assert blocked["reply"] == ""
        client.post(
            "/v1/handoff",
            json={"session_key": session_key, "action": "release"},
        )
        greeting = client.post(
            "/platforms/douyin/decide",
            json={
                "text": "在吗",
                "customer_id": "buyer-greeting",
                "message_id": "greeting-1",
            },
        ).json()
        assert greeting["status"] == "resolved"
        assert greeting["needs_approval"] is False
        assert "我是" not in greeting["reply"]
        assert "请问" in greeting["reply"]
    assert [row["content"] for row in douyin] == ["已为您登记处理。"]
    assert qianfan == []


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="aikefu-routing-") as directory:
        tmp = Path(directory)
        test_legacy_database_migration(tmp)
        test_channel_and_customer_isolation(tmp)
        test_approval_keeps_origin_platform(tmp)
    print("多平台路由、旧数据库迁移和审批回填测试通过。")


if __name__ == "__main__":
    main()
