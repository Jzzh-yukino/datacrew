"""短期对话记忆：Redis 主读 + PG 摘要兜底。

定位（与 memory.py 的长期偏好记忆分开）：
- 工作记忆：节点内 prompt，不落盘；
- 短期对话记忆：本模块，存“上一轮问了什么、focus、结果候选”，跨轮；
- 任务状态：LangGraph checkpointer（PG），负责 interrupt/resume；
- 长期记忆：mem.preferences，负责口径偏好。

设计约束：
1. Redis 是增强，不是正确性依赖；读写失败一律 fail-soft；
2. 只存截断文本、结构化槽位、result_id/候选；不存 rows；手机号/身份证/邮箱正则脱敏，
   姓名/地址等自由文本需要 NER，当前未覆盖；
3. 按 api_key 指纹 + session_id 隔离，避免跨租户串话；
4. 同 session 的 turn 由上层加锁；本模块只做数据存取；
5. 不接管 pending interrupt/approval，那些只留在 checkpointer。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger
from app.domain.sql_policy import pii_columns
from app.infra.cache import get_redis
from app.infra.db import admin_pool

log = get_logger(__name__)

MEMORY_SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS mem;
CREATE TABLE IF NOT EXISTS mem.dialogue_summaries (
    api_key_hash TEXT        NOT NULL,
    session_id   TEXT        NOT NULL,
    summary      JSONB       NOT NULL,
    version      INTEGER     NOT NULL DEFAULT 1,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (api_key_hash, session_id)
);
CREATE INDEX IF NOT EXISTS dialogue_summaries_expires_idx
    ON mem.dialogue_summaries (expires_at);
"""

_REFERENCE_RE = re.compile(r"这个|那个|该|它|那|呢|继续|上述|刚才|前面")
_CHANNEL_WORDS = ("渠道", "channel", "来源", "source")
_CANDIDATE_WORDS = (
    "channel", "city", "category", "status", "source", "brand",
    "product", "region", "province", "品类", "城市", "渠道", "状态",
)
_PII_WORDS = ("name", "phone", "mobile", "email", "address", "id_card", "password")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ID_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_SPACE_RE = re.compile(r"\s+")

_pg_tasks: set[asyncio.Task[Any]] = set()


def api_key_fingerprint(api_key: str | None) -> str:
    """API Key 的稳定指纹；绝不把原始 key 写进 Redis/PG。"""
    raw = api_key or "anonymous"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def memory_key(session_id: str, api_key_hash: str) -> str:
    return f"datacrew:dialogue:{api_key_hash}:{session_id}"


def _enabled() -> bool:
    return bool(get_settings().dialogue_memory_enabled)


def _ttl() -> int:
    return max(60, int(get_settings().dialogue_memory_ttl_s))


def sanitize_text(text: str | None, max_chars: int = 200) -> str:
    """脱敏 + 截断；用于 last_user_text / turns，不用于原始审计。"""
    clean = _SPACE_RE.sub(" ", str(text or "")).strip()
    clean = _PHONE_RE.sub("<PHONE>", clean)
    clean = _ID_RE.sub("<ID>", clean)
    clean = _EMAIL_RE.sub("<EMAIL>", clean)
    return clean[:max_chars]


def has_reference(question: str) -> bool:
    return bool(_REFERENCE_RE.search(question or ""))


def _is_pii(table: str, column: str) -> bool:
    return column in pii_columns(table) or column.lower() in _PII_WORDS


def extract_filters(schema_context: dict[str, Any] | None) -> list[dict[str, str]]:
    """把 schema_context.matched_values 转成可继承的过滤条件。"""
    matched = (schema_context or {}).get("matched_values") or {}
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for value, pairs in matched.items():
        text = sanitize_text(str(value), max_chars=64)
        if not text:
            continue
        for pair in pairs or []:
            if len(pair) < 2:
                continue
            table, column = str(pair[0]), str(pair[1])
            if _is_pii(table, column):
                continue
            key = (table, column, text)
            if key in seen:
                continue
            seen.add(key)
            out.append({"table": table, "column": column, "value": text})
    return out[:10]


