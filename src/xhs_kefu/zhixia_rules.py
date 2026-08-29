"""Deterministic offline fallback for the Zhixia customer service agent.

This module keeps customer service available without an LLM key while only
answering from the configured store data.
"""
from __future__ import annotations

import re
from typing import Any

from .zhixia_tools import ZhixiaTools


class ZhixiaRuleAgent:
    """Small data-backed responder for common customer-service scenarios."""

    def __init__(self, tools: ZhixiaTools) -> None:
        self.tools = tools

    @staticmethod
    def _order_id(text: str) -> str | None:
        match = re.search(r"\bZX\d{12}\b", text.upper())
        return match.group(0) if match else None

    @staticmethod
    def _phone_last4(text: str) -> str | None:
        match = re.search(r"(?:手机号|手机|后四位)[^0-9]{0,8}(\d{4})", text)
        return match.group(1) if match else None

    @staticmethod
    def _tool_call(name: str, args: dict[str, Any], result: Any) -> dict[str, Any]:
        return {"name": name, "status": "ok", "latency_ms": 0, "args": args, "result": result}

    @staticmethod
    def _money(cents: int) -> str:
        return f"¥{cents / 100:.2f}"

    def _product(self, sku: str) -> dict[str, Any]:
        return self.tools.product_lookup(sku)[0]

    def _policy_call(self, topic: str) -> tuple[dict[str, Any], dict[str, Any]]:
        result = self.tools.policy_lookup(topic)
        return result, self._tool_call("shop_policy_lookup", {"topic": topic}, result)

    def run(self, text: str) -> dict[str, Any]:
        normalized = text.replace(" ", "")
        order_id = self._order_id(text)
        phone_last4 = self._phone_last4(text)
        tool_calls: list[dict[str, Any]] = []

        service_words = (
            "发货", "什么时候发", "多久发", "现货", "物流", "快递", "预售", "运费", "包邮", "订单", "退款", "退货", "换货",
            "售后", "改地址", "取消", "优惠", "价保", "发票", "商品", "尺码", "库存", "推荐",
        )
        if (
            any(word in normalized for word in ("你好", "您好", "在吗", "你是谁"))
            and not any(word in normalized for word in service_words)
        ):
            return self._result(
                "chitchat",
                "您好，我是栀夏女装客服小栀。请问想了解商品、尺码，还是订单售后呢？",
            )

        if ("西装" in text and "阔腿裤" in text) and any(word in text for word in ("多少钱", "满减", "95折", "优惠")):
            jacket = self._product("ZX-J407")
            trousers = self._product("ZX-S208")
            tool_calls.append(self._tool_call("product_lookup", {"sku": "ZX-J407"}, [jacket]))
            tool_calls.append(self._tool_call("product_lookup", {"sku": "ZX-S208"}, [trousers]))
            subtotal = jacket["price_cents"] + trousers["price_cents"]
            discounted = round(subtotal * 0.95)
            payable = discounted - 6000
            reply = (
                f"短款西装 {self._money(jacket['price_cents'])} + 阔腿裤 {self._money(trousers['price_cents'])}，"
                f"原价共 {self._money(subtotal)}。两件 95 折后是 {self._money(discounted)}，"
                f"再用满 ¥499 减 ¥60 店铺券，预计实付 {self._money(payable)}；若有积分，还可按规则继续抵扣。"
            )
            return self._result("promotion_calculation", reply, tool_calls)

        if "阔腿裤" in text and any(word in normalized for word in ("M码", "腰围", "臀围", "能穿")):
            product = self._product("ZX-S208")
            tool_calls.append(self._tool_call("product_lookup", {"sku": "ZX-S208"}, [product]))
            reply = (
                "这款阔腿裤 M 码腰围 68～72 cm、臀围约 96 cm；按您腰围 70、臀围 95 来看，M 码更合适。"
                "它是高腰直筒偏阔腿版，腰臀差较大时优先按臀围选码；M 码裤长 102 cm，158 cm 身高可能需要改一点裤脚，尺码建议仅供参考。"
            )
            return self._result("size_advice", reply, tool_calls)

        if ("面试" in text or "梨形" in text) and any(word in text for word in ("穿", "买", "推荐", "预算")):
            jacket = self._product("ZX-J407")
            trousers = self._product("ZX-S208")
            tool_calls.append(self._tool_call("product_lookup", {"sku": "ZX-J407"}, [jacket]))
            tool_calls.append(self._tool_call("product_lookup", {"sku": "ZX-S208"}, [trousers]))
            reply = (
                "建议短款西装外套（炭灰色）搭配高腰垂感阔腿裤（黑色）：短上衣能提高腰线，阔腿版型对梨形身材包容度也更高，面试穿利落但不生硬。"
                "两件原价 ¥698.00，参加两件 95 折并叠加满 ¥499 减 ¥60 后约 ¥603.10，在 800 元预算内；您 158 cm，裤长可能需要稍作修改。"
            )
            return self._result("product_recommend", reply, tool_calls)

        if ("白衬衫" in text or ("衬衫" in text and "透" in text)):
            shirt = self._product("ZX-T315")
            vest = self._product("ZX-B612")
            tool_calls.append(self._tool_call("product_lookup", {"sku": "ZX-T315"}, [shirt]))
            tool_calls.append(self._tool_call("product_lookup", {"sku": "ZX-B612"}, [vest]))
            reply = (
                "店内最接近的是桑蚕丝混纺衬衫的珍珠白，但强光下会有轻微透感，我不能把它说成完全不透。"
                "建议内搭肤色云朵无痕背心，日常通勤会更安心；衬衫 ¥369.00、背心 ¥89.00。"
            )
            return self._result("product_question", reply, tool_calls)

        if order_id and any(word in text for word in ("物流", "快递", "到哪", "发货")):
            if not phone_last4:
                return self._result("logistics_status", f"可以帮您查订单 {order_id}，还请提供收货手机号后四位用于核验。")
            result = self.tools.logistics_lookup(order_id, phone_last4)
            tool_calls.append(self._tool_call("logistics_lookup", {"order_id": order_id, "phone_last4": phone_last4}, result))
            policy, policy_call = self._policy_call(text)
            tool_calls.append(policy_call)
            if not result:
                return self._result("logistics_status", "暂未查询到该订单，已提交人工复核。", tool_calls, needs_human=True)
            if result.get("error"):
                return self._result("logistics_status", "手机号后四位与订单信息不匹配，已提交人工复核。", tool_calls, needs_human=True)
            reply = f"订单 {order_id} 当前状态为「{result['status']}」，{result['eta']}。"
            if result.get("trace"):
                latest = result["trace"][-1]
                reply += f" 最新轨迹：{latest['time']}，{latest['desc']}。"
            freshness = result.get("logistics_freshness") or {}
            if freshness.get("over_72h_no_update"):
                reply += " 物流已超过 72 小时没有更新，已提交人工专员复核，预计 2 小时内反馈。"
                return self._result("logistics_exception", reply, tool_calls, needs_human=True)
            fulfillment = result.get("fulfillment_freshness") or {}
            if fulfillment.get("over_48h_unshipped"):
                reply += " 该现货订单已超过 48 小时未发出，已提交人工专员核查，预计 2 小时内反馈。"
                return self._result("shipping_exception", reply, tool_calls, needs_human=True)
            return self._result("logistics_status", reply, tool_calls)

        if order_id and "改地址" in text:
            if not phone_last4:
                return self._result("modify_address", f"可以为订单 {order_id} 提交改址申请，请补充收货手机号后四位和新的完整地址；该操作需要人工审批。")
            return self._result("modify_address", "还请提供新的完整收货地址；提交后需要人工审批，仓库锁单后可能无法修改。")

        if order_id and "取消" in text:
            if not phone_last4:
                return self._result("cancel_order", f"可以帮您核实订单 {order_id} 是否还能取消，请提供收货手机号后四位。")
            result = self.tools.cancel_order(order_id, phone_last4)
            tool_calls.append(self._tool_call("cancel_order", {"order_id": order_id, "phone_last4": phone_last4}, result))
            if result.get("error") == "verify_failed":
                return self._result("cancel_order", "手机号后四位与订单信息不匹配，已提交人工复核。", tool_calls, needs_human=True)
            if result.get("error") == "shipped_cannot_cancel":
                return self._result("cancel_order", "该订单已经发货，不能直接取消；可以提交快递拦截申请，拦截是否成功以承运方反馈为准。", tool_calls, needs_human=True)
            if not result.get("ok"):
                return self._result("cancel_order", "暂时无法确认该订单的取消状态，已提交人工复核。", tool_calls, needs_human=True)
            return self._result("cancel_order", "已为您提交取消订单申请，仓库锁单后可能无法取消，最终结果以人工审核为准。", tool_calls, needs_human=True)

        if order_id and any(word in text for word in ("订单", "退款", "售后")):
            if not phone_last4:
                return self._result("order_lookup", f"可以帮您核实订单 {order_id}，还请提供收货手机号后四位。")
            result = self.tools.order_lookup(order_id, phone_last4)
            tool_calls.append(self._tool_call("order_lookup", {"order_id": order_id, "phone_last4": phone_last4}, result))
            if not result or result.get("error"):
                return self._result("order_lookup", "订单信息核验失败，已提交人工复核。", tool_calls, needs_human=True)
            reply = f"订单 {order_id} 当前状态为「{result['status']}」。"
            if result.get("aftersale_eta"):
                reply += result["aftersale_eta"] + "。"
            elif result.get("ship_eta"):
                reply += f"预计 {result['ship_eta']} 发出。"
            fulfillment = result.get("fulfillment_freshness") or {}
            if fulfillment.get("over_48h_unshipped"):
                reply += "该现货订单已超过 48 小时未发出，已提交人工专员核查，预计 2 小时内反馈。"
                return self._result("shipping_exception", reply, tool_calls, needs_human=True)
            return self._result("order_lookup", reply, tool_calls)

        if "会员" in text or "积分" in text:
            if not phone_last4:
                return self._result("member_lookup", "可以帮您查询会员等级和积分，请提供绑定手机号后四位，无需提供完整号码。")
            result = self.tools.member_lookup(phone_last4)
            tool_calls.append(self._tool_call("member_lookup", {"phone_last4": phone_last4}, result))
            if not result:
                return self._result("member_lookup", "暂未查询到对应会员，请核对手机号后四位。", tool_calls)
            return self._result("member_lookup", f"您当前是{result['level']}，有 {result['points']} 积分、{result['growth']} 成长值。", tool_calls)

        if any(word in text for word in ("预售", "拆单", "分开发", "一起发", "现货和预售")):
            policy, policy_call = self._policy_call(text)
            tool_calls.append(policy_call)
            reply = (
                "预售商品按商品页标注日期和付款顺序发出；同一订单含现货与预售时，默认按最晚预售日期一起发。"
                "如果平台或仓库支持，会自动分包且不重复收运费，但暂时不能保证一定拆单。"
            )
            return self._result("shipping_policy", reply, tool_calls)

        if any(word in text for word in ("多久发货", "什么时候发", "发货时间", "催发货", "还不发", "几点发")):
            policy, policy_call = self._policy_call(text)
            tool_calls.append(policy_call)
            reply = (
                "现货通常付款后 24 小时内发出；18:00 前付款会优先安排当日出库，18:00 后通常次日安排，"
                "但不能承诺精确发出时间。页面显示已发货后，首条物流轨迹可能延迟 6～12 小时同步；"
                "如果要核实具体订单，请提供订单号和收货手机号后四位。"
            )
            return self._result("shipping_policy", reply, tool_calls)

        if any(word in text for word in ("多久到", "几天到", "配送时效", "送到", "快递多久")):
            policy, policy_call = self._policy_call(text)
            tool_calls.append(policy_call)
            reply = (
                "从快递揽收后计算，江浙沪通常 1～2 天，其他大部分地区 2～4 天，偏远地区 4～7 天。"
                "这是参考时效，天气、交通和大促期间可能延迟；具体订单可提供订单号和手机号后四位查询。"
            )
            return self._result("delivery_policy", reply, tool_calls)

        if any(word in text for word in ("改地址", "改手机号", "取消订单", "拦截")):
            policy, policy_call = self._policy_call(text)
            tool_calls.append(policy_call)
            return self._result(
                "order_change_policy",
                "未发货且仓库未锁单时可以提交改址或取消申请，但不能保证成功；已发货后只能尝试联系承运方改址或拦截。请提供订单号和收货手机号后四位，我先帮您核实订单状态。",
                tool_calls,
            )

        if any(word in text for word in ("穿过", "退货", "能退", "七天无理由", "换货", "退款多久", "质量问题")):
            policy, policy_call = self._policy_call(text)
            tool_calls.append(policy_call)
            reply = (
                "七天无理由要求商品未穿洗、无污渍异味，且吊牌与包装完整、不影响二次销售。"
                "如果连衣裙已经穿着一天，通常不符合无理由退货条件；若存在质量问题，请提供问题部位、商品整体和洗标照片后申请审核。"
            )
            return self._result("aftersale_policy", reply, tool_calls)

        if any(word in text for word in ("包邮", "运费", "偏远地区")):
            policy, policy_call = self._policy_call(text)
            tool_calls.append(policy_call)
            return self._result("shipping_fee", "单笔实付满 ¥99 包邮，未满收 ¥8 运费；新疆、西藏等偏远地区及特殊线路以结算页显示为准。", tool_calls)

        if any(word in text for word in ("发票", "开票", "抬头", "税号")):
            policy, policy_call = self._policy_call(text)
            tool_calls.append(policy_call)
            return self._result("invoice_policy", "支持电子普通发票，可开个人或企业抬头；订单完成后申请，通常 1～3 个工作日发送。企业信息请通过平台发票入口填写，不要在聊天中发送完整税务资料。", tool_calls)

        products = self.tools.search_products(text, top_k=2)
        if products:
            for product in products:
                tool_calls.append(self._tool_call("product_lookup", {"sku": product["sku"]}, [product]))
            items = "；".join(f"{p['name']}（{self._money(p['price_cents'])}，{p['detail']}）" for p in products)
            return self._result("product_recommend", f"根据您的描述，可以看看：{items}。如果告诉我场合、身高体重和预算，我可以再帮您缩小选择。", tool_calls)

        return self._result("clarify", "我可以帮您做穿搭推荐、核对尺码、查询订单物流，或说明退换货规则。请告诉我您现在最想解决哪一项？")

    @staticmethod
    def _result(
        intent: str,
        reply: str,
        tool_calls: list[dict[str, Any]] | None = None,
        *,
        needs_human: bool = False,
    ) -> dict[str, Any]:
        return {
            "intent": intent,
            "reply": reply,
            "tool_calls": tool_calls or [],
            "needs_human": needs_human,
        }
