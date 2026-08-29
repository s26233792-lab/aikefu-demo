"""用户不良反馈识别。

只收录明确的不满、投诉、履约异常和商品问题，普通咨询不会被误记为负面反馈。
分类结果用于运营统计，不参与客服回复内容生成。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class FeedbackSignal:
    category: str
    severity: str
    trigger: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


_CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("商品质量", ("破损", "开线", "起球", "掉色", "色差", "异味", "质量", "做工", "瑕疵", "少件", "错发")),
    ("尺码版型", ("尺码不准", "偏大", "偏小", "不合身", "版型", "勒", "显胖", "太长", "太短")),
    ("物流履约", ("没发货", "不发货", "发货慢", "物流", "快递", "丢件", "签收", "超时", "一直没到", "还没到")),
    ("退款售后", ("退款", "退货", "售后", "退不了", "不给退", "退款没到", "退款未到账")),
    ("价格活动", ("差价", "降价", "优惠", "活动", "价格", "太贵", "贵了")),
    ("客服体验", ("客服", "态度", "不回复", "没人管", "不处理", "敷衍", "转人工")),
)

_NEGATIVE_TRIGGERS = (
    "投诉", "差评", "举报", "平台介入", "12315", "工商", "法院", "报警", "维权",
    "垃圾", "气死", "太差", "差劲", "失望", "不满意", "体验不好", "再也不买",
    "骗人", "欺骗", "坑人", "离谱", "恶心", "敷衍", "没人管", "不处理", "不回复",
    "质量问题", "破损", "开线", "起球", "掉色", "色差", "异味", "瑕疵", "少件", "错发",
    "没发货", "不发货", "发货慢", "一直没到", "还没到", "物流不动", "丢件", "错误签收",
    "退款没到", "退款未到账", "退不了", "不给退", "尺码不准", "不合身",
)

_CRITICAL_TRIGGERS = (
    "12315", "工商", "法院", "报警", "人身安全", "受伤", "过敏", "平台介入", "举报",
)

_HIGH_TRIGGERS = (
    "投诉", "差评", "维权", "垃圾", "气死", "太差", "骗人", "欺骗", "丢件",
    "破损", "质量问题", "退款没到", "退款未到账", "一直没到", "没发货", "不处理",
)


def detect_negative_feedback(
    text: str,
    *,
    tone: str | None = None,
    disposition: str | None = None,
) -> FeedbackSignal | None:
    """返回不良反馈信号；没有明确负面证据时返回 ``None``。"""
    normalized = "".join(text.strip().lower().split())
    if not normalized:
        return None

    trigger = next((word for word in _NEGATIVE_TRIGGERS if word in normalized), "")
    negative_tone = (tone or "").lower() in {"negative", "angry", "complaint"}
    escalated = (disposition or "").lower() == "handoff_human"
    if not trigger and not negative_tone and not escalated:
        return None

    category = "其他反馈"
    for label, keywords in _CATEGORY_RULES:
        if any(word in normalized for word in keywords):
            category = label
            break
    if category == "其他反馈" and (escalated or any(word in normalized for word in ("投诉", "差评", "转人工"))):
        category = "客服体验"

    if any(word in normalized for word in _CRITICAL_TRIGGERS):
        severity = "critical"
    elif any(word in normalized for word in _HIGH_TRIGGERS) or negative_tone:
        severity = "high"
    else:
        severity = "medium"
    return FeedbackSignal(category=category, severity=severity, trigger=trigger or tone or disposition or "negative")
