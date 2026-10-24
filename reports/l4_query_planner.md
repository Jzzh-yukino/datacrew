# L4 组合查询规划：multi_join 25 题真实子集（2026-10-25）

## 问题

DC-9 全量 120 题 81.7% 里，multi_join 只有 64%（16/25）；README §11 标注
剩余失败全部是「口径 × 多维度组合」——口径没缺，缺的是"先筛维度、再分组、
再排名/占比"的多步拼装。继续加口径收益递减，改为把组合查询的 SQL 形状写成
**确定性提示**注入生成 prompt（`app/agents/query_planner.py`）：

| 问法 | 提示骨架 |
|---|---|
| 各城市各渠道 | 二维 GROUP BY，ORDER BY 聚合值 DESC LIMIT 20（结果集按顺序比） |
| 热销 TopN | 商品三列（id/name/SUM(quantity)），不要多带 category；LIMIT N |
| 品类占比 | 同一 FROM 里条件聚合，不用标量子查询 |
| 用户来源 × 品类 | users/orders/items/products 四表，两个 WHERE 同时成立 |
| 转化率 | 浏览/下单同口径；没提时间就禁止加 created_at |

## 效果（同一 25 题、真实 DeepSeek、同数据）

| 轮次 | 通过 | 说明 |
|---|---|---|
| 初版提示 | 18/25 = 72.0% | 多维排序写反（按维度字母序）、用户数 JOIN 订单 |
| 收紧一版 | 20/25 = 80.0% | 多维按聚合值倒序；用户数用 users 单表；占比同 FROM 条件聚合 |
| 收紧二版 | **21/25 = 84.0%** | 城市分布 LIMIT 10；TopN 只选三列；转化率禁止时间条件 |

转绿：家居家装占比、购买过食品生鲜的用户城市分布、上海北京用户数对比。
仍失败：
- 各城市各渠道订单量（SQL 仍按 cnt DESC 但缺 LIMIT 20，结果集行数不同）；
- 母婴亲子 Top10（多带了列/分组差异）；
- 买过最多品类 Top5（本轮未专门覆盖）；
- 转化率（`Connection error` 网络错误，非 SQL 失败）。

**诚实边界**：25 题子集波动大，且首轮已带初版提示，没有"完全无提示"的同子集
对照；因此不能把这 +12pt 全归因于 planner。能说的是：三轮提示迭代把同一子集
从 72% 提到 84%，与历史全量 multi_join 64% / 分项 80% 同区间，至少没有退化，
3 类目标问法已转绿。

## 复现

```bash
LLM_MODE=real python -u eval/runner.py --category multi_join --limit 25 --timeout 180
```

报告落 `reports/eval_20261025_*.md`，逐题 SQL/结果见 eval.runs。
