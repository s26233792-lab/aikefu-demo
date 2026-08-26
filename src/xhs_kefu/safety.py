"""小红书千帆客服 Agent —— 安全护栏。

对顾客消息与 Agent 回复做双向安全校验：
1. 敏感信息过滤：收货手机号、微信/个人联系方式、身份证、银行卡等；
2. 提示词注入防护：识别要求泄露系统提示/绕过规则的话术；
3. 回复内容校验：非空、不泄漏内部结构（JSON）、不含危险指令；
4. 高风险写操作判定：退款/改址/拦截/补偿等一律需人工审批。

护栏是防线，不是安全边界本身——写操作最终仍由后端风控校验。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 手机号
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
# 微信 / 个人联系方式
_CONTACT_RE = re.compile(r"(微信|vx|VX|微信号|加微|私聊|QQ号|qq号)\s*[:：]?\s*[a-zA-Z0-9_-]{5,}")
# 身份证
_ID_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
# 银行卡
_BANK_RE = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
# 短信验证码 / 支付密码（仅识别带明确语义标签的 4~8 位数字）
_OTP_RE = re.compile(r"(验证码|短信码|动态码|支付密码)\s*[:：为是]?\s*\d{4,8}", re.IGNORECASE)
# 提示词注入特征
_INJECTION_MARKERS = (
    "忽略之前", "忽略以上", "忽略所有", "系统提示词", "system prompt", "developer message",
    "泄露密钥", "泄露系统", "绕过规则", "ignore previous", "ignore all previous",
    "你是gpt", "你现在是", "解除限制", "jailbreak", "越狱",
)

# 内部结构泄漏检测（回复不应是 JSON 或含 trace/action id）
_INTERNAL_LEAK_RE = re.compile(r"(\btrace_[a-f0-9]{16,}\b|\baction_[a-f0-9]{16,}\b|dedup_auth|session_key)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class SafetyResult:
    ok: bool
    reason_code: str = ""
    detail: str = ""
    redacted: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "reason_code": self.reason_code, "detail": self.detail}


def redact_pii(text: str) -> str:
    """遮蔽文本中的敏感信息。"""
    text = _PHONE_RE.sub("[手机号已脱敏]", text)
    text = _CONTACT_RE.sub("[联系方式已脱敏]", text)
    text = _ID_RE.sub("[证件号已脱敏]", text)
    text = _BANK_RE.sub("[银行卡已脱敏]", text)
    text = _OTP_RE.sub("[验证码/支付密码已脱敏]", text)
    return text


def check_inbound(text: str) -> SafetyResult:
    """校验入站顾客消息。"""
    if any(marker in text.lower() for marker in _INJECTION_MARKERS):
        return SafetyResult(False, "PROMPT_INJECTION", "检测到提示词注入尝试")
    return SafetyResult(True, redacted=redact_pii(text))


def check_outbound(reply: str) -> SafetyResult:
    """校验出站回复（Agent 生成后、发送前）。"""
    if not reply or not reply.strip():
        return SafetyResult(False, "EMPTY_REPLY", "回复为空")
    if _INTERNAL_LEAK_RE.search(reply):
        return SafetyResult(False, "INTERNAL_LEAK", "回复疑似泄漏内部结构")
    # 回复中不应输出完整手机号/身份证/银行卡
    if _PHONE_RE.search(reply) or _ID_RE.search(reply) or _BANK_RE.search(reply) or _OTP_RE.search(reply):
        return SafetyResult(False, "PII_LEAK", "回复包含敏感信息，需人工确认")
    # 回复不应索取个人联系方式
    if _CONTACT_RE.search(reply):
        return SafetyResult(False, "CONTACT_SOLICIT", "回复疑似索取个人联系方式")
    return SafetyResult(True, redacted=redact_pii(reply))


# 高风险写操作（对应 tool 名）
HIGH_RISK_ACTIONS = {"modify_address", "intercept_express", "compensate", "approve_refund"}


def is_high_risk_intent(intent: str | None) -> bool:
    """判断意图是否属于需人工审批的高风险写操作。"""
    return intent in {"modify_address", "intercept_express", "compensation", "refund_request"}


def should_require_approval(intent: str | None, reply: str) -> tuple[bool, str]:
    """最终判断一条决策是否需要人工审批后才能发送。

    返回 (是否需要人工审批, 原因)。
    """
    if is_high_risk_intent(intent):
        return True, "HIGH_RISK_ACTION"
    out = check_outbound(reply)
    if not out.ok:
        return True, out.reason_code
    return False, ""
