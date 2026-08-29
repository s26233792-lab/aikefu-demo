"""用户不良反馈识别、去重、统计与状态流转回归测试。"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from xhs_kefu.api import create_app
from xhs_kefu.config import BASE_DIR, Settings
from xhs_kefu.feedback import detect_negative_feedback
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


def test_detection() -> None:
    assert detect_negative_feedback("这件衬衫有明显色差，做工太差了").category == "商品质量"
    assert detect_negative_feedback("物流一直没到，我要投诉").category == "物流履约"
    assert detect_negative_feedback("想看看白衬衫有什么推荐") is None
    critical = detect_negative_feedback("再不处理我就找 12315 投诉")
    assert critical is not None and critical.severity == "critical"


def test_store_stats(tmp: Path) -> None:
    store = SQLiteStore(str(tmp / "feedback.db"))
    now = datetime.now(timezone.utc)
    records = [
        ("fb-1", "商品质量", "high", "open", 0),
        ("fb-2", "物流履约", "critical", "processing", 1),
        ("fb-3", "商品质量", "medium", "resolved", 2),
    ]
    for index, (feedback_id, category, severity, status, age) in enumerate(records):
        created_at = (now - timedelta(days=age)).isoformat()
        store.add_feedback(
            id=feedback_id, message_id=f"msg-{index}", session_key=f"session-{index}",
            customer_id=f"buyer-{index}", tenant_id="demo", store_id="STORE-001",
            channel="douyin_feige" if index == 1 else "xhs_qianfan_desktop",
            category=category, severity=severity, trigger_word="测试", content="反馈内容",
            created_at=created_at,
        )
        if status != "open":
            store.update_feedback_status(feedback_id, status, created_at)
    stats = store.feedback_stats(30)
    assert stats["total"] == 3
    assert stats["unresolved"] == 2
    assert stats["resolved"] == 1
    assert stats["critical"] == 1
    assert stats["resolution_rate"] == 33.3
    assert stats["categories"][0] == {"name": "商品质量", "count": 2}


def test_api_capture_and_status(tmp: Path) -> None:
    app = create_app(_settings(str(tmp / "api.db")))
    with TestClient(app) as client:
        payload = {
            "text": "衣服有明显色差，做工太差了，我很失望",
            "customer_id": "buyer-feedback",
            "message_id": "negative-001",
            "channel": "xhs_qianfan_desktop",
        }
        first = client.post("/zhixia/decide", json=payload)
        second = client.post("/zhixia/decide", json=payload)
        assert first.status_code == 200
        assert first.json().get("feedback_id")
        assert second.status_code == 200

        rows = client.get("/v1/feedback").json()["feedback"]
        assert len(rows) == 1
        assert rows[0]["category"] == "商品质量"
        feedback_id = rows[0]["id"]

        updated = client.post(
            f"/v1/feedback/{feedback_id}/status", json={"status": "resolved"}
        )
        assert updated.status_code == 200
        assert updated.json()["feedback"]["status"] == "resolved"
        stats = client.get("/v1/feedback/stats", params={"days": 30}).json()
        assert stats["total"] == 1
        assert stats["resolved"] == 1


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="aikefu-feedback-") as directory:
        tmp = Path(directory)
        test_detection()
        test_store_stats(tmp)
        test_api_capture_and_status(tmp)
    print("用户不良反馈识别、统计和状态流转测试通过。")


if __name__ == "__main__":
    main()
