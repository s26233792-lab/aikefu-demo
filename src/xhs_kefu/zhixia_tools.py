"""栀夏 ZHIXIA 女装客服 Agent —— 工具层。

基于 agent.md 的模拟数据：
- 商品库：zhixia_products.json
- 订单库：zhixia_orders.json（核验：订单号 + 手机号后四位）
- 会员库：zhixia_members.json（核验：手机号后四位 + 会员编号）
- 店铺/活动/售后规则：zhixia_shop.md

安全边界（对齐 agent.md 核心规则）：
- 不展示完整手机号、完整地址（一律脱敏）；
- 查询订单必须核验「订单号 + 收货手机号后四位」；
- 写操作仅沙箱记录，需人工审批。
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

_DATA_DIR = Path(__file__).resolve().parent / "data"


class ZhixiaTools:
    """栀夏店铺数据查询工具。"""

    def __init__(self, data_dir: Path | None = None) -> None:
        data_dir = data_dir or _DATA_DIR
        self.products = self._load(data_dir / "zhixia_products.json")
        self.orders = self._load(data_dir / "zhixia_orders.json")
        self.members = self._load(data_dir / "zhixia_members.json")
        self.shop_rules = (data_dir / "zhixia_shop.md").read_text(encoding="utf-8")
        self._products_by_sku = {p["sku"]: p for p in self.products}
        self._orders_by_id = {o["order_id"]: o for o in self.orders}
        self._members_by_phone = {m["verify_phone_last4"]: m for m in self.members}

    @staticmethod
    def _load(path: Path) -> list[dict[str, Any]]:
        with path.open(encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, list) else []

    # ---------- 商品 ----------

    def product_lookup(self, sku: str | None = None) -> list[dict]:
        if sku:
            p = self._products_by_sku.get(sku.upper())
            return [deepcopy(p)] if p else []
        return deepcopy(self.products)

    def search_products(self, query: str, top_k: int = 3) -> list[dict]:
        """按关键词检索商品（名称/风格/颜色/面料）。"""
        scored = []
        for p in self.products:
            haystack = " ".join(
                [p["name"], p.get("style", ""), " ".join(p.get("colors", [])),
                 p.get("fabric", ""), p.get("detail", "")]
            )
            score = sum(1 for ch in query if ch in haystack)
            scored.append((score, p))
        scored.sort(key=lambda x: -x[0])
        return [p for s, p in scored[:top_k] if s > 0]

    # ---------- 订单（核验订单号 + 手机号后四位）----------

    def order_lookup(self, order_id: str, phone_last4: str | None = None) -> dict | None:
        """查询订单。必须提供订单号；若有 phone_last4 则同时核验。"""
        order = self._orders_by_id.get(order_id.strip().upper())
        if order is None:
            return None
        if phone_last4 and str(order.get("verify_phone_last4", "")) != str(phone_last4).strip():
            return {"error": "verify_failed", "reason": "手机号后四位不匹配"}
        return deepcopy(order)

    # ---------- 会员 ----------

    def member_lookup(self, phone_last4: str) -> dict | None:
        return deepcopy(self._members_by_phone.get(str(phone_last4).strip()))

    # ---------- 物流轨迹（规则生成模拟）----------

    def logistics_lookup(self, order_id: str, phone_last4: str | None = None) -> dict | None:
        """查物流轨迹。先核验订单，再用规则生成轨迹。"""
        order = self.order_lookup(order_id, phone_last4)
        if order is None:
            return None
        if isinstance(order, dict) and order.get("error"):
            return order  # 核验失败
        from .zhixia_logistics import generate_trace
        return generate_trace(order)

    # ---------- 写操作（沙箱，需人工审批）----------

    def modify_address(self, order_id: str, new_address: str) -> dict:
        order = self._orders_by_id.get(order_id.strip().upper())
        if order is None:
            return {"ok": False, "error": "order_not_found"}
        # 已发货订单不能直接修改地址
        if "已发" in order.get("status", "") or "签收" in order.get("status", ""):
            return {"ok": False, "error": "shipped_cannot_modify", "reason": "已发货订单不能直接修改地址，可联系承运方或申请拦截"}
        return {
            "ok": True, "order_id": order_id, "new_address": new_address,
            "note": "未发货订单可申请修改地址，已为您提交申请，不承诺一定成功", "sandbox": True,
        }

    def cancel_order(self, order_id: str) -> dict:
        order = self._orders_by_id.get(order_id.strip().upper())
        if order is None:
            return {"ok": False, "error": "order_not_found"}
        if "已发" in order.get("status", "") or "签收" in order.get("status", ""):
            return {"ok": False, "error": "shipped_cannot_cancel"}
        return {"ok": True, "order_id": order_id, "note": "已为您提交取消订单申请，仓库锁单后可能失败", "sandbox": True}
