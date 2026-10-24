"""组合查询规划提示（L4）：把"多维/筛选/排名/占比/转化"的问法翻译成 SQL 骨架。

为什么单独成模块（而不是继续加口径）：
评测剩下的 5 条失败全是**口径 × 多维度组合**——口径没缺，缺的是"先筛维度、
再分组、再排名/占比"的多步拼装。把这类组合的 SQL 形状写成**确定性提示**
注入生成 prompt，比让模型自己悟更稳；提示只在命中问法时出现，不污染简单题。

边界：提示是骨架，不是最终 SQL；列/表名仍由模型按 schema context 填。
宁可给"JOIN 哪些表、GROUP BY 哪些列、LIMIT 怎么来"，也不替模型写死数字。
"""
from __future__ import annotations

import re

_TOP_N = re.compile(r"(?:top|前)\s*(\d+)", re.IGNORECASE)


def build_query_plan_hint(question: str) -> str | None:
    """按问法返回 SQL 组合骨架；无命中返回 None。"""
    hints: list[str] = []
    q = question

    # 1) 多维度分组：各城市各渠道 / 各品类各渠道 ...
    if "各" in q and ("渠道" in q and ("城市" in q or "品类" in q or "来源" in q)):
        hints.append(
            "多维度分组：SELECT 维度1, 维度2, COUNT(*) AS cnt FROM biz.orders "
            "GROUP BY 维度1, 维度2 ORDER BY 维度1, 维度2；"
            "（城市=orders.city，渠道=orders.channel）"
        )
    elif "各城市" in q and ("排名" in q or "客单价" in q):
        hints.append(
            "分组 + 排名：GROUP BY city，聚合后 ORDER BY 指标 DESC；"
            "客单价 = SUM(pay_amount) / COUNT(DISTINCT user_id)"
        )

    # 2) TopN 热销
    top = _TOP_N.search(q)
    if top or "热销" in q:
        n = top.group(1) if top else "10"
        hints.append(
            f"TopN：order_items JOIN products 取商品，WHERE 品类条件，"
            f"GROUP BY 商品，ORDER BY SUM(quantity) DESC LIMIT {n}；"
            "商品名在 products.name，品类在 products.category"
        )

    # 3) 占比 / 贡献
    if "占比" in q or "贡献" in q:
        hints.append(
            "占比：用条件聚合算分子，不要先过滤再除——"
            "SUM(CASE WHEN 条件 THEN 金额 ELSE 0 END) / SUM(金额)，"
            "乘 100.0 并 ROUND(...,2)；金额口径 order_status IN ('paid','refunded')"
        )

    # 4) 双条件：某来源用户 买 某品类
    if ("来自" in q and "买" in q) or ("用户" in q and "买" in q and "品类" in q):
        hints.append(
            "双条件（用户属性 × 商品品类）：users JOIN orders ON user_id，"
            "再 JOIN order_items / products；WHERE 用户来源条件 AND 品类条件，"
            "聚合 quantity；两个条件必须同时成立（不要拆成两段）"
        )

    # 5) 转化率：浏览 -> 下单
    if "转化率" in q:
        hints.append(
            "转化率：分子=下单用户/订单，分母=浏览用户/日志，必须**同口径同周期**；"
            "traffic_logs 提供浏览行为，orders 提供下单；按 city 分组后相除，"
            "NULLIF(分母,0) 防除零，ROUND(100.0*分子/分母,2)"
        )

    return "\n".join(f"- {h}" for h in hints) if hints else None
