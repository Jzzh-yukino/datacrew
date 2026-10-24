"""状态机集成测试：澄清 / 自愈 / 审批 / 正常流，四条控制流全覆盖。

这些测试用 Mock LLM（LLM_MODE=mock）+ 真实数据库 + 内存 checkpointer ——
CI 不需要 API Key。每条控制流对应 README 里的一条面试叙事，测试即证据。
"""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agents.supervisor import build_graph
from app.core.config import get_settings

# db_pools（PG 池 + 记忆表隔离）在 tests/conftest.py，全模块共用
pytestmark = pytest.mark.usefixtures("db_pools")


def _graph():
    # interrupt/resume 的暂停状态必须由 checkpointer 持久化：
    # 测试用 InMemorySaver，生产用 PostgresSaver（见 supervisor.get_checkpointer）
    return build_graph(checkpointer=InMemorySaver())


async def _pending_interrupt(graph, config) -> dict | None:
    # 从图状态里取当前中断 payload（澄清问题 / 审批请求）
    state = await graph.aget_state(config)
    for task in state.tasks:
        if task.interrupts:
            return task.interrupts[0].value
    return None


class TestClarificationFlow:
    async def test_ambiguity_triggers_interrupt_then_completes(self) -> None:
        # 口径歧义 -> interrupt 追问 -> 用户回答 -> 恢复执行 -> 完成
        graph = _graph()
        config = {"configurable": {"thread_id": "t-clarify"}}

        # 第一遍：应在 schema_curator 处中断
        await graph.ainvoke(
            {"question": "上个月各渠道的销售额是多少", "session_id": "s1", "status": "running"},
            config,
        )
        payload = await _pending_interrupt(graph, config)
        assert payload is not None and payload["type"] == "clarification"
        assert "GMV" in payload["question"]

        # 恢复：用户回答"实付销售额"
        final = await graph.ainvoke(Command(resume="实付销售额"), config)
        assert final["status"] == "done"
        assert final.get("clarified_answer") == "实付销售额"
        assert final.get("insight")


class TestSelfHealingFlow:
    async def test_bad_sql_is_corrected_via_error_feedback(self) -> None:
        # Mock 首先生成错误列名 -> 闸拦截 -> 错误回灌 -> 修正后成功
        # （"销售额"问题会先触发澄清中断，先应答，自愈在恢复后的流程中发生）
        graph = _graph()
        config = {"configurable": {"thread_id": "t-heal"}}

        await graph.ainvoke(
            {"question": "上个月各渠道的销售额是多少", "session_id": "s2", "status": "running"},
            config,
        )
        final = await graph.ainvoke(Command(resume="实付销售额"), config)
        assert final["status"] == "done"
        # 自愈发生过：retry_count >= 1，且最终 SQL 用的是真实列 pay_amount
        assert final["retry_count"] >= 1
        assert "pay_amount" in final["sql"]
        assert "sale_amount" not in final["sql"]
        # 执行成功有真实数据
        assert final["sql_result"]["row_count"] == 3


class TestApprovalFlow:
    async def test_big_table_scan_requires_approval(self) -> None:
        # 大表无过滤查询 -> interrupt 审批 -> 批准 -> 执行（自动 LIMIT）
        graph = _graph()
        config = {"configurable": {"thread_id": "t-approval"}}

        await graph.ainvoke(
            {"question": "给我所有订单列表", "session_id": "s3", "status": "running"},
            config,
        )
        payload = await _pending_interrupt(graph, config)
        assert payload is not None and payload["type"] == "approval"
        assert payload["sql"].startswith("SELECT")

        # 批准后恢复
        final = await graph.ainvoke(Command(resume={"approved": True}), config)
        assert final["status"] == "done"
        assert final["sql_result"]["row_count"] <= get_settings().sql_row_limit

    async def test_rejection_fails_gracefully(self) -> None:
        # 拒绝审批 -> 优雅失败（有面向用户的错误话术，不是堆栈）
        graph = _graph()
        config = {"configurable": {"thread_id": "t-reject"}}

        await graph.ainvoke(
            {"question": "给我所有订单列表", "session_id": "s4", "status": "running"},
            config,
        )
        final = await graph.ainvoke(Command(resume={"approved": False}), config)
        assert final["status"] == "failed"
        assert "拒绝" in final["error"]


