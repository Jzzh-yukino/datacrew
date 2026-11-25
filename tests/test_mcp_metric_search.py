from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from openai.resources.chat.completions import AsyncCompletions
from openai.types.chat import ChatCompletion
from redis.asyncio import Redis

from app.agents.chat import chat_json
from app.core.config import get_settings
from app.infra.cache import close_redis
from app.tools.mcp_server import mcp
from app.tools.schema_registry import invalidate_cache


@pytest.fixture
async def metric_catalog(monkeypatch):
    metrics = [
        {"metric_name": "GMV", "definition": "按下单口径计算销售额"},
        {"metric_name": "实付销售额", "definition": "支付成功订单的实付金额总和"},
        {"metric_name": "客单价", "definition": "实付销售额除以支付成功订单数"},
        {
            "metric_name": "渠道销售额占比",
            "definition": "渠道实付销售额除以全渠道合计",
            "usage_hint": "占比类",
        },
    ]
    monkeypatch.setattr(Redis, "get", AsyncMock(return_value=json.dumps(metrics)))
    monkeypatch.setattr(Redis, "delete", AsyncMock(return_value=0))
    await invalidate_cache()
    yield
    await invalidate_cache()
    await close_redis()


@pytest.mark.parametrize("term", ["销售额", "卖了多少钱"])
async def test_search_metrics_returns_both_sales_amount_definitions(
    metric_catalog, term: str
) -> None:
    content = await mcp.call_tool("search_metrics", {"term": term})
    result = json.loads(content[0].text)
    assert {metric["metric_name"] for metric in result["metrics"]} == {"GMV", "实付销售额"}


@pytest.mark.parametrize(
    ("terms", "expected_metrics"),
    [
        (("销售额",), {"GMV": "按下单口径计算销售额", "实付销售额": "支付成功订单的实付金额总和"}),
        (("销售额", "实付销售额"), {"实付销售额": "支付成功订单的实付金额总和"}),
    ],
    ids=["initial-lookup", "follow-up-lookup"],
)
async def test_model_receives_metric_definitions_from_mcp(
    metric_catalog, monkeypatch, terms: tuple[str, ...], expected_metrics: dict[str, str]
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_mode", "real")
    monkeypatch.setattr(settings, "langfuse_public_key", "")
    tool_calls = [
        {
            "id": f"metric_lookup_{index}",
            "type": "function",
            "function": {"name": "search_metrics", "arguments": json.dumps({"term": term})},
        }
        for index, term in enumerate(terms)
    ]

    async def create_completion(_client, *, messages, model, tools=(), tool_choice=None, **_kwargs):
        results = [message for message in messages if message["role"] == "tool"]
        if results:
            last_call = tool_calls[len(results) - 1]
            assert results[-1]["tool_call_id"] == last_call["id"]
            assert messages[-2]["tool_calls"] == [last_call]

        if len(results) < len(tool_calls):
            assert tool_choice != "none", "模型还需要补查口径"
            assert {tool["function"]["name"] for tool in tools} == {
                "search_metrics", "describe_schema", "resolve_dimension_values"
            }
            message = {"role": "assistant", "tool_calls": [tool_calls[len(results)]]}
            finish_reason = "tool_calls"
        else:
            if len(terms) == 2:
                assert tool_choice == "none"
            message = {"role": "assistant", "content": results[-1]["content"]}
            finish_reason = "stop"
        return ChatCompletion.model_validate({
            "id": "test-completion",
            "model": model,
            "created": 0,
            "object": "chat.completion",
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        })

    monkeypatch.setattr(AsyncCompletions, "create", create_completion)
    result = await chat_json(
        [{"role": "user", "content": "请用 JSON 给出销售额的候选口径"}],
        with_schema_retrieval=True,
    )
    assert {metric["metric_name"]: metric["definition"] for metric in result["metrics"]} == (
        expected_metrics
    )
