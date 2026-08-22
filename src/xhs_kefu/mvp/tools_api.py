"""Tool Calling 层：商品/订单/物流三个 API 的统一接口。

MVP 用本地夹具数据模拟，接口签名对齐真实 API，后续可替换为真实调用。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class ProductAPI:
    """商品 API：查商品资料。"""

    def __init__(self) -> None:
        self.products = json.loads((_DATA_DIR / "products.json").read_text(encoding="utf-8"))

    def lookup(self, sku: str | None = None) -> list[dict]:
        if sku:
            return [p for p in self.products if p["sku"] == sku]
        return self.products


class OrderAPI:
    """订单 API：查订单事实。"""

    def __init__(self) -> None:
        self.orders = json.loads((_DATA_DIR / "orders.json").read_text(encoding="utf-8"))

    def lookup(self, order_id: str | None = None, customer_id: str | None = None) -> list[dict]:
        result = self.orders
        if order_id:
            result = [o for o in result if o["order_id"] == order_id]
        if customer_id:
            result = [o for o in result if o["customer_id"] == customer_id]
        return result


class LogisticsAPI:
    """物流 API：查物流状态。"""

    def __init__(self) -> None:
        self.shipments = json.loads((_DATA_DIR / "shipments.json").read_text(encoding="utf-8"))

    def lookup(self, order_id: str) -> dict | None:
        for s in self.shipments:
            if s["order_id"] == order_id:
                return s
        return None


class ToolRegistry:
    """工具注册表：商品/订单/物流三个 API 的统一入口。"""

    def __init__(self) -> None:
        self.product = ProductAPI()
        self.order = OrderAPI()
        self.logistics = LogisticsAPI()
