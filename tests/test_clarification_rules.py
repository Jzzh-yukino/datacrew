"""RW-3 规则澄清：规则从口径注册表推导，不再手写八条术语。

关键断言：
1. 同一术语在注册表里对应 >=2 个不同 SQL 口径 -> 必须追问；
2. 已带口径限定词（实付/GMV/下单…）或单一语义（订单量/客单价）-> 不问；
3. 选项全部来自注册表 metric_name。
"""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agents.supervisor import build_graph
from app.infra.memory import forget_preferences, remember_preference
from app.tools.schema_registry import derive_clarification_rules, match_clarification_rule

pytestmark = pytest.mark.usefixtures("db_pools")

METRICS = [
    {"metric_name": "GMV", "definition": "GMV 口径的销售额，含取消退款", "sql_hint": "A"},
    {"metric_name": "实付销售额", "definition": "支付成功的销售额", "sql_hint": "B"},
    {"metric_name": "下单用户数", "definition": "产生订单的用户数", "sql_hint": "C"},
    {"metric_name": "购买用户数", "definition": "购买用户数", "sql_hint": "D"},
    {"metric_name": "下单转化率", "definition": "下单用户 / 活跃用户", "sql_hint": "E"},
    {"metric_name": "下单到支付转化率", "definition": "支付订单 / 下单订单", "sql_hint": "F"},
    {"metric_name": "订单量", "definition": "订单数 COUNT(*)", "sql_hint": "G"},
]
SCHEMA_CONTEXT = {"metrics": METRICS, "tables": []}


def _graph():
    return build_graph(checkpointer=InMemorySaver())


async def _pending_interrupt(graph, config) -> dict | None:
    state = await graph.aget_state(config)
    for task in state.tasks:
        if task.interrupts:
            return task.interrupts[0].value
    return None


class TestDerivedRules:
    def test_歧义术语从注册表推导(self) -> None:
        rules = {r["term"]: r for r in derive_clarification_rules(METRICS)}
        assert {"销售额", "用户数", "转化率"} <= set(rules)
        assert set(rules["销售额"]["options"]) == {"GMV", "实付销售额"}
        assert set(rules["用户数"]["options"]) == {"下单用户数", "购买用户数"}

    @pytest.mark.parametrize(
        "question,term",
        [
            ("这个月的销售额是多少", "销售额"),
            ("这个月的用户数是多少", "用户数"),
            ("双 11 的转化率是多少", "转化率"),
        ],
    )
    def test_歧义问题命中(self, question: str, term: str) -> None:
        rule = match_clarification_rule(question, SCHEMA_CONTEXT)
        assert rule is not None and rule["term"] == term

    @pytest.mark.parametrize(
        "question",
        [
            "各渠道的实付销售额是多少",
            "GMV 是多少",
            "各渠道的下单用户数",
            "下单转化率是多少",
            "各渠道的订单量",
            "整体客单价",
            "各城市退款率",
        ],
    )
    def test_不误伤执行题(self, question: str) -> None:
        assert match_clarification_rule(question, SCHEMA_CONTEXT) is None, question

    async def test_选项全部来自注册表(self) -> None:
        from app.tools.schema_registry import load_metric_definitions

        names = {m["metric_name"] for m in await load_metric_definitions()}
        for rule in derive_clarification_rules(await load_metric_definitions()):
            assert all(option in names for option in rule["options"])


class TestRuleClarificationFlow:
    async def test_规则触发中断并恢复(self) -> None:
        graph = _graph()
        cfg = {"configurable": {"thread_id": "t-rule-clarify"}}
        await graph.ainvoke(
            {"question": "这个月的用户数是多少", "session_id": "s1", "status": "running"},
            cfg,
        )
        payload = await _pending_interrupt(graph, cfg)
        assert payload is not None and payload["type"] == "clarification"
        assert "下单用户数" in payload["options"] and "购买用户数" in payload["options"]

        final = await graph.ainvoke(Command(resume="购买用户数"), cfg)
        assert final["status"] == "done"
        assert final["clarified_answer"] == "购买用户数"

    async def test_记住的偏好不再打断(self) -> None:
        session = "rule-pref"
        await forget_preferences(session)
        await remember_preference(session, "用户数", "购买用户数")
        try:
            graph = _graph()
            cfg = {"configurable": {"thread_id": "t-rule-pref"}}
            final = await graph.ainvoke(
                {"question": "这个月的用户数是多少", "session_id": session, "status": "running"},
                cfg,
            )
            assert await _pending_interrupt(graph, cfg) is None
            assert final["status"] == "done"
            assert final["clarified_answer"] == "购买用户数"
        finally:
            await forget_preferences(session)
