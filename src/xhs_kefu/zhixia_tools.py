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
import hashlib
import re
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

_DATA_DIR = Path(__file__).resolve().parent / "data"


class ZhixiaTools:
    """栀夏店铺数据查询工具。"""

    def __init__(self, data_dir: Path | None = None, *, store: Any | None = None) -> None:
        data_dir = data_dir or _DATA_DIR
        self.store = store
        self.products = self._load(data_dir / "zhixia_products.json")
        self.orders = self._load(data_dir / "zhixia_orders.json")
        self.members = self._load(data_dir / "zhixia_members.json")
        campaigns_path = data_dir / "zhixia_campaigns.json"
        self.campaigns = self._load(campaigns_path) if campaigns_path.exists() else []
        self.shop_rules = (data_dir / "zhixia_shop.md").read_text(encoding="utf-8")
        self._products_by_sku = {p["sku"]: p for p in self.products}
        self._orders_by_id = {o["order_id"]: o for o in self.orders}
        self._members_by_phone = {m["verify_phone_last4"]: m for m in self.members}
        self._policy_sections = self._parse_policy_sections(self.shop_rules)
        self._order_notes: dict[str, list[dict[str, Any]]] = {}

    @staticmethod
    def _load(path: Path) -> list[dict[str, Any]]:
        with path.open(encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, list) else []

    @staticmethod
    def _parse_policy_sections(markdown: str) -> dict[str, str]:
        """Index level-2 Markdown sections for targeted policy retrieval."""
        sections: dict[str, list[str]] = {}
        current: str | None = None
        for line in markdown.splitlines():
            if line.startswith("## "):
                current = line[3:].strip()
                sections[current] = []
            elif current is not None and line.strip():
                sections[current].append(line.strip())
        return {title: "\n".join(lines) for title, lines in sections.items()}

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

    # ---------- 店铺政策 ----------

    def policy_lookup(self, topic: str, *, now: datetime | None = None) -> dict[str, Any]:
        """Return the most relevant store-policy sections for a customer topic."""
        aliases: dict[str, tuple[str, ...]] = {
            "发货与履约": ("发货", "催发", "预售", "现货", "拆单", "分包", "合并", "缺货", "锁单", "出库"),
            "物流与配送": ("物流", "快递", "配送", "到货", "送达", "签收", "丢件", "派送", "轨迹", "驿站", "快递柜"),
            "运费与配送范围": ("运费", "包邮", "偏远", "新疆", "西藏", "拒收"),
            "订单修改与取消": ("改地址", "联系人", "取消", "拦截", "改尺码", "改颜色", "合并订单"),
            "商品与库存": ("商品", "库存", "尺码", "面料", "颜色", "洗护", "色差"),
            "优惠、价保与优惠计算": ("优惠", "券", "满减", "折扣", "价保", "补差", "活动", "价格", "预算", "实付", "多少钱"),
            "售前下单、催付与订单备注": ("下单", "购买", "怎么买", "立即购买", "待付款", "付款", "催付", "提醒付款", "核对订单", "订单明细", "备注"),
            "会员与积分": ("会员", "积分", "成长值", "等级"),
            "退换货与退款": ("退货", "换货", "退款", "取消", "到账", "七天", "质量", "错发", "少件", "破损", "售后"),
            "发票": ("发票", "开票", "税号", "抬头", "红冲"),
            "沟通、安全与隐私": ("验证码", "银行卡", "身份证", "隐私", "转账", "链接"),
            "转人工与处理时效": ("人工", "投诉", "赔付", "多久反馈", "处理时效"),
            "店铺与客服": ("客服时间", "几点", "营业", "在线"),
        }
        query = topic.strip()
        scores: list[tuple[int, str]] = []
        for title, keywords in aliases.items():
            score = sum(1 for keyword in keywords if keyword in query)
            if title in query:
                score += 3
            scores.append((score, title))
        scores.sort(key=lambda item: (-item[0], item[1]))
        selected = [title for score, title in scores if score > 0][:3]
        if not selected:
            selected = ["店铺与客服"]
        queried_at = (now or datetime.now().astimezone()).astimezone()
        result: dict[str, Any] = {
            "topic": query,
            "queried_at": queried_at.isoformat(timespec="minutes"),
            "sections": [
                {"title": title, "content": self._policy_sections.get(title, "")}
                for title in selected
            ],
        }
        if any(word in query for word in ("活动", "优惠", "券", "满减", "折扣", "价保", "实付", "多少钱", "价格")):
            result["campaigns"] = self._campaign_snapshots(queried_at)
        return result

    def _campaign_snapshots(self, now: datetime) -> list[dict[str, Any]]:
        """Return campaigns with a computed time status for reliable announcements."""
        snapshots: list[dict[str, Any]] = []
        for campaign in self.campaigns:
            item = deepcopy(campaign)
            try:
                start_at = datetime.fromisoformat(str(item["start_at"]))
                end_at = datetime.fromisoformat(str(item["end_at"]))
                current = now.astimezone(start_at.tzinfo) if start_at.tzinfo else now.replace(tzinfo=None)
                if current < start_at:
                    item["status"] = "upcoming"
                elif current > end_at:
                    item["status"] = "expired"
                else:
                    item["status"] = "active"
            except (KeyError, TypeError, ValueError):
                item["status"] = "invalid"
            snapshots.append(item)
        snapshots.sort(key=lambda item: (item.get("status") != "active", item.get("start_at", "")), reverse=False)
        return snapshots

    # ---------- 订单（核验订单号 + 手机号后四位）----------

    def order_lookup(self, order_id: str, phone_last4: str | None = None) -> dict | None:
        """查询订单。必须同时提供订单号与收货手机号后四位。"""
        order = self._orders_by_id.get(order_id.strip().upper())
        if order is None:
            return None
        if not phone_last4:
            return {"error": "verify_required", "reason": "需要收货手机号后四位"}
        if str(order.get("verify_phone_last4", "")) != str(phone_last4).strip():
            return {"error": "verify_failed", "reason": "手机号后四位不匹配"}
        result = deepcopy(order)

        # 订单行只保存 SKU；查询时关联商品主数据，避免模型自行猜商品名称。
        for item in result.get("items", []):
            product = self._products_by_sku.get(str(item.get("sku", "")).upper())
            if product:
                item["name"] = product.get("name")
                item["unit_price_cents"] = product.get("price_cents")

        if "待付款" in str(result.get("status", "")):
            result["payment_reminder_state"] = self._payment_reminder_state(result)

        # 给模型明确的查询时刻和物流时效事实，让它能识别过期 ETA，
        # 而不是把历史日期误说成“这两天”。
        now = datetime.now().astimezone()
        result["queried_at"] = now.isoformat(timespec="minutes")
        if "运输中" in str(result.get("status", "")) or "已发货" in str(result.get("status", "")):
            freshness: dict[str, Any] = {
                "over_72h_no_update": False,
                "eta_overdue": False,
            }
            event_match = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", str(result.get("latest_event", "")))
            if event_match:
                event_at = datetime.strptime(event_match.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=now.tzinfo)
                elapsed_hours = max(0, int((now - event_at).total_seconds() // 3600))
                freshness["hours_since_latest_update"] = elapsed_hours
                freshness["over_72h_no_update"] = elapsed_hours >= 72
            eta_dates = re.findall(r"\d{4}-\d{2}-\d{2}", str(result.get("eta", "")))
            if eta_dates:
                eta_end = datetime.strptime(eta_dates[-1], "%Y-%m-%d").date()
                freshness["eta_overdue"] = now.date() > eta_end
            result["logistics_freshness"] = freshness
        elif "待发货" in str(result.get("status", "")) or "拣货" in str(result.get("status", "")):
            is_preorder = "预售" in str(result.get("status", ""))
            fulfillment: dict[str, Any] = {
                "is_preorder": is_preorder,
                "over_24h_unshipped": False,
                "over_48h_unshipped": False,
                "ship_eta_overdue": False,
            }
            try:
                created_at = datetime.fromisoformat(str(result.get("created_at", "")))
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=now.tzinfo)
                elapsed_hours = max(0, int((now - created_at).total_seconds() // 3600))
                fulfillment["hours_since_payment"] = elapsed_hours
                if not is_preorder:
                    fulfillment["over_24h_unshipped"] = elapsed_hours >= 24
                    fulfillment["over_48h_unshipped"] = elapsed_hours >= 48
            except (TypeError, ValueError):
                pass
            ship_dates = re.findall(r"\d{4}-\d{2}-\d{2}", str(result.get("ship_eta", "")))
            if ship_dates:
                ship_eta_end = datetime.strptime(ship_dates[-1], "%Y-%m-%d").date()
                fulfillment["ship_eta_overdue"] = now.date() > ship_eta_end
            result["fulfillment_freshness"] = fulfillment
        return result

    @staticmethod
    def _payment_reminder_state(order: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
        """Evaluate reminder eligibility without sending anything to the customer."""
        current = (now or datetime.now().astimezone()).astimezone()
        policy = order.get("payment_reminder") or {}
        count = int(policy.get("count_24h", 0) or 0)
        max_count = int(policy.get("max_24h", 2) or 2)
        min_interval = int(policy.get("min_interval_minutes", 120) or 120)
        eligible = "待付款" in str(order.get("status", "")) and count < max_count
        reason = "eligible" if eligible else "frequency_limit"
        deadline_text = str(order.get("payment_deadline", ""))
        if deadline_text:
            try:
                deadline = datetime.fromisoformat(deadline_text)
                comparable_now = current.astimezone(deadline.tzinfo) if deadline.tzinfo else current.replace(tzinfo=None)
                if comparable_now >= deadline:
                    eligible = False
                    reason = "payment_expired"
            except ValueError:
                pass
        last_text = str(policy.get("last_at", ""))
        if eligible and last_text:
            try:
                last_at = datetime.fromisoformat(last_text)
                comparable_now = current.astimezone(last_at.tzinfo) if last_at.tzinfo else current.replace(tzinfo=None)
                if (comparable_now - last_at).total_seconds() < min_interval * 60:
                    eligible = False
                    reason = "interval_limit"
            except ValueError:
                pass
        return {
            "eligible": eligible,
            "reason": reason,
            "count_24h": count,
            "max_24h": max_count,
            "min_interval_minutes": min_interval,
            "payment_deadline": deadline_text or None,
        }

    def add_order_note(
        self,
        order_id: str,
        phone_last4: str,
        note: str,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Record a short, confirmed, non-sensitive order note in the demo sandbox."""
        order = self.order_lookup(order_id, phone_last4)
        if order is None:
            return {"ok": False, "error": "order_not_found"}
        if order.get("error"):
            return {"ok": False, **order}
        clean_note = re.sub(r"\s+", " ", str(note)).strip()
        if not clean_note:
            return {"ok": False, "error": "note_required"}
        if len(clean_note) > 80:
            return {"ok": False, "error": "note_too_long", "max_length": 80}
        if re.search(r"\b1\d{10}\b|验证码|支付密码|银行卡|身份证", clean_note):
            return {"ok": False, "error": "sensitive_content"}
        if not confirmed:
            return {
                "ok": False,
                "error": "confirm_required",
                "note_summary": clean_note,
                "message": "请向顾客复述备注摘要并取得明确确认后再写入",
            }
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        note_id = "note_" + hashlib.sha1(
            f"{order_id}|{clean_note}".encode("utf-8")
        ).hexdigest()[:12]
        record = {
            "id": note_id,
            "order_id": order_id,
            "note_summary": clean_note,
            "created_at": now,
            "sandbox": True,
            "fulfillment_guaranteed": False,
        }
        self._order_notes.setdefault(order_id, []).append(record)
        if self.store is not None:
            self.store.save_action(
                action_id=note_id,
                business_key=f"order_note|{order_id}|{hashlib.sha1(clean_note.encode('utf-8')).hexdigest()[:12]}",
                action_type="order_note",
                payload={
                    "order_id": order_id,
                    "note_summary": clean_note,
                    "fulfillment_guaranteed": False,
                },
                state="completed",
                created_at=now,
            )
        return {"ok": True, **record}

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

    def modify_address(self, order_id: str, phone_last4: str, new_address: str) -> dict:
        order = self.order_lookup(order_id, phone_last4)
        if order is None:
            return {"ok": False, "error": "order_not_found"}
        if order.get("error"):
            return {"ok": False, **order}
        # 已发货订单不能直接修改地址
        if "已发" in order.get("status", "") or "签收" in order.get("status", ""):
            return {"ok": False, "error": "shipped_cannot_modify", "reason": "已发货订单不能直接修改地址，可联系承运方或申请拦截"}
        return {
            "ok": True, "order_id": order_id, "new_address": new_address,
            "note": "未发货订单可申请修改地址，已为您提交申请，不承诺一定成功", "sandbox": True,
        }

    def cancel_order(self, order_id: str, phone_last4: str) -> dict:
        order = self.order_lookup(order_id, phone_last4)
        if order is None:
            return {"ok": False, "error": "order_not_found"}
        if order.get("error"):
            return {"ok": False, **order}
        if "已发" in order.get("status", "") or "签收" in order.get("status", ""):
            return {"ok": False, "error": "shipped_cannot_cancel"}
        return {"ok": True, "order_id": order_id, "note": "已为您提交取消订单申请，仓库锁单后可能失败", "sandbox": True}
