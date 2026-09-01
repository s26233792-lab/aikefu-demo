"""小红书千帆客服 Agent —— 回答机制决策引擎。

把"什么自动答、什么转人工"统一成一个明确的状态机，避免散落在各处。

处置类型（Disposition）：
- AUTO_REPLY        自动回复（普通咨询，LLM 查事实生成后直接发）
- REQUIRE_APPROVAL  转人工审批（退货/补偿/改址/拦截等写操作，先入待审队列）
- HANDOFF_HUMAN     转人工接管（情绪升级/投诉/要求转人工/超出能力，停手等真人）
- REJECT            拒绝（提示词注入、异常内容，绝不回复）

判定优先级（从高到低）：
  1. 注入/异常 → REJECT
  2. 情绪升级/投诉/明确要求转人工 → HANDOFF_HUMAN
  3. 写操作意图 → REQUIRE_APPROVAL
  4. 明确可答复 → AUTO_REPLY
  5. 不确定/兜底 → HANDOFF_HUMAN
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .safety import check_inbound


class Disposition(StrEnum):
    AUTO_REPLY = "auto_reply"
    REQUIRE_APPROVAL = "require_approval"
    HANDOFF_HUMAN = "handoff_human"
    REJECT = "reject"


class Tone(StrEnum):
    """用户语气分级（两级 + 一档兜底）。"""

    NORMAL = "normal"        # 正常 → 自动回复
    NEGATIVE = "negative"    # 负面情绪 → 提示 + 转人工
    NEEDS_HUMAN = "needs_human"  # 明确要求转人工 → 强制接管


@dataclass(frozen=True, slots=True)
class Decision:
    disposition: Disposition
    reason_code: str = ""
    detail: str = ""
    tone: Tone = Tone.NORMAL


# 明确要求转人工
_EXPLICIT_HANDOFF_WORDS = ("转人工", "人工客服", "真人", "找人工", "人工服务")

# 投诉/情绪升级
_COMPLAINT_WORDS = ("投诉", "差评", "平台介入", "12315", "工商", "法院", "报警", "举报", "维权")
# 强烈情绪词（配合其他词易触发升级，单看不算投诉）
_EMOTION_WORDS = (
    "气死", "垃圾店", "骗人", "太差", "极度", "非常不满", "再也不买", "欺骗", "诈骗",
    "不满意", "不太满意", "体验很差", "服务很差", "态度很差", "太敷衍", "不负责任",
    # 真实售后反馈里常见的失望/描述不符表达。它们不是普通售前疑问，
    # 应进入人工复核，避免规则兜底误把质量投诉当成商品推荐。
    "很失望", "太失望", "做工很差", "做工太差", "严重偏小", "严重偏大", "描述不符", "根本不准",
)

# 真实顾客很少直接说“我不满”，更常见的是描述已经发生的问题。
# 只有“问题事实 + 已发生/程度表达”同时出现才升级，避免把
# “白色会不会有色差？”这类普通售前疑问误判为投诉。
_ISSUE_WORDS = (
    "色差", "线头", "破损", "开线", "异味", "瑕疵", "污渍", "掉色", "起球",
    "少件", "漏发", "错发", "不合身", "尺码不准", "偏小", "偏大",
    "没发货", "没到", "未到账", "退款没到", "不回复", "没人管",
)
_DEFINITE_ISSUE_MARKERS = (
    "明显", "很多", "一堆", "严重", "一直", "根本", "这么久", "拖了",
    "还是没", "至今", "太久", "不处理", "没人管", "很失望", "太失望", "离谱",
)
_RECEIVED_MARKERS = ("收到以后", "收到后", "到手以后", "到手后", "实物")
_PRESALE_MARKERS = ("会不会", "容易", "可能", "有色差吗", "色差大不大")

# 写操作意图（退款/补偿/少发/改址/拦截）
# 注意：不含"赔付"（会误伤"赔付政策/赔付规则"这类纯咨询）
_WRITE_PATTERNS = (
    ("REFUND_REQUEST", ("退款", "退钱", "全额退", "仅退款", "退货")),
    ("COMPENSATION_REQUEST", ("赔偿", "补偿", "赔我", "赔偿我")),
    ("MISSING_ITEM_REQUEST", ("少发", "漏发", "少件", "空袋", "缺货", "缺件")),
    ("ADDRESS_CHANGE", ("改地址", "修改地址", "改收货", "换地址", "地址错了")),
    ("INTERCEPT_EXPRESS", ("拦截", "追回", "退回快递", "退回发货")),
)


def is_explicit_handoff(text: str) -> bool:
    """是否顾客明确要求转人工。"""
    return any(w in text for w in _EXPLICIT_HANDOFF_WORDS)


def detect_complaint(text: str) -> str | None:
    """检测投诉、负面情绪或明确的已发生问题，返回触发原因或 None。"""
    for w in _COMPLAINT_WORDS:
        if w in text:
            return w
    if any(w in text for w in _EMOTION_WORDS):
        return "强烈负面情绪"
    has_issue = any(w in text for w in _ISSUE_WORDS)
    if has_issue and any(w in text for w in _DEFINITE_ISSUE_MARKERS):
        return "明确不满反馈"
    if (
        has_issue
        and any(w in text for w in _RECEIVED_MARKERS)
        and not any(w in text for w in _PRESALE_MARKERS)
    ):
        return "明确售后问题"
    return None


def detect_escalation(text: str) -> str | None:
    """兼容旧接口：投诉或明确要求转人工，返回触发词或 None。"""
    esc = detect_complaint(text)
    if esc:
        return esc
    if is_explicit_handoff(text):
        return "明确要求转人工"
    return None


# 咨询语气词（含这些词的「退款/赔偿」是咨询，不是诉求）
_QUERY_MARKERS = ("是什么", "怎么", "如何", "能不能", "可以吗", "规则", "流程", "条件", "政策", "吗", "？", "?", "几天", "多久")


def detect_write_intent(text: str) -> str | None:
    """检测写操作意图（退款/补偿/改址/拦截），返回 reason_code 或 None。

    注意：只匹配"动作"意图词，避免把"退款规则怎么走"这类咨询误判为写操作。
    - 含疑问语气（是什么/怎么/规则/吗 等）→ 是咨询，返回 None；
    - 「赔 + 金额/赔我」才是明确赔付诉求。
    """
    # 咨询语气 → 不是写操作诉求
    if any(m in text for m in _QUERY_MARKERS):
        return None
    for code, words in _WRITE_PATTERNS:
        if any(w in text for w in words):
            return code
    # 「赔 + 金额」或「赔我」才是明确赔付诉求（避免误伤"赔付政策"咨询）
    import re
    if re.search(r"赔(?:我|你)?\s*\d", text) or "赔我" in text:
        return "COMPENSATION_REQUEST"
    return None


def analyze_tone(text: str) -> Tone:
    """分析用户语气（两级 + 明确转人工）。

    - 明确要求转人工（"转人工/人工客服/真人"）→ NEEDS_HUMAN
    - 投诉/负面情绪（"投诉/差评/气死/垃圾"等）→ NEGATIVE
    - 其余 → NORMAL
    """
    if is_explicit_handoff(text):
        return Tone.NEEDS_HUMAN
    if detect_complaint(text):
        return Tone.NEGATIVE
    return Tone.NORMAL


def decide(text: str) -> Decision:
    """统一决策入口：给定顾客消息，返回处置类型。"""
    # 1. 注入/异常 → 拒绝
    inbound = check_inbound(text)
    if not inbound.ok:
        return Decision(Disposition.REJECT, inbound.reason_code, inbound.detail, Tone.NORMAL)

    # 2. 明确要求转人工 / 投诉 → 转人工接管
    if is_explicit_handoff(text):
        return Decision(Disposition.HANDOFF_HUMAN, "EXPLICIT_HANDOFF", "明确要求转人工", Tone.NEEDS_HUMAN)
    if detect_complaint(text):
        return Decision(Disposition.HANDOFF_HUMAN, "ESCALATION", "投诉/负面情绪", Tone.NEGATIVE)

    # 3. 写操作意图 → 转人工审批
    write = detect_write_intent(text)
    if write:
        return Decision(Disposition.REQUIRE_APPROVAL, write, "高风险写操作", Tone.NORMAL)

    # 4. 明确咨询 → 自动回复（LLM 路径会处理具体回复）
    return Decision(Disposition.AUTO_REPLY, "AUTO", "普通咨询", Tone.NORMAL)
