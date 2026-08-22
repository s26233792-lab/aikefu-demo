"""小红书千帆客服 Agent —— Planner（意图识别）。

规则规划器：确定性意图分类，无需 LLM。
- rules 模式：唯一规划器；
- llm 模式：主链路走 llm_agent.LLMAgent（完整 Agent Loop），本规划器仅作
  LLM 失败时的降级兜底。

订单号/SKU/金额等实体由确定性解析器从顾客文本安全抽取，模型不得自由补全业务事实。
"""
from __future__ import annotations

import re
from typing import Protocol

from .domain import DecisionPlan, IncomingMessage, Intent

# 不用 \b（汉字前后 \b 不生效），改用非字母数字边界
_ORDER_RE = re.compile(r"(?<![A-Za-z0-9])XHS-\d{8}-\d{3}(?![A-Za-z0-9])", re.IGNORECASE)
_SKU_RE = re.compile(r"(?<![A-Za-z0-9])SKU-[A-Z0-9-]+(?![A-Za-z0-9])", re.IGNORECASE)
_MONEY_RE = re.compile(r"(\d+(?:\.\d{1,2})?)\s*元")


def extract_order_ids(text: str) -> list[str]:
    return list(dict.fromkeys(m.upper() for m in _ORDER_RE.findall(text)))


def extract_sku(text: str) -> str | None:
    m = _SKU_RE.search(text)
    return m.group(0).upper() if m else None


def extract_amount_cents(text: str) -> int | None:
    m = _MONEY_RE.search(text)
    if not m:
        return None
    try:
        return int(round(float(m.group(1)) * 100))
    except ValueError:
        return None


class Planner(Protocol):
    async def plan(
        self, message: IncomingMessage, history: list[dict[str, str]] | None = None
    ) -> DecisionPlan: ...


_INJECTION = ("忽略之前", "忽略以上", "系统提示词", "泄露密钥", "ignore previous", "system prompt")


def intent_from_text(text: str) -> DecisionPlan:
    """从文本确定性推断意图（供规则规划器与 LLM 降级共用）。"""
    lowered = text.lower()
    order_ids = extract_order_ids(text)
    order_id = order_ids[0] if order_ids else None
    sku = extract_sku(text)
    amount = extract_amount_cents(text)

    if any(m in lowered for m in _INJECTION):
        return DecisionPlan(Intent.SECURITY_REJECTED, security_reason="prompt_injection_marker")
    if any(w in text for w in ("拦截", "追回", "退回发货")):
        return DecisionPlan(Intent.INTERCEPT_EXPRESS, order_id=order_id)
    # 改地址：命中"地址"+"改/换/变更"，或"改成/改为"+地址实体
    if any(w in text for w in ("改地址", "修改地址", "改收货", "换地址", "地址错了", "地址改", "改下地址")) or (
        "地址" in text and any(w in text for w in ("改", "换", "变更"))
    ) or (
        any(w in text for w in ("改成", "改为", "更改到")) and any(c in text for c in ("省", "市", "区", "县", "路", "号楼", "室", "镇", "街道"))
    ):
        return DecisionPlan(Intent.MODIFY_ADDRESS, order_id=order_id)
    if any(w in text for w in ("补偿", "赔偿", "赔付", "赔我", "赔")):
        return DecisionPlan(
            Intent.COMPENSATION, order_id=order_id, amount_cents=amount, reason="unspecified"
        )
    if any(w in text for w in ("异常", "延误", "卡住", "丢件", "查不到", "一直不动")):
        return DecisionPlan(Intent.LOGISTICS_EXCEPTION, order_id=order_id)
    if any(w in text for w in ("物流", "快递", "到哪", "几天到", "签收", "催货")):
        return DecisionPlan(Intent.LOGISTICS_STATUS, order_id=order_id)
    if any(w in text for w in ("下单", "拍下", "付款", "催付", "怎么买", "购买")):
        return DecisionPlan(Intent.PLACE_ORDER, sku=sku)
    if sku or any(w in text for w in ("材质", "尺码", "参数", "怎么洗", "尺寸", "颜色", "推荐", "介绍", "价格", "多少钱", "钱", "售价", "报价", "价")):
        return DecisionPlan(
            Intent.PRODUCT_QUESTION if sku else Intent.PRODUCT_RECOMMEND, sku=sku
        )
    if any(w in lowered for w in ("hello", "hi", "你好", "您好", "在吗")):
        return DecisionPlan(Intent.GREETING)
    if any(w in text for w in ("写代码", "天气", "股票", "新闻", "讲笑话")):
        return DecisionPlan(Intent.OUT_OF_SCOPE)
    return DecisionPlan(Intent.UNKNOWN)


class RuleBasedPlanner:
    """确定性降级规划器，无需 LLM。

    在 llm 模式下作为降级兜底：LLM Agent Loop（llm_agent.LLMAgent）失败时回落到此。
    """

    async def plan(
        self, message: IncomingMessage, history: list[dict[str, str]] | None = None
    ) -> DecisionPlan:
        return intent_from_text(message.text)


def build_planner(mode: str, *, base_url: str, model: str, api_key: str | None) -> Planner:
    """返回规划器。

    - rules 模式：总是 RuleBasedPlanner；
    - llm 模式：主链路走 llm_agent.LLMAgent（在 runtime 里），此处仍返回
      RuleBasedPlanner 作为 LLM 失败时的降级兜底。
    """
    return RuleBasedPlanner()
