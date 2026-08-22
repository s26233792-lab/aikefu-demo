"""Router Agent：意图分类，把消息路由到 FAQ / 商品 / 售后 三个专业子 Agent。

路由规则（关键词 + 优先级）：
- 售后（优先级最高）：退款/退货/补偿/赔偿/物流/快递/改地址/拦截/投诉/破损/少发/错发
- 商品：推荐/材质/参数/尺码/价格/多少钱/库存/颜色
- FAQ：发货时间/到货时间/退换货规则/发票/怎么下单/联系人工
- 兜底：路由到 FAQ（通用问答）
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Route(StrEnum):
    FAQ = "faq"
    PRODUCT = "product"
    AFTERSALE = "aftersale"
    HANDOFF = "handoff"  # 直接转人工（投诉/情绪升级）


@dataclass(frozen=True, slots=True)
class RoutingResult:
    route: Route
    confidence: str = ""  # 命中的关键词，便于追踪
    reason: str = ""


# 售后关键词（优先级最高，含写操作和物流查询）
_AFTERSALE_WORDS = (
    "退款", "退货", "退钱", "补偿", "赔偿", "赔付", "物流", "快递", "改地址",
    "修改地址", "拦截", "破损", "坏了", "质量", "少发", "漏发", "错发", "发错",
    "到哪", "签收", "催货",
)
# 常见问题（FAQ）特征：询问"多久/什么时候/能…吗/可以…吗"这类关于规则的问题
_FAQ_SIGNAL_WORDS = ("多久", "什么时候", "何时", "几天", "能开发票吗", "可以开发票吗", "发票", "怎么下单", "如何购买")
# 商品关键词
_PRODUCT_WORDS = ("推荐", "材质", "参数", "尺码", "尺寸", "颜色", "价格", "多少钱", "库存", "卖点", "介绍")
# 投诉/情绪升级（直接转人工）
_HANDOFF_WORDS = ("投诉", "差评", "平台介入", "12315", "工商", "法院", "报警", "转人工", "人工客服", "气死", "垃圾")


class RouterAgent:
    """路由 Agent：决定消息交给哪个子 Agent。"""

    def route(self, text: str) -> RoutingResult:
        # 1. 投诉/情绪升级 → 转人工（最高优先级）
        for w in _HANDOFF_WORDS:
            if w in text:
                return RoutingResult(Route.HANDOFF, w, "投诉/情绪升级，转人工")

        # 2. FAQ 特征优先（"多久发货"算 FAQ，不算售后）
        for w in _FAQ_SIGNAL_WORDS:
            if w in text:
                return RoutingResult(Route.FAQ, w, "常见问题")

        # 3. 售后 → AfterSale Agent
        for w in _AFTERSALE_WORDS:
            if w in text:
                return RoutingResult(Route.AFTERSALE, w, "售后问题")

        # 4. 商品 → Product Agent
        for w in _PRODUCT_WORDS:
            if w in text:
                return RoutingResult(Route.PRODUCT, w, "商品咨询")

        # 5. 兜底 → FAQ Agent
        return RoutingResult(Route.FAQ, "default", "常见问题")
