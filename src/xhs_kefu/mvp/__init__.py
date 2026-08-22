"""小红书/抖音/千牛 电商客服多 Agent MVP。

架构：
    Platform Adapter → Message API → Router Agent
        → FAQ Agent / Product Agent / Aftersale Agent
        → RAG（商品资料/FAQ/售后规则）→ Tool Calling（商品/订单/物流）
        → Guardrails → 自动回复 / 转人工

这是与单 Agent 版本（xhs_kefu 顶层）并行的一份极简多 Agent 实现。
"""
