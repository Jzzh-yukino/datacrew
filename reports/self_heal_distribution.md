# 自愈分布：按错误类型的尝试数与成功率（DC-7）

> 样本：2026-10-25 三次真实 DeepSeek 评测（`LLM_MODE=real`），共 85 题：
> `eval_20261025_003426`（simple_agg 35）、`eval_20261025_003608`（metric_def 15）、
> `eval_20261025_004014`（simple_agg 35，percentile 修复后）。
> 数据源：逐题 JSONL 的 `heal_errors`（每次 executor 的 `error_type` + 数据库错误原文），
> 报告里的「自愈分布（按错误类型）」表由同一份记录生成。

## 分布

| 错误类型 | 涉及题数 | 自愈尝试数 | 最终通过 | 成功率 |
|---|---|---|---|---|
| 其他数据库错误（类型/数值语法） | 4 | 4 | 4 | 100% |
| 闸门拒绝（函数白名单） | 1 | 3 | 0 | 0% |
| 列不存在 | 0 | 0 | 0 | — |
| 表不存在 | 0 | 0 | 0 | — |
| 语法错误 | 0 | 0 | 0 | — |
| 超时 | 0 | 0 | 0 | — |
| **合计** | 5（5.9%） | 7 | 4 | 80% |

> 样本量只有 85 题、自愈触发率 5.9%——**这张表说明的是"我们开始按类型归因了"，
> 不是"各类错误的发生率"**。列不存在/语法/超时在本样本为 0 例，如实留空而非
> 用 mock 数据凑数（mock 的自愈是构造的 sale_amount 列不存在，不能当真实分布）。

## 两个由分布统计抓出来的真实缺陷

1. **中位数题被函数白名单误拦，且错误信息把参数当函数名**（`simple_agg` id 23
   「订单金额的中位数大致是多少」）：
   `PERCENTILE_CONT(0.5)` 的 sqlglot 节点 `func.name` 是参数 `"0.5"`，闸 6 同时
   用 `sql_name()` 和 `name` 做候选，报出「未授权的函数: **0.5**」并连续拒绝
   3 轮自愈。修复：①`percentile_cont` / `percentile_disc` 是只读标量聚合、
   结果单行，加入白名单；②函数名候选过滤成标识符（`_function_names`），
   错误信息不再出现"0.5"这种参数。修复后同一题 **0 次自愈直接通过**，
   报告里「闸门拒绝」类在后续 run 归零。
2. **`sqlglot` 类型化函数节点的 name 语义**：这类节点（PercentileCont）的
   `name` 不是函数名而是首参数，是同一条闸的通用坑——已用测试
   `test_function_candidate_names_ignore_numeric_argument` 锁死。

## 复现

```powershell
$env:LLM_MODE="real"
.\.venv\Scripts\python.exe -X utf8 eval/runner.py --category simple_agg
# 报告 reports/eval_*.md 的「自愈分布（按错误类型）」一节；
# 逐题明细在 reports/eval_*.jsonl 的 heal_errors 字段
```
