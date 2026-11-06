# DataCrew 验收矩阵（双项目优化方案 DC-0…DC-9 + MEM-1 + L4 规划）

> 每一项指到「代码 / 测试 / 数字证据 / 提交」。数字来自 `reports/` 与 `eval.runs`。
> 最近更新：2026-11-06。

| 任务 | 交付 | 验证/数字 | 提交 |
|---|---|---|---|
| DC-0 | 提交工作区既有改动 + 修复 README D7 错位 | `reports/` 基线保留；pytest 全绿 | `1e446ea` |
| DC-1 | 评测可诊断：SQL/闸门/自愈/终态逐题留证 + JSONL + `eval.run_items` | `tests/test_eval_report.py`；`reports/eval_*.jsonl` | `ab70407` |
| DC-2 | 可回答性闸门：数据没有的字段生成前拒答 | 意图/可回答性测试；unanswerable 40%→100% | `e9eed1c` |
| DC-3 | 意图闸门（写/资源滥用/探测）+ should_refuse 判分 | `tests/test_sql_guard.py`；should_refuse 50%→100% | `87f7384` |
| DC-4 | 规则优先澄清：8 类口径歧义确定性追问 | `tests/test_clarification.py`；ambiguous 10%→80% | `88cdf30` |
| DC-5 | P1 真实评测对比表（分类别） | `reports/eval_20261025_*.md`；README §11 | `4d56a03` |
| DC-6 | 时间口径公式化（created_at/相对时间/大促窗口） | time_range 33%→93.3% | `a683b42` |
| DC-7 | 自愈分布按错误类型统计 + percentile 闸修复 | `reports/self_heal_distribution.md` | `8068270` |
| DC-8 | 50 万订单分层压测 + checkpointer 池 10→30；P50 4924→194ms、QPS 1.9→27.6 | `reports/load_test_analysis.md`；README §11 | `d9c91ff` |
| DC-9 | 全量 120 题真实评测 **97/120 = 80.8%**（Q1–Q7 后，分类别见报告）+ 留出集 v2 **29/30 = 96.7%**；pytest 301 | `b153444`、R2 |
| MEM-1 | Redis 短期对话记忆（focus/候选 + 30min TTL + PG 摘要兜底；默认关闭） | `tests/test_dialogue_memory.py`（12 项）；Redis/PG 回填、同 session 锁、ask 注入 | 本轮 |
| L4 | 组合查询规划提示（多维/筛选+排名/占比/双条件/转化） | `tests/test_query_planner.py`（6 条）；pytest 249；multi_join 25 题真实子集 72%→80%→84% | 本轮 |

## 当前门禁

- `ruff check app eval scripts` 全绿
- `pytest tests/ -q`：**313 passed**
- 前端 `npm test`（55 项）与 `npm run build`：全绿（本轮只改后端；已复跑）
- 两仓库 `git status` clean

## 已知边界（不藏）

- multi_join 跨 run 波动大（64~80%），全量 80.8% 不挑最好成绩；留出集 v2 96.7% 与训练集并列（v1 66.7% 为 RW-3 前代码，仅存档）；
- L4 提示是 SQL 骨架，不保证 5 条失败全部转绿，真实子集复测结果单独报；
- 真实模式攻击 SQL 拦截 5/10，差距已由意图闸门前置（DC-3），不宣称 10/10。
