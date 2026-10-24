# DataCrew 分层压测报告（DC-8）

> 环境：Windows / Python 3.12 / uvicorn 单 worker / PostgreSQL 16（Docker，pgvector）
> / Redis 7（Docker）/ 数据 `generate_mock_data.py --scale 1.0`：**500,000 订单、
> 735,831 明细、50,000 用户、200,000 流量日志**。评测集已在同一份数据上重建
> （`eval/build_eval_set.py --reset`）。
> 客户端：`scripts/load_test.py`（httpx SSE，20 key 轮询；该脚本本轮修了"429/401
> 不计入错误率"的统计漏洞）。

## 1. mock 模式：并发 1/10/50/100/200

每档 100 请求（并发 200 档 200 请求）。"优化前"= checkpointer 池 10；"优化后"=
池 30（`CHECKPOINTER_POOL_MAX`，本轮已改为默认值）。

| 并发 | QPS 前 | QPS 后 | P50 前 | P50 后 | P95 后 | 错误率 |
|---|---|---|---|---|---|---|
| 1 | 8.2 | **8.7** | 102ms | **98ms** | 104ms | 0% |
| 10 | 23.1 | **27.6** | 259ms | **194ms** | 482ms | 0% |
| 50 | 23.3 | 25.7 | 1,440ms | 1,383ms | 1,744ms | 0% |
| 100 | 22.7 | **30.0** | 2,981ms | **1,964ms** | 2,010ms | 0% |
| 200 | 27.3 | **37.8** | 4,546ms | **3,440ms** | 3,866ms | 0% |

读法：**QPS 在 1→10 档从 8.7 涨到 27.6，之后基本在 26~38 平台**；延迟随并发
线性增长（排队）。系统在 ~30-40 QPS 处达到饱和，不是"越并发越快"。

### 瓶颈定位（证据，不是猜）

1. **不是 CPU**：压测进行中 `py-spy dump` 连续 5 次采样，主线程全部停在
   `asyncio select`（等待 IO），asyncio 线程池空闲。
2. **不是业务 DB 池**：压测中 `pg_stat_activity` 只有 1 条 active、47 条 idle，
   `wait_event=ClientRead`——PG 在等应用。
3. **不是 Redis 限流**：本档把每 key 限流调到 600/min（容量测试），100/200
   请求档 0 个 429；单独用 20/min 的默认配置跑 600 请求，只有 13~37% 成功
   ——限流器本身是**按设计生效**，不是故障。
4. **是 checkpointer 写入放大**：一次图 invoke 约 **11 次 checkpoint 写入**
   （aput + aput_writes），每次是独立 round-trip。隔离基准：池 30 下
   `aput` 单次 4.6ms、100 并发 542 写/s；1100 次写入 → ~2.0s，正好对上并发 100
   档 P50~2s。**把池从 10 调到 30 直接把天花板从 ~23 QPS 推到 ~38 QPS**。

结论：mock 链路的单请求成本主要是"11 次 checkpointer 写 × 连接池排队"，
LLM（mock）与 SQL 执行都不是瓶颈。可继续优化的是减少 checkpoint 写次数
（合并 writes / 批量提交），本轮只做了池大小这一个低风险修改。

## 2. 真实 DeepSeek：并发 10/20，各 50 请求

| 并发 | LLM 并发上限 | QPS | P50 | P95 | 成功/错误 | 结论 |
|---|---|---|---|---|---|---|
| 10 | 8 | 0.32 | 30,160ms | 37,822ms | 100% / 0% | 稳定但慢 |
| 20 | 8 | 0.30 | 59,886ms | 83,671ms | **76% / 24%** | 12 个 ReadTimeout（首事件 >30s） |
| 20 | 20 | 0.30 | 66,612ms | 69,497ms | **98% / 2%** | 超时消失，但更慢 |

### 瓶颈定位

- 日志里每次 `llm.chat` 的 p50 延迟：LLM 并发 8 时 **6,984ms**（p95 18.9s）；
  并发 20 时 **9,009ms**（p95 21.3s）。DeepSeek 没有返回 429。
- 一次问答平均 **5.9 次 LLM 调用**（curator + SQL 生成 + 洞察 + 自愈/重试），
  单次 7-9s → 理论吞吐 ≈ 并发/（5.9×8s）≈ 0.2-0.4 QPS，与实测 0.3 QPS 吻合。
- **本地信号量（8）不是吞吐瓶颈**：把它提到 20，QPS 不变，只是把排队从
  "应用侧"移到"DeepSeek 服务端"（单次延迟 7.0s→9.0s），并消除了客户端
  30s 首事件超时（76%→98%）。
- 24% ReadTimeout 的机制：请求在应用侧排队 >30s 才开始出第一个 SSE 事件，
  httpx 的读超时（30s）触发。**这是真实的用户可见问题**，但修复方向不是继续
  加并发（换不来吞吐），而是：①SSE 先发一个 `accepted/queued` 事件保活；
  ②前端/客户端把首事件超时与整体超时分开配置。两条都留作后续工作（本轮
  不改 SSE 契约）。

## 3. 本轮修改

1. `app/core/config.py`：新增 `checkpointer_pool_max`（默认 10→**30**），
   `app/agents/supervisor.py` 使用它；连接预算 = 业务池 20+20 + 30 = 70 < 100。
2. `scripts/load_test.py`：HTTP 失败（429 限流/401 鉴权）单列计数并计入错误率。
   修之前 600 请求实际只有 36.7% 成功，报告却打印"业务层失败 0"——压测工具
   自己先骗了人。
3. 未改：SSE 首事件保活、checkpoint 写入合并——前者改接口契约，后者要动
   langgraph 写入路径，均留待有明确收益证据后做（本轮证据只支持池大小）。

## 复现

```powershell
# mock
$env:LLM_MODE="mock"; $env:RATE_LIMIT_PER_MIN="600"
.\.venv\Scripts\python.exe -m app.main
.\.venv\Scripts\python.exe -X utf8 scripts/load_test.py --concurrency 100 --total 100

# real（成本约 ¥0.5/50 请求）
$env:LLM_MODE="real"; $env:LLM_MAX_CONCURRENCY="8"
.\.venv\Scripts\python.exe -m app.main
.\.venv\Scripts\python.exe -X utf8 scripts/load_test.py --concurrency 20 --total 50
```