def extract_candidates(sql_result: dict[str, Any] | None) -> dict[str, list[str]]:
    """只提取低基数维度候选，不存原始 rows。"""
    result = sql_result or {}
    columns = [str(c) for c in (result.get("columns") or [])]
    rows = result.get("rows") or []
    candidates: dict[str, list[str]] = {}
    for idx, column in enumerate(columns):
        low = column.lower()
        if not any(word in low or word in column for word in _CANDIDATE_WORDS):
            continue
        if any(word in low for word in _PII_WORDS):
            continue
        values: list[str] = []
        for row in rows:
            if idx >= len(row):
                continue
            value = sanitize_text(str(row[idx]), max_chars=64)
            if value and value not in values:
                values.append(value)
        if 1 <= len(values) <= 10:
            candidates[column] = values
    return candidates


def extract_memory_from_turn(
    question: str,
    state: dict[str, Any] | None,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """从一轮图终态提取记忆；纯函数，便于单测。"""
    prev = previous or {}
    state = state or {}
    status = str(state.get("status") or "unknown")
    schema_context = state.get("schema_context") or {}
    filters = extract_filters(schema_context)
    if not filters and has_reference(question):
        filters = list((prev.get("focus") or {}).get("filters") or [])

    sql_result = state.get("sql_result") or {}
    last_result = {
        "result_id": uuid.uuid4().hex[:12],
        "columns": [str(c) for c in (sql_result.get("columns") or [])],
        "candidates": extract_candidates(sql_result),
        "selected": None,
    }
    if not last_result["columns"] and prev.get("last_result"):
        last_result = dict(prev["last_result"])

    turns = list(prev.get("turns") or [])
    turns.append({"q": sanitize_text(question), "status": status})
    max_turns = max(1, int(get_settings().dialogue_memory_max_turns))
    turns = turns[-max_turns:]

    return {
        "version": int(prev.get("version") or 0) + 1,
        "updated_at": _now_iso(),
        "last_user_text": sanitize_text(question),
        "focus": {"filters": filters},
        "last_result": last_result,
        "turn_count": int(prev.get("turn_count") or 0) + 1,
        "turns": turns,
    }


def build_memory_context(memory: dict[str, Any] | None, question: str) -> dict[str, Any] | None:
    """给 state 的只读上下文；没有记忆/没有指代时返回 None。"""
    if not memory or not has_reference(question):
        return None
    return {
        "last_user_text": memory.get("last_user_text"),
        "focus": memory.get("focus") or {},
        "last_result": memory.get("last_result") or {},
        "turn_count": memory.get("turn_count", 0),
    }


def augment_question(question: str, memory: dict[str, Any] | None) -> str:
    """只在有指代时把记忆拼进问题；当前轮显式内容仍优先。"""
    if not memory or not has_reference(question):
        return question

    q = question
    focus = memory.get("focus") or {}
    last_result = memory.get("last_result") or {}
    candidates = last_result.get("candidates") or {}
    channels: list[str] = []
    for column, values in candidates.items():
        if any(word in column.lower() or word in column for word in _CHANNEL_WORDS):
            channels.extend(str(v) for v in values if str(v))
    channels = list(dict.fromkeys(channels))

    if "这个渠道" in q or "那个渠道" in q or "该渠道" in q:
        if len(channels) == 1:
            q = q.replace("这个渠道", f"渠道「{channels[0]}」")
            q = q.replace("那个渠道", f"渠道「{channels[0]}」")
            q = q.replace("该渠道", f"渠道「{channels[0]}」")
        elif channels:
            q += f"（渠道候选：{' / '.join(channels[:5])}，请确认具体渠道）"
        else:
            q += "（“这个渠道”在上一轮结果中没有唯一候选，请先澄清）"

    filters = focus.get("filters") or []
    if filters:
        lines = [f"{f.get('table')}.{f.get('column')}={f.get('value')}" for f in filters[:8]]
        q += "\n【对话记忆】已确认条件：" + "；".join(lines)
    if channels:
        q += "；上一轮渠道候选：" + " / ".join(channels[:5])
    q += "。规则：只有唯一匹配才继承；不唯一时先澄清，不要默认取第一名。"
    return q


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


async def ensure_dialogue_memory_schema() -> bool:
    """幂等建表；失败只降级，不阻塞启动。"""
    try:
        async with admin_pool().acquire() as conn:
            await conn.execute(MEMORY_SCHEMA_SQL)
        return True
    except Exception as e:
        log.warning("dialogue.ensure_failed", extra={"context": {"reason": str(e)[:120]}})
        return False


async def _load_pg(session_id: str, api_key_hash: str) -> dict[str, Any] | None:
    try:
        async with admin_pool().acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT summary FROM mem.dialogue_summaries
                WHERE api_key_hash = $1 AND session_id = $2 AND expires_at > now()
                """,
                api_key_hash, session_id,
            )
        if row is None:
            return None
        summary = row["summary"]
        if isinstance(summary, str):
            summary = json.loads(summary)
        return dict(summary)
    except Exception as e:
        log.warning("dialogue.pg_load_degraded", extra={"context": {"reason": str(e)[:120]}})
        return None


async def load_dialogue_memory(
    session_id: str, api_key_hash: str
) -> dict[str, Any] | None:
    """Redis 主读；miss 时回查 PG 并回填。任何失败返回 None。"""
    if not _enabled() or not session_id:
        return None
    key = memory_key(session_id, api_key_hash)
    try:
        raw = await get_redis().get(key)
        if raw:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
    except Exception as e:
        log.warning("dialogue.redis_load_degraded", extra={"context": {"reason": str(e)[:120]}})

    if not get_settings().dialogue_memory_pg_fallback:
        return None
    data = await _load_pg(session_id, api_key_hash)
    if data:
        try:
            await get_redis().set(key, json.dumps(data, ensure_ascii=False), ex=_ttl())
        except Exception:
            pass
    return data


async def _persist_pg(session_id: str, api_key_hash: str, memory: dict[str, Any]) -> None:
    try:
        async with admin_pool().acquire() as conn:
            await conn.execute(
                """
                INSERT INTO mem.dialogue_summaries
                    (api_key_hash, session_id, summary, version, updated_at, expires_at)
                VALUES ($1, $2, $3::jsonb, $4, now(), now() + make_interval(secs => $5))
                ON CONFLICT (api_key_hash, session_id) DO UPDATE
                   SET summary = EXCLUDED.summary,
                       version = EXCLUDED.version,
                       updated_at = now(),
                       expires_at = EXCLUDED.expires_at
                """,
                api_key_hash, session_id,
                json.dumps(memory, ensure_ascii=False), int(memory.get("version") or 1), _ttl(),
            )
    except Exception as e:
        log.warning("dialogue.pg_save_degraded", extra={"context": {"reason": str(e)[:120]}})


async def save_dialogue_memory(
    session_id: str, api_key_hash: str, memory: dict[str, Any]
) -> None:
    """Redis 同步写；PG 摘要异步 best-effort。失败静默。"""
    if not _enabled() or not session_id or not memory:
        return
    key = memory_key(session_id, api_key_hash)
    try:
        await get_redis().set(key, json.dumps(memory, ensure_ascii=False), ex=_ttl())
    except Exception as e:
        log.warning("dialogue.redis_save_degraded", extra={"context": {"reason": str(e)[:120]}})
    if get_settings().dialogue_memory_pg_fallback:
        task = asyncio.create_task(_persist_pg(session_id, api_key_hash, memory))
        _pg_tasks.add(task)
        task.add_done_callback(_pg_tasks.discard)


async def flush_dialogue_memory() -> None:
    """测试/关停时等待 PG 摘要写完。"""
    if _pg_tasks:
        await asyncio.gather(*list(_pg_tasks), return_exceptions=True)


async def forget_dialogue_memory(session_id: str, api_key_hash: str) -> None:
    """测试/清空会话用。"""
    try:
        await get_redis().delete(memory_key(session_id, api_key_hash))
    except Exception:
        pass
    try:
        async with admin_pool().acquire() as conn:
            await conn.execute(
                "DELETE FROM mem.dialogue_summaries WHERE api_key_hash = $1 AND session_id = $2",
                api_key_hash, session_id,
            )
    except Exception:
        pass


_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


@asynccontextmanager
async def dialogue_lock(
    session_id: str, api_key_hash: str
) -> AsyncIterator[bool]:
    """同 session 串行锁；Redis 故障时 fail-open，不阻塞问数。"""
    if not _enabled():
        yield True
        return
    key = f"datacrew:dialogue:lock:{api_key_hash}:{session_id}"
    token = uuid.uuid4().hex
    try:
        ok = await get_redis().set(
            key, token, nx=True, ex=max(30, int(get_settings().dialogue_memory_lock_ttl_s))
        )
    except Exception:
        yield True
        return
    if not ok:
        yield False
        return
    try:
        yield True
    finally:
        try:
            await get_redis().eval(_RELEASE_LUA, 1, key, token)
        except Exception:
            pass
