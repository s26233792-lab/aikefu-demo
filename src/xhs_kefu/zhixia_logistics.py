"""栀夏 ZHIXIA 物流轨迹生成器（模拟）。

不存死数据——按订单状态 + 时间规则生成多节点物流轨迹，行为接近真实快递：
- 待发货：下单 → 仓库拣货中 → 预计发货（无运输轨迹）
- 运输中：揽收 → 发件枢纽 → 干线运输 → 收件枢纽 → 派件中
- 已签收：完整轨迹 + 签收节点
- 售后处理中：原订单轨迹 + 退件轨迹

每次生成基于订单下单时间推算，节点时间自然递推。
"""
from __future__ import annotations

from datetime import datetime, timedelta


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def generate_trace(order: dict, now: datetime | None = None) -> dict:
    """根据订单生成物流轨迹，返回 {carrier, status, trace, eta, latest_event}。

    order 需含：created_at, status, address_masked（目的地）, tracking_masked
    """
    now = now or datetime.now()
    origin = "浙江省杭州市"  # 发货地（agent.md）
    created = datetime.fromisoformat(order.get("created_at", now.isoformat()))
    tracking = order.get("tracking_masked", "") or "待揽收后生成"
    carrier = order.get("logistics", "中通快递")
    addr = order.get("address_masked", "目的地")
    status = order.get("status", "待发货")

    trace: list[dict] = []
    eta: str = ""

    # 发货时间（现货 24h 内）
    ship_at = created + timedelta(hours=20)

    if "待发货" in status or "拣货" in status:
        trace = [
            {"time": _fmt(created), "desc": "订单已提交，支付成功"},
            {"time": _fmt(ship_at), "desc": "仓库拣货中，即将发出"},
        ]
        eta = order.get("ship_eta", "预计今天发出")

    elif "运输中" in status or "已发货" in status:
        # 运输节点：揽收 → 发件枢纽 → 干线 → 收件枢纽 → 派件
        hub_out = ship_at + timedelta(hours=8)
        transit = hub_out + timedelta(hours=14)
        hub_in = transit + timedelta(hours=12)
        delivering = hub_in + timedelta(hours=6)
        trace = [
            {"time": _fmt(ship_at), "desc": f"【{origin}】快递员已揽收（{carrier}）"},
            {"time": _fmt(hub_out), "desc": f"【{origin}】快件已到达杭州转运中心"},
            {"time": _fmt(transit), "desc": "干线运输中，快件正发往目的地城市"},
            {"time": _fmt(hub_in), "desc": f"【目的地】快件到达 {addr.split('省')[0] if '省' in addr else addr} 转运中心"},
            {"time": _fmt(delivering), "desc": "正在派件，快递员将尽快联系您"},
        ]
        eta = order.get("eta", f"{delivering.strftime('%m-%d')} 至 {(delivering + timedelta(days=1)).strftime('%m-%d')}")
        # 最新节点用最后一条
        order["latest_event"] = trace[-1]["desc"]
        order["eta_display"] = eta

    elif "签收" in status:
        delivered = ship_at + timedelta(hours=40)
        trace = [
            {"time": _fmt(ship_at), "desc": f"【{origin}】快递员已揽收"},
            {"time": _fmt(ship_at + timedelta(hours=8)), "desc": "到达转运中心，干线运输中"},
            {"time": _fmt(ship_at + timedelta(hours=34)), "desc": f"到达目的地，安排派送"},
            {"time": _fmt(delivered), "desc": "已签收（签收人：本人/驿站代收）"},
        ]
        order["latest_event"] = trace[-1]["desc"]

    elif "售后" in status or "退货" in status:
        # 售后中：原轨迹 + 退件轨迹
        original = [
            {"time": _fmt(created), "desc": "订单已提交"},
            {"time": _fmt(ship_at), "desc": "已发货，快件揽收"},
        ]
        return_label = order.get("return_logistics", "顺丰速运")
        return_ship = ship_at + timedelta(hours=44)
        trace = original + [
            {"time": _fmt(ship_at + timedelta(hours=40)), "desc": "已签收"},
            {"time": _fmt(return_ship), "desc": f"退件已揽收（{return_label}），退货退款处理中"},
        ]
        order["latest_event"] = trace[-1]["desc"]
    else:
        trace = [
            {"time": _fmt(created), "desc": "订单已提交"},
            {"time": _fmt(ship_at), "desc": "商家处理中，将尽快发货"},
        ]

    return {
        "order_id": order.get("order_id"),
        "carrier": carrier,
        "tracking_masked": tracking,
        "status": status,
        "trace": trace,
        "latest_event": order.get("latest_event", trace[-1]["desc"] if trace else ""),
        "latest_event_time": trace[-1]["time"] if trace else "",
        "eta": eta or order.get("ship_eta", ""),
        "data_as_of": _fmt(now),
    }
