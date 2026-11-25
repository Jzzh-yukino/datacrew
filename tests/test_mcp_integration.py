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
from pathlib import Path
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from openai.resources.chat.completions import AsyncCompletions
from openai.types.chat import ChatCompletion

from app.agents.chat import chat_json
from app.agents.supervisor import build_graph
from app.core.config import get_settings

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
            "北京各渠道的订单量",
            "SELECT o.channel, COUNT(*) FROM biz.orders AS o JOIN biz.users AS u "
            "ON u.id = o.user_id WHERE u.city = '北京' GROUP BY o.channel",
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
    ],
    ids=["free-sql-evidence", "controlled-order-count", "controlled-month-order-count"],
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


@needs_pg
@pytest.mark.usefixtures("db_pools")
async def test_repeated_city_order_count_reuses_sql_and_runs_executor(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "llm_mode", "mock")
    request = {
        "question": "北京的订单量",
        "session_id": uuid4().hex,
        "api_key_hash": "sql-reuse-test",
    }
    graph = build_graph()
    first = await graph.ainvoke(request)
    second = await graph.ainvoke(request)

    origins = [
        next(
            step["detail"]["sql_origin"] for step in run["trace"] if step["node"] == "sql_generator"
        )
        for run in (first, second)
    ]
    assert origins == ["controlled_compiler", "controlled_cache"]
    assert second["sql_result"]["ok"]


@needs_pg
def test_sql_execute_attack_blocked_inside_mcp(mcp: McpStdio) -> None:
    r = mcp.call("sql_execute", {"sql": "SELECT gold_sql FROM eval.queries"})
    assert r["ok"] is False
    assert r["error_type"] == "unsafe"
