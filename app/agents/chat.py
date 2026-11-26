"""Chat 函数抽象：Agent 节点只依赖这个接口，不关心背后是真实 LLM 还是 Mock。

切换方式：.env 里 LLM_MODE=mock|real（默认 mock，配好 key 后改 real）

为什么绕这一层（面试可讲）：
1. 测试不需要外部服务（见 llm_mock.py）
2. 换 provider / 换模型只改适配层，节点代码零改动
3. 未来加语义缓存、加 Langfuse trace 都只在这一层做，节点无感
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, Protocol

from app.agents.llm_mock import mock_chat
from app.core.config import get_settings
from app.core.logging import get_logger

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletionToolParam

    from app.infra.llm import LLMResponse

log = get_logger(__name__)

_FENCE = chr(96) * 3  # 三个反引号：LLM 常把 JSON 包在代码块里
MAX_SCHEMA_RETRIEVAL_ROUNDS = 2
SCHEMA_RETRIEVAL_TOOLS = (
    "search_metrics", "get_metric_specs", "describe_schema", "resolve_dimension_values",
)


class ToolResult(NamedTuple):
    name: str
    arguments: dict[str, Any]
    content: str


class ChatFn(Protocol):
    async def __call__(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
        tier: Literal["main", "small"] = "main",
        with_schema_retrieval: bool = False,
        tool_results: list[ToolResult] | None = None,
    ) -> str: ...


def _parse_json(text: str) -> dict[str, Any]:
    """宽容 JSON 解析：LLM 有时会在 JSON 外面包代码块。"""
    text = text.strip()
    if text.startswith(_FENCE):
        text = text.split("\n", 1)[-1].rsplit(_FENCE, 1)[0]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 兜底：截取第一个 { 到最后一个 }
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


async def _recorded_completion(
    messages: list[dict[str, Any]],
    *,
    json_mode: bool,
    tier: Literal["main", "small"],
    tools: list[ChatCompletionToolParam] | None = None,
    tool_choice: Literal["none"] | None = None,
) -> LLMResponse:
    from app.infra.llm import get_llm

    resp = await get_llm().chat(
        messages, json_mode=json_mode, tier=tier, tools=tools, tool_choice=tool_choice
    )
    # 观测旁路：记 Langfuse trace（未配置 key 时自动降级，不影响主链路）
    from app.infra.observability import record_generation

    record_generation(
        model=resp.model,
        messages=messages,
        content=resp.content,
        prompt_tokens=resp.prompt_tokens,
        completion_tokens=resp.completion_tokens,
        latency_ms=resp.latency_ms,
        cost_yuan=resp.cost_yuan,
        tier=tier,
    )
    return resp


async def _real_chat(
    messages: list[dict[str, str]],
    *,
    json_mode: bool = False,
    tier: Literal["main", "small"] = "main",
    with_schema_retrieval: bool = False,
    tool_results: list[ToolResult] | None = None,
) -> str:
    if not with_schema_retrieval:
        return (await _recorded_completion(messages, json_mode=json_mode, tier=tier)).content

    from app.tools.mcp_server import mcp

    tools: list[ChatCompletionToolParam] = [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.inputSchema,
            },
        }
        for tool in await mcp.list_tools()
        if tool.name in SCHEMA_RETRIEVAL_TOOLS
    ]
    history = messages
    for _ in range(MAX_SCHEMA_RETRIEVAL_ROUNDS):
        response = await _recorded_completion(history, json_mode=json_mode, tier=tier, tools=tools)
        if not response.message.tool_calls:
            return response.content

        history = [*history, response.message.model_dump(exclude_none=True)]
        for call in response.message.tool_calls:
            if call.type != "function" or call.function.name not in SCHEMA_RETRIEVAL_TOOLS:
                raise ValueError("SchemaCurator 只能调用已开放的检索工具")
            name = call.function.name
            arguments = json.loads(call.function.arguments)
            content = (await mcp.call_tool(name, arguments))[0].text
            if tool_results is not None:
                tool_results.append(ToolResult(name, arguments, content))
            history.append({"role": "tool", "tool_call_id": call.id, "content": content})

    response = await _recorded_completion(
        history, json_mode=json_mode, tier=tier, tools=tools, tool_choice="none"
    )
    return response.content


async def _mock_chat_async(
    messages: list[dict[str, str]],
    *,
    json_mode: bool = False,
    tier: Literal["main", "small"] = "main",
    with_schema_retrieval: bool = False,
    tool_results: list[ToolResult] | None = None,
) -> str:
    # tier 对 mock 无意义（本地规则不花钱），签名保持一致让节点代码两种模式零差异
    return mock_chat(messages, json_mode=json_mode, tier=tier)


def get_chat_fn() -> ChatFn:
    """按配置返回 chat 实现。mock 模式让全链路测试不依赖 API Key。"""
    mode = get_settings().llm_mode.lower()
    if mode == "real":
        return _real_chat
    return _mock_chat_async


async def chat_json(
    messages: list[dict[str, str]],
    *,
    tier: Literal["main", "small"] = "main",
    with_schema_retrieval: bool = False,
    tool_results: list[ToolResult] | None = None,
) -> dict[str, Any]:
    """调用 LLM 并把输出解析成 dict（所有节点统一走这里）。

    tier: "main"=主力模型 / "small"=轻量模型（分级路由，成本优化最直接的一刀）。
    轻任务（歧义检测、意图分类）走 small，重任务（SQL 生成、结论解读）走 main。
    """
    chat_fn = get_chat_fn()
    text = await chat_fn(
        messages,
        json_mode=True,
        tier=tier,
        with_schema_retrieval=with_schema_retrieval,
        tool_results=tool_results,
    )
    return _parse_json(text)
