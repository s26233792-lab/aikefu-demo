"""小红书千帆客服 Agent —— 工具层。

严格对齐参考架构：只读工具（查单/查物流/查商品）默认即可用；
写操作（改地址/拦截/补偿）必须经过风控与人工审批，且由后端校验权限，
模型绝不能直接携带后端地址、密钥、权限字段。

写操作在此 DEMO 里是"沙箱写"：只记录动作状态，不真正调用快递/千帆写接口。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .fixtures import Fixtures


class CommerceTools:
    """严格限范围的工具，只基于演示夹具。"""

    def __init__(self, fixtures: Fixtures) -> None:
        self.fixtures = fixtures

    # ---------- 只读工具 ----------

    def order_lookup(
        self,
        *,
        tenant_id: str,
        store_id: str,
        customer_id: str,
        order_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """按订单号或顾客身份查订单事实。"""
        if order_id:
            order = self.fixtures.order(order_id, customer_id=customer_id)
            return [deepcopy(order)] if order else []
        return deepcopy(
            [
                o
                for o in self.fixtures.orders
                if o["tenant_id"] == tenant_id
                and o["store_id"] == store_id
                and o["customer_id"] == customer_id
            ]
        )

    def logistics_lookup(self, *, order_id: str) -> dict[str, Any] | None:
        """查物流状态与最新节点。"""
        shipment = self.fixtures.shipment(order_id)
        return deepcopy(shipment) if shipment else None

    def product_lookup(
        self, *, tenant_id: str, store_id: str, sku: str | None
    ) -> list[dict[str, Any]]:
        """按 SKU 查商品资料；为空时返回店铺全量商品供推荐。"""
        if sku:
            product = self.fixtures.product(sku)
            return [deepcopy(product)] if product else []
        return deepcopy(
            [
                p
                for p in self.fixtures.products
                if p["tenant_id"] == tenant_id and p["store_id"] == store_id
            ]
        )

    # ---------- 写操作（沙箱）----------

    def modify_address(
        self, *, order_id: str, new_address: str, receiver_name: str
    ) -> dict[str, Any]:
        """修改收货地址（沙箱写，仅记录）。"""
        order = self.fixtures.order(order_id)
        if order is None:
            return {"ok": False, "error": "order_not_found"}
        return {
            "ok": True,
            "order_id": order_id,
            "old_address": order["shipping_address"],
            "new_address": new_address,
            "receiver_name": receiver_name,
            "sandbox": True,
        }

    def intercept_express(self, *, order_id: str, reason: str) -> dict[str, Any]:
        """快递拦截（沙箱写，仅记录）。"""
        shipment = self.fixtures.shipment(order_id)
        if shipment is None:
            return {"ok": False, "error": "shipment_not_found"}
        if shipment["status"] == "delivered":
            return {"ok": False, "error": "already_delivered"}
        return {
            "ok": True,
            "order_id": order_id,
            "tracking_id": shipment["tracking_id"],
            "reason": reason,
            "sandbox": True,
        }
