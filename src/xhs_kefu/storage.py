"""小红书千帆客服 Agent —— SQLite 存储。

存储：会话消息、决策、动作（写操作）状态、发送回执。
与参考架构一致：决策 / 发送 / 回执三态分离。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any


class SQLiteStore:
    def __init__(self, path: str) -> None:
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self.connection:
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    dedupe_key TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_key TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    intent TEXT,
                    status TEXT,
                    reply TEXT,
                    tool_calls TEXT,
                    policy TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS actions (
                    action_id TEXT PRIMARY KEY,
                    business_key TEXT UNIQUE,
                    action_type TEXT,
                    payload TEXT,
                    state TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS receipts (
                    message_id TEXT PRIMARY KEY,
                    session_key TEXT,
                    delivered TEXT,
                    ack_at TEXT
                )
                """
            )
            # 人工审批队列：待审回复 / 待审写操作
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS moderation (
                    id TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL,
                    customer_id TEXT,
                    kind TEXT NOT NULL,         -- reply | action
                    content TEXT NOT NULL,       -- 待审内容（回复文案 或 写操作 payload JSON）
                    intent TEXT,
                    reason_code TEXT,
                    status TEXT NOT NULL,        -- pending | approved | rejected | taken_over
                    created_at TEXT NOT NULL
                )
                """
            )
            # 会话接管状态（human_active = 人工接管中）
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS handoff (
                    session_key TEXT PRIMARY KEY,
                    state TEXT NOT NULL DEFAULT 'auto',  -- auto | human_active
                    reason TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # 待发送队列：审批台手写/审批通过的回复，由 Worker 回填千帆
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox (
                    id TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL,
                    customer_id TEXT,
                    content TEXT NOT NULL,
                    channel TEXT,
                    status TEXT NOT NULL,  -- queued | sent | failed
                    created_at TEXT NOT NULL
                )
                """
            )

    def recent_turns(self, session_key: str, limit: int = 8) -> list[dict[str, str]]:
        rows = self.connection.execute(
            """
            SELECT role, content FROM messages
            WHERE session_key = ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (session_key, limit),
        ).fetchall()
        turns = [{"role": r["role"], "content": r["content"]} for r in rows]
        return list(reversed(turns))

    def dedupe_hit(self, dedupe_key: str) -> dict | None:
        """去重命中时，返回上次的【回复内容】而非顾客原文。

        消息表里同一 dedupe_key 有两条：user（顾客原文）+ assistant（回复）。
        去重命中应复用「回复」，绝不能把顾客原文当回复返回（会导致复读 bug）。
        """
        # 优先返回 assistant 的回复
        row = self.connection.execute(
            "SELECT role, content FROM messages WHERE dedupe_key = ?",
            (dedupe_key + "|assistant",),
        ).fetchone()
        if row is not None and row["content"]:
            return {"role": row["role"], "content": row["content"]}
        # 兜底：没有 assistant 记录时返回 None（当作新消息重新处理）
        return None

    def save_turn(
        self, *, dedupe_key: str, session_key: str, role: str, content: str, created_at: str
    ) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO messages(dedupe_key, session_key, role, content, created_at) VALUES (?,?,?,?,?)",
                (dedupe_key, session_key, role, content, created_at),
            )

    def save_decision(self, *, session_key: str, trace_id: str, decision: dict, created_at: str) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO decisions(session_key, trace_id, intent, status, reply, tool_calls, policy, created_at)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    session_key,
                    trace_id,
                    decision.get("intent"),
                    decision.get("status"),
                    decision.get("reply"),
                    json.dumps(decision.get("tool_calls", []), ensure_ascii=False),
                    json.dumps(decision.get("policy"), ensure_ascii=False),
                    created_at,
                ),
            )

    def save_action(self, *, action_id: str, business_key: str, action_type: str, payload: dict, state: str, created_at: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO actions(action_id, business_key, action_type, payload, state, created_at) VALUES (?,?,?,?,?,?)",
                (action_id, business_key, action_type, json.dumps(payload, ensure_ascii=False), state, created_at),
            )

    def get_action(self, action_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM actions WHERE action_id = ?", (action_id,)
        ).fetchone()
        if row is None:
            return None
        return {
            "action_id": row["action_id"],
            "action_type": row["action_type"],
            "payload": json.loads(row["payload"]),
            "state": row["state"],
        }

    def list_pending_actions(self) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM actions WHERE state = 'pending_approval' ORDER BY created_at"
        ).fetchall()
        return [
            {
                "action_id": r["action_id"],
                "action_type": r["action_type"],
                "payload": json.loads(r["payload"]),
                "state": r["state"],
            }
            for r in rows
        ]

    def update_action_state(self, action_id: str, state: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE actions SET state = ? WHERE action_id = ?", (state, action_id)
            )

    def save_receipt(self, *, message_id: str, session_key: str, delivered: str, ack_at: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO receipts(message_id, session_key, delivered, ack_at) VALUES (?,?,?,?)",
                (message_id, session_key, delivered, ack_at),
            )

    def history_decisions(self, session_key: str, limit: int = 20) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT trace_id, intent, status, reply, tool_calls, policy, created_at
            FROM decisions WHERE session_key = ? ORDER BY id DESC LIMIT ?
            """,
            (session_key, limit),
        ).fetchall()
        result = []
        for r in reversed(rows):
            result.append(
                {
                    "trace_id": r["trace_id"],
                    "intent": r["intent"],
                    "status": r["status"],
                    "reply": r["reply"],
                    "tool_calls": json.loads(r["tool_calls"]),
                    "policy": json.loads(r["policy"]),
                    "created_at": r["created_at"],
                }
            )
        return result

    def health(self) -> bool:
        try:
            self.connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ----- 人工审批 / 接管 -----

    def add_moderation(
        self, *, id: str, session_key: str, customer_id: str, kind: str,
        content: str, intent: str | None, reason_code: str, created_at: str
    ) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO moderation(id, session_key, customer_id, kind, content, intent, reason_code, status, created_at) "
                "VALUES (?,?,?,?,?,?,?, 'pending', ?)",
                (id, session_key, customer_id, kind, content, intent, reason_code, created_at),
            )

    def list_moderation(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self.connection.execute(
                "SELECT * FROM moderation WHERE status = ? ORDER BY created_at", (status,)
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM moderation ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_moderation(self, id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM moderation WHERE id = ?", (id,)
        ).fetchone()
        return dict(row) if row else None

    def update_moderation_status(self, id: str, status: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE moderation SET status = ? WHERE id = ?", (status, id)
            )

    # ----- 接管 -----

    def set_handoff(self, session_key: str, state: str, reason: str, updated_at: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO handoff(session_key, state, reason, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(session_key) DO UPDATE SET state=excluded.state, reason=excluded.reason, updated_at=excluded.updated_at",
                (session_key, state, reason, updated_at),
            )

    def get_handoff(self, session_key: str) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM handoff WHERE session_key = ?", (session_key,)
        ).fetchone()
        return dict(row) if row else None

    # ----- 待发送队列（outbox）：审批台手写/审批通过的回复 → Worker 回填千帆 -----

    def add_outbox(
        self, *, id: str, session_key: str, customer_id: str, content: str,
        channel: str, created_at: str
    ) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO outbox(id, session_key, customer_id, content, channel, status, created_at) "
                "VALUES (?,?,?,?,?, 'queued', ?)",
                (id, session_key, customer_id, content, channel, created_at),
            )

    def pull_outbox(self, limit: int = 10) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM outbox WHERE status = 'queued' ORDER BY created_at LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_outbox(self, id: str, status: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE outbox SET status = ? WHERE id = ?", (status, id)
            )
