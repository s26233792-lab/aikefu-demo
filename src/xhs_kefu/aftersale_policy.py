"""小红书/抖音/千牛售后风控引擎。

忠实移植参考项目 dxl-commerce-agent 的 kefu-core/SKILL.md 售后规则，
这是其真实运营踩坑总结的精髓。核心机制：

1. 先核实事实，再回复顾客（禁止凭记忆编造已验证）；
2. 缺货纸条单 / 礼物单 / 「没发」单的识别与全额仅退款；
3. 3元→5元「不退货小额赔偿」挽留阶梯；
4. 售后原因统一引导「与商家协商一致」；
5. 禁止承诺异步回访（只在本轮响应）；
6. 证据要求（破损/少发需照片）。

所有金额用「分」为单位的整数，避免浮点误差。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RefundVerdict(StrEnum):
    """退款/赔偿的最终处置。"""

    FULL_REFUND_GIFT = "full_refund_gift"          # 缺货纸条单/礼物单/没发单 → 全额仅退款
    SMALL_COMPENSATE = "small_compensate"           # 3元/5元小额赔偿挽留
    NEED_EVIDENCE = "need_evidence"                 # 需补证据
    NEED_CLARIFY = "need_clarify"                   # 需核实，不得承诺金额
    RETURN_REFUND = "return_refund"                 # 退货退款
    HUMAN = "human"                                 # 转人工


@dataclass(frozen=True, slots=True)
class RefundDecision:
    verdict: RefundVerdict
    amount_cents: int = 0               # 建议金额（分）
    reason_code: str = ""
    message: str = ""                   # 应回复顾客的话术
    rules: list[str] = field(default_factory=list)  # 命中的规则说明


# 「与商家协商一致」标准话术（参考 SKILL.md 第 10 条原文）
NEGOTIATED_REASON_PROMPT = (
    "如您认可该方案，申请售后时原因请选择「与商家协商一致」，金额按我们确认的金额填写；"
    "这个原因是系统自动审批，通常很快通过；如果选择其他原因，需要售后人员人工审核，会慢很多。"
)

# 禁止异步回访的固定话术（参考 SKILL.md 第 15、60 条）
NO_ASYNC_FOLLOWUP = (
    "（注：客服仅在本轮响应后无法自动回访，如需继续处理请在本会话内补充信息。）"
)


@dataclass(frozen=True, slots=True)
class CompensationRule:
    """小额赔偿规则（参考 SKILL.md 第 8、9 条）。"""

    default_offer_cents: int = 300    # 默认 3 元
    max_offer_cents: int = 500        # 最高 5 元
    paid_amount_threshold_cents: int = 1500  # 实付 >= 15 元才可提至 5 元

    def allowed_amount(self, paid_amount_cents: int, *, upgrade: bool) -> int:
        """计算可建议的赔偿金额。

        - 默认 3 元，且不超过实付；
        - 仅当实付 >= 15 元 且 顾客不接受3元但愿留货（upgrade=True）时，可提至 5 元。
        """
        base = min(self.default_offer_cents, paid_amount_cents)
        if upgrade and paid_amount_cents >= self.paid_amount_threshold_cents:
            return min(self.max_offer_cents, paid_amount_cents)
        return base


class AftersalePolicyEngine:
    """售后风控决策引擎。"""

    def __init__(self, rule: CompensationRule | None = None) -> None:
        self.rule = rule or CompensationRule()

    # ---------- 缺货纸条单 / 礼物单 / 没发单 识别 ----------

    def is_gift_or_not_sent(self, order: dict[str, Any]) -> tuple[bool, str]:
        """判断订单是否命中「缺货纸条单/礼物单/没发单」。

        命中条件（SKILL.md 第 6、11 条）：
        - gift_order.is_gift == True
        - gift_order.matched_outer_id 命中 TARGET_OUTER_ID（demo 用固定值）
        - seller_memo 包含「没发」
        """
        gift = order.get("gift_order") or {}
        if gift.get("is_gift") is True:
            return True, "gift_order.is_gift"
        matched = gift.get("matched_outer_id", "")
        # demo 环境未真正配置 TARGET_OUTER_ID，此处仅当 matched_outer_id 非空即视为命中
        if matched:
            return True, f"matched_outer_id={matched}"
        memo = order.get("seller_memo", "")
        if "没发" in memo:
            return True, "seller_memo_含_没发"
        return False, ""

    # ---------- 售后问题本质分类 ----------

    def classify_issue(self, text: str) -> str:
        """把顾客反馈分类：damaged/wrong_item/missing_item/quality/物流/change_of_mind/other。"""
        if any(w in text for w in ("破损", "破了", "碎了", "污渍", "异味")):
            return "damaged"
        if any(w in text for w in ("错发", "发错", "不一致")):
            return "wrong_item"
        if any(w in text for w in ("少发", "漏发", "少件", "空袋", "缺货")):
            return "missing_item"
        if any(w in text for w in ("质量", "瑕疵", "坏了", "不满意")):
            return "quality"
        if any(w in text for w in ("物流", "快递", "延误", "丢件")):
            return "logistics"
        if any(w in text for w in ("不喜欢", "不想要", "拍错", "买错")):
            return "change_of_mind"
        return "other"

    # ---------- 主决策入口 ----------

    def evaluate(
        self,
        *,
        order: dict[str, Any] | None,
        issue_type: str,
        user_text: str,
        has_evidence: bool,
        upgrade_compensation: bool = False,
    ) -> RefundDecision:
        """根据订单事实 + 问题类型 + 证据，给出退款/赔偿处置。"""
        # 1. 无订单事实 → 需核实，不得承诺金额
        if order is None:
            return RefundDecision(
                RefundVerdict.NEED_CLARIFY, 0, "ORDER_NOT_FOUND",
                "暂时不能确认，请提供订单号或订单详情页以便核实。",
            )

        # 2. 缺货纸条单/礼物单/没发单 → 全额仅退款（不走 3/5 元赔付）
        is_gift, gift_reason = self.is_gift_or_not_sent(order)
        if is_gift and issue_type == "missing_item":
            paid = int(order.get("paid_amount_cents", 0))
            return RefundDecision(
                RefundVerdict.FULL_REFUND_GIFT, paid, gift_reason,
                f"已核验该订单为缺货纸条单/没发单，可对订单 {order.get('order_id')} 申请已发货仅退款，"
                f"金额按实付 ¥{paid/100:.2f} 填写。",
                rules=["缺货纸条单/礼物单/没发单 → 全额仅退款"],
            )

        # 3. 需证据类（破损/错发/少发等）且无证据 → 需补证据
        evidence_types = {"damaged", "wrong_item", "missing_item", "quality"}
        if issue_type in evidence_types and not has_evidence:
            return RefundDecision(
                RefundVerdict.NEED_EVIDENCE, 0, "EVIDENCE_REQUIRED",
                "为核实问题，请提供清晰照片（破损/错发/少发请拍商品全图 + 细节图，至少 2 张）。",
                rules=["破损/错发/少发/质量 → 需证据照片"],
            )

        # 4. 非缺货单 → 优先「不退货小额赔偿」挽留
        paid = int(order.get("paid_amount_cents", 0))
        amount = self.rule.allowed_amount(paid, upgrade=upgrade_compensation)
        if issue_type in evidence_types or issue_type == "quality":
            msg = (
                f"非常抱歉给您带来不好的体验！为表歉意，可为您申请 ¥{amount/100:.2f} 补偿"
                f"（不退货留货）。{NEGOTIATED_REASON_PROMPT}"
            )
            return RefundDecision(
                RefundVerdict.SMALL_COMPENSATE, amount, "SMALL_COMPENSATE", msg,
                rules=["3元默认/实付≥15元可提至5元", "不退货小额赔偿挽留"],
            )

        # 5. 顾客明确要退货退款
        if any(w in user_text for w in ("退货", "退款退货", "寄回")):
            return RefundDecision(
                RefundVerdict.RETURN_REFUND, 0, "RETURN_REFUND",
                f"如需退货退款，请申请退货退款，原因选择「与商家协商一致」。{NEGOTIATED_REASON_PROMPT}"
                "退货会产生寄回与等待成本，建议优先接受小额补偿留货。",
                rules=["退货退款 → 与商家协商一致"],
            )

        # 6. 兜底 → 转人工
        return RefundDecision(
            RefundVerdict.HUMAN, 0, "NEED_HUMAN",
            "该售后情况需人工核验，已为您转交人工客服。",
        )

    # ---------- 回复约束脚手架 ----------

    @staticmethod
    def forbid_async_promise() -> str:
        """禁止承诺异步回访的话术兜底。"""
        return NO_ASYNC_FOLLOWUP
