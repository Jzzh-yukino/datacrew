"""DC-1：评测记录的诊断字段 + 报告/JSONL 的可下钻性。

这些测试直接构造 SSE 事件流（不连数据库、不调 LLM），锁死三件事：
1. 失败记录必然带 generated_sql（含自愈前每次尝试），不再只有"结果集不一致"；
2. 闸门结论/终态/自愈次数字段齐全，报告失败明细确实有 SQL 列且截断 200 字；
3. JSONL 逐题明细完整可回读（SQL 不截断）。
"""
from __future__ import annotations

import json

from eval.result_hash import hash_rows
from eval.runner import (
    SQL_PREVIEW_CHARS,
    _report,
    build_record,
    classify_heal_error,
    write_jsonl,
)


def _q(category: str = "simple_agg", rows: list | None = None) -> dict:
    return {
        "id": 7,
        "question": "各渠道的支付成功率",
        "category": category,
        "result_hash": hash_rows(rows or []),
    }


def _sql_node(sql: str, retry: int = 0) -> dict:
    return {
        "event": "node_done",
        "node": "sql_generator",
        "latency_ms": 12,
        "detail": {"retry": retry},
        "sql": sql,
    }


def _exec_node(**detail) -> dict:
    return {"event": "node_done", "node": "executor", "latency_ms": 5, "detail": detail}


def test_失败记录带每次尝试的_SQL_与闸门归因():
    bad1 = "SELECT channel, COUNT(*) FROM biz.orders GROUP BY 1"
    bad2 = "SELECT channel, sum(pay_amount) FROM biz.orders GROUP BY channel"
    events = [
        _sql_node(bad1, retry=0),
        _exec_node(error="db", error_msg='column "sale_amount" does not exist'),
        _sql_node(bad2, retry=1),
        _exec_node(error="db"),
        {
            "event": "error",
            "message": "查询失败（已重试 2 次）：column not found",
            "status": "failed",
            "sql": bad2,
            "retry_count": 2,
            "last_error": "column not found",
        },
    ]
    rec = build_record(_q(), 1, events, latency_ms=1234)

    assert rec["passed"] is False
    assert rec["generated_sql"] == bad2, "终态 SQL 必须是最后一次生成的"
    assert rec["sql_attempts"] == [bad1, bad2], "自愈前的 SQL 也要留证"
    assert rec["gate"]["outcome"] == "blocked"
    assert rec["gate"]["reason"] == "db"
    assert rec["status"] == "failed"
    assert rec["self_heal_count"] == 2
    assert rec["heal_error_types"] == ["db", "db"]
    assert rec["heal_errors"][0]["category"] == "列不存在"
    assert rec["heal_errors"][1]["category"] == "其他数据库错误"
    assert rec["latency_ms"] == 1234
    assert rec["error"].startswith("查询失败")


def test_自愈后成功的闸门结论是_passed_而不是_blocked():
    """首次执行失败、自愈后成功：闸门结论必须看最后一次执行。

    DC-1 首版把"出现过任何 executor error"当作 blocked，结果自愈成功的题
    在报告里被误标成"被闸拦截"——归因比没有归因更有害。
    """
    rows = [["app", 3]]
    bad = "SELECT wrong_col FROM biz.orders"
    good = "SELECT channel, COUNT(*) FROM biz.orders GROUP BY channel"
    events = [
        _sql_node(bad, retry=0),
        _exec_node(error="db"),
        _sql_node(good, retry=1),
        _exec_node(row_count=1),
        {"event": "result", "status": "done", "summary": "app 3", "sql": good,
         "rows": rows, "retry_count": 1},
    ]
    rec = build_record(_q(rows=rows), 6, events, 20)

    assert rec["passed"] is True
    assert rec["gate"] == {"outcome": "passed", "rows": 1}
    assert rec["self_heal_count"] == 1
    assert rec["heal_error_types"] == ["db"]


def test_成功记录包含结果哈希判定与闸门通过():
    rows = [["app", 0.62], ["web", 0.51]]
    events = [
        _sql_node("SELECT channel, rate FROM biz.orders"),
        _exec_node(row_count=2),
        {
            "event": "result",
            "status": "done",
            "summary": "app 渠道 62%",
            "sql": "SELECT channel, rate FROM biz.orders",
            "rows": rows,
            "retry_count": 0,
        },
    ]
    rec = build_record(_q(rows=rows), 2, events, latency_ms=88)

    assert rec["passed"] is True
    assert rec["reason"] == "结果集一致"
    assert rec["gate"] == {"outcome": "passed", "rows": 2}
    assert rec["status"] == "done"
    assert rec["self_heal_count"] == 0


def test_澄清与审批的终态可区分():
    clarify_events = [
        {"event": "clarification", "type": "clarification",
         "question": "销售额按 GMV 还是实付？", "options": ["GMV", "实付"]},
    ]
    rec = build_record(_q("ambiguous"), 3, clarify_events, 10)
    assert rec["passed"] is True, "追问即通过（ambiguous 判据）"
    assert rec["status"] == "awaiting_clarification"
    assert rec["gate"]["outcome"] == "not_reached"

    approval_events = [
        _sql_node("SELECT * FROM biz.orders"),
        {"event": "approval", "type": "approval",
         "reason": "大表查询必须人工审批", "sql": "SELECT * FROM biz.orders"},
    ]
    rec = build_record(_q("should_refuse"), 4, approval_events, 10)
    assert rec["status"] == "awaiting_approval"
    assert rec["gate"]["outcome"] == "approval_required"
    assert rec["gate"]["approval_state"] == "pending"


