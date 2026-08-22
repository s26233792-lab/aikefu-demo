"""小红书千帆客服 Agent —— 演示夹具数据。

三个演示场景各有一条顾客会话：
- 售前：CUST-9001 咨询商品 & 引导下单
- 售中：CUST-9002 查物流 & 修改地址 & 快递拦截
- 售后：CUST-9003 物流异常 & 协商补偿

所有数据仅用于演示，与参考架构 SyntheticFixtures 一致，不接真实 ERP。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class Fixtures:
    """从 JSON 夹具加载订单/商品/物流/证据。"""

    def __init__(self, data_dir: Path) -> None:
        self.orders = self._load(data_dir / "orders.json")
        self.products = self._load(data_dir / "products.json")
        self.shipments = self._load(data_dir / "shipments.json")
        # 方便按主键索引
        self._orders_by_id = {o["order_id"]: o for o in self.orders}
        self._products_by_sku = {p["sku"]: p for p in self.products}
        self._shipments_by_order = {
            s["order_id"]: s for s in self.shipments
        }

    @staticmethod
    def _load(path: Path) -> list[dict[str, Any]]:
        with path.open(encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, list):
            raise ValueError(f"Fixture must be a JSON list: {path.name}")
        return data

    def order(self, order_id: str, *, customer_id: str | None = None) -> dict | None:
        order = self._orders_by_id.get(order_id)
        if order is None:
            return None
        if customer_id is not None and order.get("customer_id") != customer_id:
            return None
        return order

    def products_by_store(self, store_id: str) -> list[dict]:
        return [p for p in self.products if p["store_id"] == store_id]

    def product(self, sku: str) -> dict | None:
        return self._products_by_sku.get(sku)

    def shipment(self, order_id: str) -> dict | None:
        return self._shipments_by_order.get(order_id)
