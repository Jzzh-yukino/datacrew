"""DC-4 规则优先澄清：确定性追问八类口径歧义，同时不误伤执行类问题。

两组断言同样重要：
1. 该问的（ambiguous 评测档）必须问——不依赖 LLM 的时好时坏；
2. 不该问的（simple_agg/multi_join/time_range/metric_def 里带同样术语的题）
   一个都不能问——「过度追问」是历史上 22/25 误追问的事故。
"""
from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agents.supervisor import build_graph
from app.infra.memory import forget_preferences, remember_preference
from app.tools.schema_registry import CLARIFICATION_RULES, match_clarification_rule
from app.tools.schema_registry import load_metric_definitions

pytestmark = pytest.mark.usefixtures("db_pools")


def _graph():
    return build_graph(checkpointer=InMemorySaver())


async def _pending_interrupt(graph, config) -> dict | None:
    state = await graph.aget_state(config)
    for task in state.tasks:
        if task.interrupts:
            return task.interrupts[0].value
    return None


SCHEMA_CONTEXT = {
    "tables": [{"name": "orders", "columns": ["user_id", "channel"]}],
    "metrics": [{"metric_name": "下单用户数"}],
    "matched_values": {"北京": [["users", "city"]], "上海": [["users", "city"]]},
}


class TestRuleMatching:
    @pytest.mark.parametrize("question,term", [
        ("这个月的用户数是多少", "用户数"),
        ("双 11 的转化率是多少", "转化率"),
        ("客单价是多少", "客单价"),
        ("活跃用户有多少", "活跃用户"),
        ("取消率怎么样", "取消率"),
        ("Top 用户的消费金额", "消费金额"),
        ("各品类的销售情况", "销售情况"),
        ("新用户的下单量", "新用户"),
    ])
    def test_ambiguous_questions_match(self, question: str, term: str) -> None:
        rule = match_clarification_rule(question, SCHEMA_CONTEXT)
        assert rule is not None, f"该追问却没命中：{question}"
        assert rule["term"] == term
        assert len(rule["options"]) >= 2

    def test_销售额与复购率不接管_评测集自身存在同义执行题(self) -> None:
        # 91「上个月各渠道的销售额是多少」与 66「11 月各渠道销售额」、
        # 71「本月至今的销售额」是同一句话的不同时态；94「整体的复购率」与
        # 76「整体复购率是多少」同理。任何规则能命中前者就会误杀后者，所以
        # 这两类交给 LLM 补充判定，规则不接管（评测报告里如实记为未追问）。
        assert match_clarification_rule("上个月各渠道的销售额是多少", SCHEMA_CONTEXT) is None
        assert match_clarification_rule("整体的复购率", SCHEMA_CONTEXT) is None

    @pytest.mark.parametrize("question", [
        # simple_agg / multi_join / time_range / metric_def 中带同样术语的执行题
        "各城市的用户数",
        "有登录行为的用户数",
        "只下过一单的用户数",
        "上海和北京的用户数对比",
        "整体客单价",
        "双 11 当天的客单价",
        "各城市客单价排名",
        "各渠道的平均客单价",
        "下单到支付的转化率",
        "浏览到下单的转化率（整体）",
        "整体取消率",
        "北京用户各品类的消费金额",
        "各渠道的实付销售额是多少",
        "双 11 当天的销售额",
        "各渠道的订单量排名",
        "新增用户数是多少",
        "各渠道的下单用户数",
    ])
    def test_execution_questions_not_hijacked(self, question: str) -> None:
        assert match_clarification_rule(question, SCHEMA_CONTEXT) is None, question


class TestRuleClarificationFlow:
    async def test_rule_triggers_interrupt_then_resumes(self) -> None:
        graph = _graph()
        cfg = {"configurable": {"thread_id": "t-rule-clarify"}}
        # s1 在 conftest 的测试会话清单里：每轮跑前清记忆，否则上一轮 resume
        # 沉淀的偏好会让本轮不再追问（规则澄清同样走记忆写路径）。
        await graph.ainvoke(
            {"question": "这个月的用户数是多少", "session_id": "s1", "status": "running"},
            cfg,
        )
        payload = await _pending_interrupt(graph, cfg)
        assert payload is not None and payload["type"] == "clarification"
        assert payload["options"] == ["下单用户数", "购买用户数", "日活用户"]

        final = await graph.ainvoke(Command(resume="日活用户"), cfg)
        assert final["status"] == "done"
        assert final["clarified_answer"] == "日活用户"

    async def test_remembered_preference_does_not_interrupt(self) -> None:
        session = "rule-pref"
        await forget_preferences(session)
        await remember_preference(session, "用户数", "日活用户")
        try:
            graph = _graph()
            cfg = {"configurable": {"thread_id": "t-rule-pref"}}
            final = await graph.ainvoke(
                {"question": "这个月的用户数是多少", "session_id": session, "status": "running"},
                cfg,
            )
            assert await _pending_interrupt(graph, cfg) is None, "偏好命中时不应再打断"
            assert final["status"] == "done"
            assert final["clarified_answer"] == "日活用户"
        finally:
            await forget_preferences(session)


async def test_options_reference_registry_metric_names() -> None:
    """纯口径名选项必须真的在注册表里（口径改名后这里立刻红）。"""
    names = {m["metric_name"] for m in await load_metric_definitions()}
    for rule in CLARIFICATION_RULES:
        for option in rule["options"]:
            if "（" not in option:
                assert option in names, f"{rule['term']} 的选项 {option} 不在口径注册表"
