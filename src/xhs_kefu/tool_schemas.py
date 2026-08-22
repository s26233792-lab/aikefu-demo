"""小红书千帆客服 Agent —— 工具 JSON Schema。

仅供 LLM function calling 使用；后端工具会再次校验参数，
模型提供的字段（如地址、金额、理由）一律按不可信数据处理。
"""
from __future__ import annotations

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "order_lookup",
            "description": "按订单号或顾客身份查询订单事实（状态、金额、收货地址、商品、备注）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "显式订单号，如 XHS-20260101-001；无法确定时留空。",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "logistics_lookup",
            "description": "按订单号查询物流状态与最新节点。",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "订单号，如 XHS-20260101-001。",
                    }
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "product_lookup",
            "description": "按 SKU 查商品资料；SKU 为空时返回店铺全量商品用于推荐。",
            "parameters": {
                "type": "object",
                "properties": {
                    "sku": {
                        "type": "string",
                        "description": "商品 SKU，如 SKU-JACKET-RED-M；留空则返回全量商品列表。",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "modify_address",
            "description": "修改收货地址（高风险写操作，需人工审批）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "订单号。"},
                    "new_address": {"type": "string", "description": "顾客提供的新完整地址。"},
                    "receiver_name": {"type": "string", "description": "收货人姓名。"},
                },
                "required": ["order_id", "new_address"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "intercept_express",
            "description": "快递拦截（高风险写操作，需人工审批；已签收订单不可拦截）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "订单号。"},
                    "reason": {"type": "string", "description": "拦截原因。"},
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compensate",
            "description": "协商补偿（需风控校验金额上限与证据）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "订单号。"},
                    "amount_cents": {
                        "type": "integer",
                        "description": "补偿金额（分）。",
                    },
                    "reason": {
                        "type": "string",
                        "enum": ["damaged", "quality_issue", "wrong_item", "missing_item", "logistics_exception", "change_of_mind"],
                        "description": "补偿理由。",
                    },
                },
                "required": ["order_id", "amount_cents", "reason"],
            },
        },
    },
]

TOOL_NAMES = [s["function"]["name"] for s in TOOL_SCHEMAS]
