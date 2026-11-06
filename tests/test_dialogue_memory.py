"""短期对话记忆：纯函数 + Redis/PG 集成。"""
from __future__ import annotations

import pytest

from app.application import ask_service
from app.core.config import get_settings
from app.infra.cache import get_redis
from app.infra.dialogue_memory import (
    api_key_fingerprint,
    augment_question,
    build_memory_context,
    dialogue_lock,
    ensure_dialogue_memory_schema,
    extract_candidates,
    extract_filters,
    extract_memory_from_turn,
    flush_dialogue_memory,
    forget_dialogue_memory,
    load_dialogue_memory,
    memory_key,
    sanitize_text,
    save_dialogue_memory,
)

SESSION = "dialogue-test"
KEY_HASH = api_key_fingerprint("test-key")


@pytest.fixture()
def memory_on():
    settings = get_settings()
    old = settings.dialogue_memory_enabled
    settings.dialogue_memory_enabled = True
    try:
        yield
    finally:
        settings.dialogue_memory_enabled = old


def _memory() -> dict:
    return {
        "version": 1,
        "last_user_text": "北京的销售额",
        "focus": {
            "filters": [{"table": "users", "column": "city", "value": "北京"}]
        },
        "last_result": {
            "columns": ["channel", "sales"],
            "candidates": {"channel": ["App", "H5"]},
            "selected": None,
        },
    }


def test_sanitize_text_redacts_pii():
    text = "张三 13800138000 110101199001011234 a@b.com"
    clean = sanitize_text(text)
    assert "13800138000" not in clean
    assert "110101199001011234" not in clean
    assert "a@b.com" not in clean
    assert "<PHONE>" in clean
    assert "<ID>" in clean
    assert "<EMAIL>" in clean


def test_extract_filters_skips_pii_and_caps():
    ctx = {
        "matched_values": {
            "北京": [["users", "city"]],
            "张三": [["users", "name"]],
            "App": [["orders", "channel"]],
        }
    }
    filters = extract_filters(ctx)
    assert {"table": "users", "column": "city", "value": "北京"} in filters
    assert all(f["column"] != "name" for f in filters)


def test_extract_candidates_keeps_dimensions_not_pii():
    result = {
        "columns": ["channel", "name", "pay_amount"],
        "rows": [["App", "张三", 1], ["H5", "李四", 2]],
    }
    candidates = extract_candidates(result)
    assert candidates == {"channel": ["App", "H5"]}


def test_extract_memory_from_turn_does_not_store_rows():
    state = {
        "status": "done",
        "schema_context": {"matched_values": {"北京": [["users", "city"]]}},
        "sql_result": {
            "columns": ["channel", "sales"],
            "rows": [["App", 50000], ["H5", 30000]],
        },
    }
    memory = extract_memory_from_turn("北京的销售额", state)
    assert memory["focus"]["filters"][0]["value"] == "北京"
    assert memory["last_result"]["candidates"] == {"channel": ["App", "H5"]}
    assert "rows" not in memory["last_result"]
    assert memory["last_user_text"] == "北京的销售额"


def test_augment_question_unique_channel_resolves():
    q = augment_question("这个渠道的利润", {
        **_memory(),
        "last_result": {"candidates": {"channel": ["App"]}},
    })
    assert "渠道「App」" in q
    assert "users.city=北京" in q


def test_augment_question_multiple_channels_does_not_guess():
    q = augment_question("这个渠道的利润", _memory())
    assert "渠道「" not in q
    assert "请确认具体渠道" in q


def test_augment_question_no_antecedent_asks():
    q = augment_question("这个渠道的利润", {
        "last_user_text": "北京的销售额",
        "focus": {"filters": []},
        "last_result": {"candidates": {}},
    })
    assert "没有唯一候选" in q


def test_build_context_only_for_reference():
    assert build_memory_context(_memory(), "北京的销售额") is None
    assert build_memory_context(_memory(), "这个渠道的利润") is not None


@pytest.mark.usefixtures("db_pools")
async def test_redis_pg_roundtrip_and_pg_fallback(memory_on):
    await ensure_dialogue_memory_schema()
    await forget_dialogue_memory(SESSION, KEY_HASH)
    memory = _memory()
    await save_dialogue_memory(SESSION, KEY_HASH, memory)
    await flush_dialogue_memory()
    try:
        loaded = await load_dialogue_memory(SESSION, KEY_HASH)
        assert loaded and loaded["last_user_text"] == "北京的销售额"

        await get_redis().delete(memory_key(SESSION, KEY_HASH))
        fallback = await load_dialogue_memory(SESSION, KEY_HASH)
        assert fallback and fallback["version"] == 1
    finally:
        await forget_dialogue_memory(SESSION, KEY_HASH)


@pytest.mark.usefixtures("db_pools")
async def test_ask_injects_unique_channel_and_city(memory_on, monkeypatch):
    """ask_service 真的把记忆拼进图输入，而不是只存不用。"""
    await ensure_dialogue_memory_schema()
    await forget_dialogue_memory(SESSION, KEY_HASH)
    await save_dialogue_memory(SESSION, KEY_HASH, {
        **_memory(),
        "last_result": {"candidates": {"channel": ["App"]}},
    })

    class _FakeGraph:
        def __init__(self):
            self.seen: dict = {}

        async def aupdate_state(self, config, values):
            return None

        async def astream(self, value, config, stream_mode):
            self.seen = value
            yield {"schema_curator": {"trace": [{"node": "schema_curator", "latency_ms": 1}]}}

        async def aget_state(self, config):
            class _State:
                tasks = []
                values = {
                    "question": self.seen.get("question", ""),
                    "status": "done",
                    "sql_result": {"columns": ["channel"], "rows": [["App"]]},
                }

            return _State()

    fake = _FakeGraph()

    async def _fake_get_graph():
        return fake

    monkeypatch.setattr(ask_service, "_get_graph", _fake_get_graph)
    try:
        events = [ev async for ev in ask_service.ask("这个渠道的利润", SESSION, KEY_HASH)]
        assert fake.seen["question"].startswith("渠道「App」的利润")
        assert "users.city=北京" in fake.seen["question"]
        assert fake.seen["dialogue_context"]["last_result"]["candidates"]["channel"] == ["App"]
        assert any(ev.get("event") == "result" for ev in events)
    finally:
        await forget_dialogue_memory(SESSION, KEY_HASH)


@pytest.mark.usefixtures("db_pools")
async def test_dialogue_lock_serializes_same_session(memory_on):
    async with dialogue_lock(SESSION, KEY_HASH) as first:
        assert first is True
        async with dialogue_lock(SESSION, KEY_HASH) as second:
            assert second is False
    async with dialogue_lock(SESSION, KEY_HASH) as third:
        assert third is True


async def test_memory_disabled_is_noop():
    settings = get_settings()
    old = settings.dialogue_memory_enabled
    settings.dialogue_memory_enabled = False
    try:
        await save_dialogue_memory(SESSION, KEY_HASH, _memory())
        assert await load_dialogue_memory(SESSION, KEY_HASH) is None
        async with dialogue_lock(SESSION, KEY_HASH) as acquired:
            assert acquired is True
    finally:
        settings.dialogue_memory_enabled = old
