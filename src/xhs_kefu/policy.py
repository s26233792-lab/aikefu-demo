"""小红书千帆客服 Agent —— 风控引擎。

对写操作（改地址 / 快递拦截 / 协商补偿）做后端强校验，模型输出无权绕过：
- 补偿金额必须为正、不超过实付、不超过策略上限；
- 补偿超过自动审批限额 → 人工审批；
- 破损/错发/少发等理由必须提供证据（附件）；
- 改地址 / 拦截 一律人工审批（高风险）。

对外只返回 allow / deny / require_approval 三类结果与 reason_code。
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .domain import PolicyDecision, PolicyOutcome


@dataclass(frozen=True, slots=True)
class CompensationRule:
    auto_approve_limit_cents: int     # 自动审批上限
    maximum_refund_cents: int         # 策略最大值
    default_offer_cents: int          # 默认小额补偿建议（如 300 分 = 3 元）
    max_offer_cents: int              # 挽留可提至上限（如 500 分 = 5 元）
    require_evidence_for: frozenset[str]

    @classmethod
    def from_file(cls, path: Path) -> "CompensationRule":
        with path.open("rb") as stream:
            raw = tomllib.load(stream)["compensation"]
        return cls(
            auto_approve_limit_cents=int(raw["auto_approve_limit_cents"]),
            maximum_refund_cents=int(raw["maximum_refund_cents"]),
            default_offer_cents=int(raw["default_offer_cents"]),
            max_offer_cents=int(raw["max_offer_cents"]),
            require_evidence_for=frozenset(raw["require_evidence_for"]),
        )

    @classmethod
    def defaults(cls) -> "CompensationRule":
        return cls(
            auto_approve_limit_cents=500,
            maximum_refund_cents=3000,
            default_offer_cents=300,
            max_offer_cents=500,
            require_evidence_for=frozenset({"damaged", "quality_issue", "wrong_item", "missing_item"}),
        )


class PolicyEngine:
    """风控决策，只依赖可信数据与规则。"""

    def __init__(self, rule: CompensationRule) -> None:
        self.rule = rule

    def requires_evidence(self, reason: str) -> bool:
        return reason in self.rule.require_evidence_for

    def evaluate_compensation(
        self,
        *,
        order: dict[str, Any],
        amount_cents: int,
        reason: str,
        has_evidence: bool,
    ) -> PolicyDecision:
        if order.get("status") in {"processing", "pending"}:
            # 未发货订单走退款而非补偿，交由人工
            return PolicyDecision(
                PolicyOutcome.REQUIRE_APPROVAL,
                "ORDER_NOT_SHIPPED",
                "该订单尚未发货，补偿需人工确认走退款流程。",
            )
        if amount_cents <= 0:
            return PolicyDecision(
                PolicyOutcome.DENY,
                "INVALID_AMOUNT",
                "补偿金额必须为正数。",
            )
        if amount_cents > int(order["paid_amount_cents"]):
            return PolicyDecision(
                PolicyOutcome.DENY,
                "ABOVE_PAID_AMOUNT",
                "补偿金额不能超过订单实付金额。",
            )
        if amount_cents > self.rule.maximum_refund_cents:
            return PolicyDecision(
                PolicyOutcome.DENY,
                "ABOVE_POLICY_MAXIMUM",
                f"补偿金额超过策略上限 ¥{self.rule.maximum_refund_cents / 100:.2f}。",
            )
        if self.requires_evidence(reason) and not has_evidence:
            return PolicyDecision(
                PolicyOutcome.DENY,
                "EVIDENCE_REQUIRED",
                "该补偿理由需顾客提供凭证（破损/错发/少发照片）。",
            )
        if amount_cents > self.rule.auto_approve_limit_cents:
            return PolicyDecision(
                PolicyOutcome.REQUIRE_APPROVAL,
                "HUMAN_APPROVAL_REQUIRED",
                "补偿金额超过自动审批限额，需人工审批。",
            )
        return PolicyDecision(
            PolicyOutcome.ALLOW,
            "WITHIN_AUTO_LIMIT",
            "补偿在自动限额内，可执行。",
        )

    def evaluate_high_risk_action(self, *, action: str, order: dict[str, Any]) -> PolicyDecision:
        """改地址 / 快递拦截等高风险写操作：默认人工审批。"""
        if action == "intercept_express":
            if order.get("status") == "delivered":
                return PolicyDecision(
                    PolicyOutcome.DENY,
                    "ALREADY_DELIVERED",
                    "订单已签收，无法拦截。",
                )
        return PolicyDecision(
            PolicyOutcome.REQUIRE_APPROVAL,
            "HIGH_RISK_ACTION",
            "该操作属于高风险写操作，需人工审批后执行。",
        )
