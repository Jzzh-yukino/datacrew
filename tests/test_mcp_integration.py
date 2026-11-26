"""MCP 集成测试：启动 stdio server，通过 JSON-RPC 调用工具。

为什么值得单独立一个文件（D7 踩坑记）：
    2026-10-01 冒烟发现 python_sandbox 在 MCP server 里必超时、本机直跑却
    正常——根因是子进程默认继承父进程 stdin，而 MCP server 的 stdin 是
    stdio 协议管道，子进程拿着这个句柄永远完不成（communicate 死等到
    timeout）。**单元测试覆盖不到这个问题，因为触发条件是"运行在协议
    管道环境里"**——只有完整起一次 stdio 会话才能复现。这就是本文件的存在理由。

依赖：schema_search/sql_execute 需要 PG（CI 有 service 容器；本地没有则跳过）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from asyncpg import Connection
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from openai.resources.chat.completions import AsyncCompletions
from openai.types.chat import ChatCompletion

from app.agents.chat import chat_json
from app.agents.supervisor import build_graph
from app.core.config import get_settings
from app.tools import schema_registry

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class McpStdio:
    """最小 MCP stdio 客户端：换行分隔的 JSON-RPC（协议版本 2024-11-05）。"""

    def __init__(self) -> None:
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "app.tools.mcp_server"],
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
            bufsize=1,
            env=env,
        )
        self._next_id = 0
        self._send(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1"},
                },
            }
        )
        assert "result" in self._recv()
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, obj: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def _recv(self) -> dict:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            if line.strip():
                return json.loads(line)
        raise AssertionError("stream closed")

    def call(self, name: str, arguments: dict) -> dict:
        self._next_id += 1
        self._send(
            {
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        resp = self._recv()
        assert "result" in resp, resp
        assert not resp["result"].get("isError"), resp["result"]
        return json.loads(resp["result"]["content"][0]["text"])

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        self.proc.terminate()
        self.proc.wait(timeout=5)


@pytest.fixture()
def mcp():
    client = McpStdio()
    yield client
    client.close()


def _pg_reachable() -> bool:
    try:
        import asyncio

        import asyncpg

        from app.core.config import get_settings

        asyncio.run(asyncpg.connect(get_settings().pg_ro_dsn, timeout=2))
        return True
    except Exception:
        return False


needs_pg = pytest.mark.skipif(not _pg_reachable(), reason="PG 不可达（本地无库时跳过）")


def test_tools_discovered(mcp: McpStdio) -> None:
    """通过 MCP 工具发现接口获取注册的工具。"""
    mcp._next_id += 1
    mcp._send({"jsonrpc": "2.0", "id": mcp._next_id, "method": "tools/list"})
    resp = mcp._recv()
    names = sorted(t["name"] for t in resp["result"]["tools"])
    assert names == [
        "chart_gen",
        "describe_schema",
        "get_metric_specs",
        "python_sandbox",
        "resolve_dimension_values",
        "schema_search",
        "search_metrics",
        "sql_execute",
    ]


def test_sandbox_compute_inside_mcp(mcp: McpStdio) -> None:
    """D7 回归：沙箱在 MCP server 进程里必须正常返回（stdin 继承事故的守卫）。"""
    started = time.perf_counter()
    r = mcp.call("python_sandbox", {"code": "print(sum(range(101)))"})
    assert r["ok"] is True
    assert r["stdout"].strip() == "5050"
    assert time.perf_counter() - started < 5  # 死等现象下这里会是 timeout_s


def test_sandbox_network_blocked_inside_mcp(mcp: McpStdio) -> None:
    r = mcp.call(
        "python_sandbox",
        {
            "code": "import socket; socket.create_connection(('8.8.8.8', 53), timeout=2)",
            "timeout_s": 5,
        },
    )
    assert r["ok"] is False
    assert r["error_type"] == "blocked"


def test_sandbox_timeout_inside_mcp(mcp: McpStdio) -> None:
    r = mcp.call("python_sandbox", {"code": "import time; time.sleep(30)", "timeout_s": 2})
    assert r["ok"] is False
    assert r["error_type"] == "timeout"


def test_chart_gen_inside_mcp(mcp: McpStdio) -> None:
    r = mcp.call(
        "chart_gen",
        {
            "chart_type": "bar",
            "data": [["app", 100], ["h5", 20]],
            "title": "回归",
        },
    )
    assert r["ok"] is True
    assert r["url"].startswith("/files/")


@needs_pg
def test_schema_search_inside_mcp(mcp: McpStdio) -> None:
    # 注意：schema_search 没有 {"ok": ...} 信封——它没有失败路径，
    # 检索不到就兜底返回全部表（命中数趋近 0 的失败由 LLM 侧感知）。
    # 契约不对称是刻意为之：错误信封只写给"会失败"的工具。
    r = mcp.call("schema_search", {"question": "各渠道的实付销售额"})
    assert r["tables"], r
    assert any("orders" == tb["name"] for tb in r["tables"])


@needs_pg
def test_describe_schema_returns_order_user_foreign_key(mcp: McpStdio) -> None:
    result = mcp.call("describe_schema", {"table": "orders"})
    assert result["foreign_keys"] == [
        {"columns": ["user_id"], "referenced_table": "users", "referenced_columns": ["id"]}
    ]


@needs_pg
def test_describe_schema_returns_order_column_types(mcp: McpStdio) -> None:
    result = mcp.call("describe_schema", {"table": "orders"})
    columns = {column["name"]: column["data_type"] for column in result["columns"]}
    assert {
        "created_at": "timestamp with time zone",
        "pay_amount": "numeric",
    }.items() <= columns.items()


@needs_pg
def test_resolve_dimension_values_maps_beijing_to_city(mcp: McpStdio) -> None:
    result = mcp.call("resolve_dimension_values", {"value": "北京"})
    assert result["matches"] == [{"table": "users", "column": "city"}]


@needs_pg
@pytest.mark.usefixtures("db_pools")
@pytest.mark.parametrize(
    ("tool_name", "arguments", "response_key", "expected"),
    [
        (
            "describe_schema", {"table": "orders"}, "foreign_keys",
            [{"columns": ["user_id"], "referenced_table": "users", "referenced_columns": ["id"]}],
        ),
        (
            "resolve_dimension_values", {"value": "北京"}, "matches",
            [{"table": "users", "column": "city"}],
        ),
    ],
    ids=["foreign-key", "dimension-value"],
)
async def test_model_receives_schema_evidence_from_mcp(
    monkeypatch, tool_name: str, arguments: dict, response_key: str, expected: list
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_mode", "real")
    monkeypatch.setattr(settings, "langfuse_public_key", "")
    tool_call = {
        "id": "schema_lookup",
        "type": "function",
        "function": {"name": tool_name, "arguments": json.dumps(arguments)},
    }

    async def create_completion(_client, *, messages, model, tools=(), **_kwargs):
        last_message = messages[-1]
        if last_message["role"] == "tool":
            assert last_message["tool_call_id"] == "schema_lookup"
            message = {"role": "assistant", "content": last_message["content"]}
            finish_reason = "stop"
        else:
            assert tool_name in {tool["function"]["name"] for tool in tools}
            message = {"role": "assistant", "tool_calls": [tool_call]}
            finish_reason = "tool_calls"
        return ChatCompletion.model_validate({
            "id": "schema-completion",
            "model": model,
            "created": 0,
            "object": "chat.completion",
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        })

    monkeypatch.setattr(AsyncCompletions, "create", create_completion)
    result = await chat_json(
        [{"role": "user", "content": "请用工具查找北京用户订单的相关字段，并返回 JSON。"}],
        with_schema_retrieval=True,
    )
    assert result[response_key] == expected


@needs_pg
@pytest.mark.usefixtures("db_pools")
@pytest.mark.parametrize(
    ("question", "expected_sql", "controlled"),
    [
        (
            "北京各渠道的订单量从多到少排行",
            "SELECT o.channel, COUNT(*) AS order_count FROM biz.orders AS o JOIN biz.users AS u "
            "ON u.id = o.user_id WHERE u.city = '北京' GROUP BY o.channel "
            "ORDER BY order_count DESC",
            False,
        ),
        (
            "北京的订单量",
            "SELECT COUNT(*) AS order_count FROM biz.orders AS o JOIN biz.users AS u "
            "ON u.id = o.user_id WHERE u.city = '北京'",
            True,
        ),
        (
            "2025年11月北京的订单量",
            "SELECT COUNT(*) AS order_count FROM biz.orders AS o JOIN biz.users AS u "
            "ON u.id = o.user_id WHERE u.city = '北京' "
            "AND o.created_at >= '2025-11-01 00:00:00+00:00' "
            "AND o.created_at < '2025-12-01 00:00:00+00:00'",
            True,
        ),
        (
            "北京各渠道的订单量",
            "SELECT o.channel, COUNT(*) AS order_count FROM biz.orders AS o JOIN biz.users AS u "
            "ON u.id = o.user_id WHERE u.city = '北京' GROUP BY o.channel ORDER BY o.channel",
            True,
        ),
    ],
    ids=[
        "free-sql-evidence", "controlled-order-count", "controlled-month-order-count",
        "controlled-channel-order-count",
    ],
)
async def test_sql_generation_uses_city_evidence(
    monkeypatch, question: str, expected_sql: str, controlled: bool
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_mode", "real")
    monkeypatch.setattr(settings, "langfuse_public_key", "")

    async def create_completion(_client, *, messages, model, tools=(), **_kwargs):
        finish_reason = "stop"
        if messages[-1]["role"] == "tool":
            message = {"role": "assistant", "content": '{"answerable":true,"ambiguity":null}'}
        elif isinstance(tools, list):
            message = {"role": "assistant", "tool_calls": [{
                "id": "city_lookup",
                "type": "function",
                "function": {"name": "resolve_dimension_values", "arguments": '{"value":"北京"}'},
            }]}
            finish_reason = "tool_calls"
        else:
            assert not controlled, "单城市的已注册订单量应由编译器生成 SQL"
            assert "北京 → biz.users.city" in messages[0]["content"]
            assert "订单渠道使用 biz.orders.channel" in messages[0]["content"]
            assert "用户注册来源使用 biz.users.channel_source" in messages[0]["content"]
            message = {"role": "assistant", "content": json.dumps({"sql": expected_sql})}
        return ChatCompletion.model_validate({
            "id": "dimension-evidence",
            "model": model,
            "created": 0,
            "object": "chat.completion",
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        })

    monkeypatch.setattr(AsyncCompletions, "create", create_completion)
    graph = build_graph(checkpointer=InMemorySaver())
    result = await graph.ainvoke(
        {"question": question, "session_id": "dimension-evidence-test"},
        config={"configurable": {"thread_id": "dimension-evidence-test"}},
        interrupt_before=["executor"],
    )
    assert result["sql"] == expected_sql
    assert result["sql_origin"] == ("controlled_compiler" if controlled else "free_sql")


@needs_pg
@pytest.mark.usefixtures("db_pools")
async def test_new_metric_requires_confirmation_and_is_reused_in_its_session(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_mode", "real")
    monkeypatch.setattr(settings, "langfuse_public_key", "")
    definition = {
        "name": "优质订单量",
        "population": "订单",
        "formula": "统计符合筛选条件的订单行数",
        "filters": ["实付金额大于 100 元"],
        "time_basis": "下单时间",
        "unit": "单",
    }

    sql = "SELECT COUNT(*) AS order_count FROM biz.orders WHERE pay_amount > 100"
    confirmed = False

    async def create_completion(_client, *, messages, model, tools=(), **_kwargs):
        context = "\n".join(message["content"] for message in messages)
        already_known = "已确认会话口径" in context and definition["formula"] in context
        if isinstance(tools, list) and tools:
            if "新增指标" in messages[-1]["content"]:
                body = {"answerable": True, "ambiguity": None, "metric_draft": definition}
            else:
                body = {"answerable": already_known, "ambiguity": None}
        elif "PostgreSQL 专家" in messages[0]["content"]:
            assert confirmed and already_known, "生成 SQL 时必须使用已确认的草案"
            body = {"sql": sql}
        else:
            body = {"summary": "统计完成"}
        return ChatCompletion.model_validate({
            "id": "metric-draft",
            "model": model,
            "created": 0,
            "object": "chat.completion",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": json.dumps(body)},
                "finish_reason": "stop",
            }],
        })

    monkeypatch.setattr(AsyncCompletions, "create", create_completion)
    graph = build_graph(checkpointer=InMemorySaver())
    session = uuid4().hex
    request = {"session_id": session, "api_key_hash": "metric-draft-test"}
    config = {"configurable": {"thread_id": session}}
    await graph.ainvoke({
        "question": "新增指标优质订单量，定义为实付金额大于 100 元的订单数，按下单时间统计",
        **request,
    }, config=config)
    state = await graph.aget_state(config)
    (pending,) = (item.value for task in state.tasks for item in task.interrupts)
    assert pending["type"] == "metric_confirmation"
    assert pending["draft"]["definition"] == definition
    assert pending["draft"]["version"] == 1
    assert pending["draft"]["draft_id"]
    assert len(pending["draft"]["definition_digest"]) == 64
    assert pending["draft"] == state.values["metric_draft"]
    assert state.values.get("sql") is None
    assert state.values.get("sql_result") is None

    confirmed = True
    draft = pending["draft"]
    decision = {key: draft[key] for key in ("draft_id", "version", "definition_digest")}
    result = await graph.ainvoke(Command(resume={**decision, "approved": True}), config=config)
    assert result["sql_result"]["ok"]
    assert result["sql_origin"] == "free_sql"

    followup = {**request, "question": "优质订单量是多少？"}
    fresh_graph = build_graph()
    reused = await fresh_graph.ainvoke(followup)
    assert reused["sql_result"]["ok"]
    for other_scope in ({"session_id": uuid4().hex}, {"api_key_hash": "another-key"}):
        isolated = await fresh_graph.ainvoke({**followup, **other_scope})
        assert isolated["refusal"]
        assert isolated.get("sql") is None


@needs_pg
@pytest.mark.usefixtures("db_pools")
@pytest.mark.parametrize(
    ("question", "followup"),
    [
        ("北京的订单量", "北京的订单量"),
        ("北京各渠道的订单量", "北京各渠道的订单量分别是多少？"),
    ],
    ids=["same-question", "same-plan-different-wording"],
)
async def test_repeated_city_order_count_reuses_sql_and_runs_executor(
    monkeypatch, question: str, followup: str
) -> None:
    monkeypatch.setattr(get_settings(), "llm_mode", "mock")
    request = {
        "question": question,
        "session_id": uuid4().hex,
        "api_key_hash": "sql-reuse-test",
    }
    graph = build_graph()
    first = await graph.ainvoke(request)
    second = await graph.ainvoke({**request, "question": followup})

    assert [first["sql_origin"], second["sql_origin"]] == [
        "controlled_compiler", "controlled_cache",
    ]
    assert second["sql"] == first["sql"]
    assert second["sql_result"]["ok"]


@needs_pg
@pytest.mark.usefixtures("db_pools")
async def test_city_order_count_recompiles_when_metric_version_changes(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "llm_mode", "mock")
    monkeypatch.setattr(schema_registry, "ORDER_COUNT_METRIC_VERSION", 1)
    request = {
        "question": "北京的订单量",
        "session_id": uuid4().hex,
        "api_key_hash": "sql-reuse-test",
    }
    graph = build_graph()
    first = await graph.ainvoke(request)
    cached = await graph.ainvoke(request)
    monkeypatch.setattr(schema_registry, "ORDER_COUNT_METRIC_VERSION", 2)
    updated = await graph.ainvoke(request)
    updated_cached = await graph.ainvoke(request)

    runs = (first, cached, updated, updated_cached)
    assert [run["sql_origin"] for run in runs] == [
        "controlled_compiler", "controlled_cache", "controlled_compiler", "controlled_cache",
    ]
    assert all(run["sql_result"]["ok"] for run in runs)


@needs_pg
@pytest.mark.usefixtures("db_pools")
async def test_previous_month_city_order_count_reuses_sql_within_data_month(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "llm_mode", "mock")
    fetchval = Connection.fetchval
    data_as_of = date(2025, 12, 2)

    async def fetch_with_order_date(conn, query, *args, **kwargs):
        if "max(created_at)" in query and "FROM biz.orders" in query:
            return data_as_of
        return await fetchval(conn, query, *args, **kwargs)

    monkeypatch.setattr(Connection, "fetchval", fetch_with_order_date)
    request = {
        "question": "上个月北京的订单量",
        "session_id": uuid4().hex,
        "api_key_hash": "sql-reuse-test",
    }
    graph = build_graph()
    first = await graph.ainvoke(request)
    data_as_of = date(2025, 12, 20)
    same_month = await graph.ainvoke(request)
    data_as_of = date(2026, 1, 2)
    next_month = await graph.ainvoke(request)

    assert [run["sql_origin"] for run in (first, same_month, next_month)] == [
        "controlled_compiler", "controlled_cache", "controlled_compiler",
    ]
    assert same_month["sql"] == first["sql"]
    assert "o.created_at >= '2025-11-01 00:00:00+00:00'" in first["sql"]
    assert "o.created_at < '2025-12-01 00:00:00+00:00'" in first["sql"]
    assert "o.created_at >= '2025-12-01 00:00:00+00:00'" in next_month["sql"]
    assert "o.created_at < '2026-01-01 00:00:00+00:00'" in next_month["sql"]
    assert all(run["sql_result"]["ok"] for run in (first, same_month, next_month))


@needs_pg
def test_sql_execute_attack_blocked_inside_mcp(mcp: McpStdio) -> None:
    r = mcp.call("sql_execute", {"sql": "SELECT gold_sql FROM eval.queries"})
    assert r["ok"] is False
    assert r["error_type"] == "unsafe"