def test_报告失败明细有_SQL_列且截断_200_字():
    long_sql = "SELECT " + ",".join(f"c{i}" for i in range(100)) + " FROM biz.orders"
    assert len(long_sql) > SQL_PREVIEW_CHARS
    events = [
        _sql_node(long_sql),
        _exec_node(error="unsafe"),
        {"event": "error", "message": "被安全闸拒绝", "status": "failed",
         "sql": long_sql, "retry_count": 0},
    ]
    rec = build_record(_q(), 5, events, 10)
    meta = {
        "run_at": "2026-10-24 23:00", "model": "deepseek-chat", "llm_mode": "mock",
        "prompt_version": "v1.2", "git_commit": "abcdef0", "total": 1,
        "passed": 0, "accuracy": 0.0, "p95_latency_ms": 10, "cost_yuan": 0.0,
    }
    text = _report([rec], meta)

    assert "生成 SQL（截断 200）" in text
    assert "blocked：unsafe" in text
    # 报告单元格里的 SQL 被截断且压成一行
    row = next(line for line in text.splitlines() if line.startswith("| 7 |"))
    assert "…" in row
    assert "c99" not in row


def test_jsonl_逐题明细完整可回读(tmp_path):
    sql = "SELECT 1"
    events = [
        _sql_node(sql),
        _exec_node(row_count=1),
        {"event": "result", "status": "done", "summary": "1", "sql": sql,
         "rows": [[1]], "retry_count": 0},
    ]
    rec = build_record(_q(rows=[[1]]), 1, events, 10)
    meta = {
        "run_at": "2026-10-24 23:00", "model": "deepseek-chat", "llm_mode": "mock",
        "prompt_version": "v1.2", "git_commit": "abcdef0", "total": 1,
        "passed": 1, "accuracy": 1.0, "p95_latency_ms": 10, "cost_yuan": 0.0,
    }
    path = tmp_path / "eval_x.jsonl"
    write_jsonl(path, [rec], meta)

    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["type"] == "meta" and lines[0]["model"] == "deepseek-chat"
    assert lines[1]["type"] == "item"
    assert lines[1]["generated_sql"] == sql
    assert lines[1]["gate"]["outcome"] == "passed"
    assert len(lines) == 2


def test_自愈错误分类覆盖_db_细分():
    assert classify_heal_error("unsafe", "SELECT 不允许") == "闸门拒绝"
    assert classify_heal_error("timeout", "statement timeout") == "超时"
    assert classify_heal_error(
        "db", 'column "sale_amount" does not exist'
    ) == "列不存在"
    assert classify_heal_error(
        "db", 'relation "biz.shipments" does not exist'
    ) == "表不存在"
    assert classify_heal_error("db", 'syntax error at or near "FROM"') == "语法错误"
    assert classify_heal_error(
        "db", "permission denied for table queries"
    ) == "权限拒绝"
    assert classify_heal_error("db", "connection reset") == "其他数据库错误"


def test_报告包含自愈分布表():
    """DC-7：报告要回答"哪类错误在自愈、成功了没有"，而不只是总数。"""
    rows = [["app", 1]]
    events = [
        _sql_node("SELECT wrong FROM biz.orders"),
        _exec_node(error="db", error_msg='column "wrong" does not exist'),
        _sql_node("SELECT channel FROM biz.orders"),
        _exec_node(error="db", error_msg='column "wrong" does not exist'),
        _sql_node("SELECT channel, 1 FROM biz.orders"),
        _exec_node(row_count=1),
        {"event": "result", "status": "done", "summary": "x",
         "sql": "SELECT channel, 1 FROM biz.orders", "rows": rows, "retry_count": 2},
    ]
    rec = build_record(_q(rows=rows), 8, events, 10)
    meta = {
        "run_at": "2026-10-25 00:30", "model": "deepseek-chat", "llm_mode": "real",
        "prompt_version": "v1.3", "git_commit": "abcdef0", "total": 1,
        "passed": 1, "accuracy": 1.0, "p95_latency_ms": 10, "cost_yuan": 0.0,
    }
    text = _report([rec], meta)

    assert "自愈分布（按错误类型）" in text
    assert "| 列不存在 | 1 | 2 | 1 | 100% |" in text
    assert "合计：1/1 题触发自愈，其中 1 题最终通过" in text


async def test_单题超时不会挂死整轮(monkeypatch):
    """DC-9 实际踩坑：真实模式代理把单条 LLM 请求挂住 40+ 分钟，全量卡死。

    per-question timeout 把"一条题"的故障隔离成一条 error 记录，整轮继续。
    """
    import asyncio

    from eval.runner import run_one

    async def hang(*_args, **_kwargs):
        await asyncio.sleep(10)
        yield {"event": "result"}

    monkeypatch.setattr("eval.runner.ask", hang)
    rec = await run_one(_q(), 1, timeout_s=0.05)
    assert rec["status"] == "failed"
    assert "超时" in (rec["error"] or "")
    assert rec["passed"] is False

