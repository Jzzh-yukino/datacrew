"""DC-6 时间口径不变量：数据范围真实可查 + SQL 生成提示词必须带公式。

为什么锁提示词：time_range 的修复不是改 SQL，而是把「双11取哪一年、最近 N 天
边界、上个月按谁算」这些组织约定写进提示词。提示词一旦被回退/精简，本测试
必须立刻红——否则 93% 的通过率会在下一次改动里静默退化。
"""
from __future__ import annotations

import pytest

from app.agents import nodes
from app.agents.nodes import _data_bounds, sql_generator_node

pytestmark = pytest.mark.usefixtures("db_pools")


async def test_data_bounds_returns_real_range() -> None:
    lo, hi = await _data_bounds()
    assert len(lo) == 10 and len(hi) == 10, (lo, hi)  # YYYY-MM-DD
    assert lo < hi


async def test_sql_prompt_contains_time_rules(monkeypatch) -> None:
    captured: dict[str, str] = {}

    async def fake_chat_json(messages, **_kwargs):
        captured["system"] = messages[0]["content"]
        return {"sql": "SELECT COUNT(*) FROM biz.orders", "reasoning": "x"}

    monkeypatch.setattr(nodes, "chat_json", fake_chat_json)
    lo, _hi = await _data_bounds()
    out = await sql_generator_node({
        "question": "上个月的订单量",
        "schema_context": {
            "tables": [{"name": "orders", "columns": ["id", "created_at"]}],
            "metrics": [],
        },
        "data_as_of": "2026-09-30",
    })
    prompt = captured["system"]

    # 数据范围必须注入（模型要知道"数据里有哪些年份"才可能取对日历事件年份）
    assert lo in prompt
    assert "2026-09-30" in prompt
    for needle in (
        "时间口径",
        "统计周期一律用 created_at",
        "最近 N 天",
        "上个月",
        "本月至今",
        "双11",
        "大促期间",
        "那一周",
    ):
        assert needle in prompt, f"提示词缺少时间约定：{needle}"
    assert out["sql"] == "SELECT COUNT(*) FROM biz.orders"