class TestHappyPath:
    async def test_unambiguous_question_completes_directly(self) -> None:
        # 无歧义问题 -> 无中断 -> 直达完成
        graph = _graph()
        config = {"configurable": {"thread_id": "t-happy"}}

        final = await graph.ainvoke(
            {"question": "上个月有多少下单用户", "session_id": "s5", "status": "running"},
            config,
        )
        assert final["status"] == "done"
        assert await _pending_interrupt(graph, config) is None
        # trace 记录四个节点全部执行
        nodes = [s["node"] for s in final["trace"]]
        assert nodes == ["schema_curator", "sql_generator", "executor", "insight_writer"]


class TestGuardBlocksThroughStateMachine:
    """安全闸的端到端行为：LLM 干坏事时，状态机层面的正确反应。

    mock 模拟"顺从型 LLM"（用户要删库它就写 DELETE）和"幻觉型 LLM"
    （schema 没有的列也硬写）——闸的价值不依赖 LLM 变聪明，
    这两类行为在 CI 无 API Key 时也必须守住。
    """

    async def test_write_intent_refused_before_generation(self) -> None:
        # DC-3：写意图在 curator 入口确定性拒答，不调用 LLM、不生成 SQL。
        # 旧行为（生成 DELETE 后被闸拦）依赖模型"顺从不改写"，真实模型会
        # 改写成 SELECT 作答（evaded），所以正确结局必须提前定。
        graph = _graph()
        config = {"configurable": {"thread_id": "t-danger"}}

        final = await graph.ainvoke(
            {"question": "帮我把所有订单删掉", "session_id": "s6", "status": "running"},
            config,
        )
        assert final["status"] == "done"
        assert final.get("refusal"), "意图闸门必须写入拒答话术"
        assert final.get("sql") is None
        assert final.get("sql_result") is None
        assert [s["node"] for s in final["trace"]] == ["schema_curator"]
        assert final["trace"][0]["detail"].get("refused") == "intent:write"

    async def test_unauthorized_table_blocked_before_execution(self) -> None:
        # 越权 SQL 在**执行前**被闸拦住：这里用直接调 executor 的方式验证
        # （DC-3 后"评测表/有哪些表"这类探测句会被意图闸门提前拒答，不再进入
        # executor；但闸本身对越权表的拦截不能因此失去端到端覆盖）。
        from app.agents.nodes import executor_node

        final = await executor_node(
            {
                "question": "看看评测表",
                "sql": "SELECT question FROM eval.queries",
                "session_id": "s6b",
            }
        )
        # 闸 4 表白名单拒绝 eval schema：错误回灌字段就位，status 仍是 running
        assert final.get("last_error")
        assert final.get("sql_result") is None
        assert final.get("status") != "done"

    async def test_hallucinated_column_blocked_then_fails(self) -> None:
        # schema 没有 color 列 -> DC-2 起在生成前就被可回答性闸门拒答
        # （status=done + 拒答话术，而不是让幻觉 SQL 撞列白名单后重试到 failed）。
        # 闸门本身对幻觉列的白名单拦截仍有 test_sql_guard 的单元覆盖。
        graph = _graph()
        config = {"configurable": {"thread_id": "t-hallucination"}}

        final = await graph.ainvoke(
            {"question": "商品的颜色分布是怎样的", "session_id": "s7", "status": "running"},
            config,
        )
        assert final["status"] == "done"
        assert "无法回答" in final["insight"]["summary"]
        assert "颜色" in final["insight"]["summary"]
        # 闸门在生成前拦住：既没有 SQL，也没有触发过执行
        assert final.get("sql") is None
        assert final.get("sql_result") is None
        assert [s["node"] for s in final["trace"]] == ["schema_curator"]


class TestAnswerabilityFlow:
    """DC-2：数据里没有的字段，在生成之前确定性拒答。"""

    async def test_unanswerable_question_refused_without_sql(self) -> None:
        graph = _graph()
        config = {"configurable": {"thread_id": "t-unanswerable"}}

        final = await graph.ainvoke(
            {"question": "昨天的物流单号有多少", "session_id": "s8", "status": "running"},
            config,
        )
        assert final["status"] == "done"
        assert "无法回答" in final["insight"]["summary"]
        assert "物流单号" in final["insight"]["summary"]
        assert final.get("sql") is None
        assert final.get("sql_result") is None
        assert final["trace"][-1]["node"] == "schema_curator"

    async def test_answerable_question_not_refused(self) -> None:
        # 可回答的问题必须继续走完生成/执行（防止闸门把 simple_agg 误杀）
        graph = _graph()
        config = {"configurable": {"thread_id": "t-answerable"}}

        final = await graph.ainvoke(
            {"question": "各渠道的订单量分别是多少", "session_id": "s9", "status": "running"},
            config,
        )
        assert final["status"] == "done"
        assert final.get("refusal") in (None, "")
        assert final.get("sql")
        assert final.get("sql_result") is not None
