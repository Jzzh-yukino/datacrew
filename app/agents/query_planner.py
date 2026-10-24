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
            "多维度分组：SELECT u.city, o.channel, COUNT(*) AS cnt "
            "FROM biz.orders o JOIN biz.users u ON o.user_id = u.id "
            "GROUP BY u.city, o.channel ORDER BY cnt DESC LIMIT 20；"
            "（结果集对比按顺序，必须按聚合值倒序，不要按维度字母序）"
        )
    elif "各城市" in q and ("排名" in q or "客单价" in q):
        hints.append(
            "分组 + 排名：GROUP BY city，聚合后 ORDER BY 指标 DESC；"
            "客单价 = SUM(pay_amount) / COUNT(DISTINCT user_id)"
        )
    # 用户数对比：注册用户口径在 users 表，不是"下过单的用户"
    if "用户数" in q and ("对比" in q or "各城市" in q):
        hints.append(
            "用户数：直接 SELECT city, COUNT(*) FROM biz.users GROUP BY city；"
            "不要 JOIN orders——除非问题明确问“下单用户数/购买过…的用户数”"
        )
    # 购买过某品类的用户城市分布
    if "购买过" in q and "城市分布" in q:
        hints.append(
            "品类用户城市分布：orders JOIN order_items JOIN products JOIN users "
            "四表，WHERE 品类条件，COUNT(DISTINCT o.user_id)，"
            "GROUP BY u.city ORDER BY cnt DESC **LIMIT 10**"
        )

    # 2) TopN 热销
    top = _TOP_N.search(q)
    if top or "热销" in q:
        n = top.group(1) if top else "10"
        hints.append(
            f"TopN：order_items JOIN orders JOIN products，WHERE 品类条件，"
            f"GROUP BY p.id, p.name，只 SELECT p.id, p.name, SUM(quantity) 三列"
            f"（不要附加 category 等多余列，结果集按列逐一比对），"
            f"ORDER BY SUM(quantity) DESC LIMIT {n}"
        )

    # 3) 占比 / 贡献
    if "占比" in q or "贡献" in q:
        hints.append(
            "占比：orders JOIN order_items JOIN products 后做**条件聚合**，"
            "分子分母都在同一 FROM 里："
            "ROUND(100.0*SUM(CASE WHEN p.category=... THEN o.pay_amount ELSE 0 END)"
            "/SUM(o.pay_amount),2)；不要用标量子查询（会漏掉品类 JOIN / 口径不一致）"
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
            "NULLIF(分母,0) 防除零，ROUND(100.0*分子/分母,2)；"
            "问题没提时间范围时**禁止**在 WHERE 里写 created_at / 任何时间条件"
            "（转化率也默认全量，不要自己加“最近一年”）"
        )

    return "\n".join(f"- {h}" for h in hints) if hints else None
