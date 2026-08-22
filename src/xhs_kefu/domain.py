"""小红书千帆客服 Agent —— 领域模型。

忠实还原参考架构 dxl-commerce-agent 的领域层，聚焦小红书场景。
所有业务事实（订单/物流/商品）必须来自工具结果，禁止模型凭记忆猜测。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class Intent(StrEnum):
    """客服意图分类（售前/售中/售后）。"""

    # 售前
    PRODUCT_RECOMMEND = "product_recommend"      # 商品推荐
    PRODUCT_QUESTION = "product_question"        # 产品参数/材质/答疑
    PLACE_ORDER = "place_order"                  # 引导下单/催付
    # 售中
    LOGISTICS_STATUS = "logistics_status"        # 物流查询/催货
    MODIFY_ADDRESS = "modify_address"            # 修改收货地址
    INTERCEPT_EXPRESS = "intercept_express"      # 快递拦截
    # 售后
    LOGISTICS_EXCEPTION = "logistics_exception"  # 物流异常跟进
    COMPENSATION = "compensation"                # 协商补偿/补偿
    # 兜底
    GREETING = "greeting"
    OUT_OF_SCOPE = "out_of_scope"
    SECURITY_REJECTED = "security_rejected"
    UNKNOWN = "unknown"


class PolicyOutcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class ActionState(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    DENIED = "denied"
    SUCCEEDED = "succeeded"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """客服入口统一事件，与参考架构 IncomingMessage 对齐。

    小红书千帆网页版 Worker 与本地模拟器都产出同一种结构。
    """

    tenant_id: str
    channel: str = "xhs_qianfan"
    store_id: str = "STORE-001"
    customer_id: str = ""
    message_id: str = ""
    text: str = ""
    attachments: tuple[str, ...] = ()
    received_at: datetime = field(default_factory=now_utc)

    @property
    def session_key(self) -> str:
        """会话归属：租户 + 渠道 + 店铺 + 顾客。"""
        return "|".join(
            (self.tenant_id, self.channel, self.store_id, self.customer_id)
        )

    @property
    def dedupe_key(self) -> str:
        """消息去重键：会话 + 消息 ID。"""
        return f"{self.session_key}|{self.message_id}"


@dataclass(frozen=True, slots=True)
class DecisionPlan:
    """规划结果：意图 + 从文本中安全抽取的实体。

    订单号/SKU 只能从文本中显式抽取，不能由模型自由补全业务事实。
    """

    intent: Intent
    order_id: str | None = None
    sku: str | None = None
    address: str | None = None
    amount_cents: int | None = None
    reason: str | None = None
    needs_evidence: bool = False
    security_reason: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """风控决策结果。"""

    outcome: PolicyOutcome
    reason_code: str
    explanation: str

    def to_dict(self) -> dict[str, str]:
        return {
            "outcome": self.outcome.value,
            "reason_code": self.reason_code,
            "explanation": self.explanation,
        }


@dataclass(slots=True)
class TraceStep:
    """链路追踪步骤，用于右侧可视化面板。"""

    kind: str          # runtime / planner / tool / policy / action / handoff
    name: str
    status: str
    detail: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "latency_ms": self.latency_ms,
        }
