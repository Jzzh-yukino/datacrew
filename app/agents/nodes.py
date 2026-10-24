"""四个 Agent 节点：SchemaCurator / SQLGenerator / Executor / InsightWriter。

每个节点的职责边界（面试可讲的"单一职责"）：
- SchemaCurator：只负责"该查什么"——检索表与口径，识别歧义。不碰 SQL。
- SQLGenerator：只负责"怎么查"——生成/修正 SQL。不执行。
- Executor：只负责"安全地查"——调 MCP 工具，处理审批与错误分类。
- InsightWriter：只负责"怎么说"——结果翻译成业务结论。

节点间只通过 AgentState 通信，不直接互相调用 —— 每个节点可独立单测。
"""
from __future__ import annotations

import datetime as _dt
import json
import time
from typing import Any

from langgraph.types import interrupt

from app.agents.chat import chat_json, get_chat_fn
from app.agents.prompts import (
    INSIGHT_WRITER_SYSTEM,
    SCHEMA_CURATOR_SYSTEM,
    SQL_GENERATOR_FIX_TEMPLATE,
    SQL_GENERATOR_SYSTEM,
    render,
)
from app.agents.state import AgentState
from app.core.logging import get_logger
from app.domain.sql_policy import INTENT_REFUSAL_TEXT, detect_forbidden_intent
from app.infra.db import admin_pool  # noqa: TID252  数据截止日查询
from app.infra.memory import (
    explicitly_stated,
    load_preferences,
    mark_preference_used,
    remember_preference,
)
from app.tools.chart_gen import generate_chart
from app.tools.schema_search import search_schema
from app.tools.sql_execute import execute_sql

log = get_logger(__name__)

MAX_RETRIES = 3

# ---- 元数据类问题（表结构 / 列数 / 索引）：正确答案要查 information_schema，而它被
# 闸 4（表白名单）禁止——业务表白名单之外一律不放行。所以正确结局是**拒答**，
# 而不是硬答一个数字。真实模型实测事故：问"流量日志表一共有多少列记录"，模型生成
# `COUNT(*) FROM biz.traffic_logs` 并自信地回答"共 20000 条记录"——把"列"当"行"，
# 用户会当真。规则先于 LLM 判定：确定性问题不该花一次 LLM 调用（也省钱）。
#
# 刻意**不含**"有哪些表 / 表名 / 全部字段"：那些是既有 should_refuse（越权）用例，
# 必须继续走七道闸拦截——tests/test_metadata_refusal.py 有专门的不变量测试守住这条边界。
_METADATA_PATTERNS = (
    "多少列", "几列", "有哪些列", "列的列表", "列名",
    "多少字段", "有哪些字段", "字段名",
    "表结构", "建表语句", "ddl", "索引", "主键", "外键",
)

METADATA_REFUSAL = (
    "这类问题问的是数据库元数据（表结构 / 列数 / 索引），不在业务数据的查询范围内，"
    "无法回答。可以问我业务口径的数据，例如「上个月各渠道的实付销售额是多少」。"
)


def is_metadata_question(question: str) -> bool:
    """元数据类问题判定（纯函数，可单测）——命中则直接拒答，不进 SQL 生成。"""
    q = (question or "").lower().replace(" ", "")
    return any(p in q for p in _METADATA_PATTERNS)


# ---- 可回答性闸门（DC-2）----
# 问题：unanswerable 只有 40%——模型会为"商品的颜色/库存/供应商"编出不存在的
# 列或表，报错重试几次后要么失败成"拒答"（碰巧算对），要么被降级成别的答案。
# 正确做法是在**生成之前**判"数据里有没有回答这个问题所需的概念"。
#
# 两级判据：
#   1. 模型判（curator 结构化输出 answerable / missing_concepts）——语义映射
#      中英文列名，模型比规则强；
#   2. 确定性兜底（本模块的词表 + assess_answerability 纯函数）——模型说
#      answerable=true 却同时列出"颜色"这种 schema 里根本没有的概念时，不信自报，
#      直接拒。反过来，模型列的缺失概念其实能映射到已有列（如"渠道"→channel）
#      时也不拒——**两个方向都不能让模型的单方判断说了算**。
#
# 词表只描述"这个 schema 里某个概念可以由哪些列/口径表达"，是领域映射而不是
# 评测集答案：新增表/列时同步补这里（漏补的后果是退回模型判断，不是误拒）。
_SCHEMA_CONCEPT_ALIASES: dict[str, tuple[str, ...]] = {
    "渠道": ("channel", "channel_source"),
    "来源": ("channel", "channel_source", "event_type"),
    "城市": ("city",),
    "地区": ("city",),
    "状态": ("status", "order_status"),
    "品类": ("category",),
    "分类": ("category",),
    "金额": ("amount", "pay_amount", "gmv_amount", "price"),
    "价格": ("price", "list_price"),
    "销售额": ("pay_amount", "gmv_amount"),
    "营收": ("pay_amount", "gmv_amount"),
    "销量": ("quantity",),
    "数量": ("quantity",),
    "件数": ("quantity",),
    "用户": ("user_id", "users"),
    "商品": ("product_id", "products"),
    "订单": ("order_id", "orders"),
    "时间": ("created_at", "pay_time", "ts"),
    "日期": ("created_at", "pay_time", "ts"),
    "支付": ("pay_time", "order_status", "pay_amount"),
    "转化": ("order_status", "pay_time", "event_type"),
    "事件": ("event_type",),
    "名称": ("name",),
    "用户数": ("user_id",),
    "复购": ("order_id", "user_id"),
    "退款": ("order_status",),
    "取消": ("order_status",),
}


def _schema_vocabulary(schema_context: dict[str, Any]) -> set[str]:
    """schema 上下文的词面集合：表名、列名（含拆词）、口径名。"""
    vocab: set[str] = set()
    for table in schema_context.get("tables") or []:
        name = str(table.get("name") or "").lower()
        if name:
            vocab.add(name)
        for col in table.get("columns") or []:
            col = str(col).lower()
            vocab.add(col)
            vocab.update(part for part in col.split("_") if part)
    for metric in schema_context.get("metrics") or []:
        name = str(metric.get("metric_name") or "").lower()
        if name:
            vocab.add(name)
    return vocab


def concept_covered(concept: str, schema_context: dict[str, Any]) -> bool:
    """概念在 schema 上下文里有没有映射（纯函数，可单测）。

    匹配三种情况：概念与词表项互相包含（"销量" ∈ "商品销量TopN"）、概念含
    已知维度词且该词的列确实在上下文里（"支付成功率"含"支付"）、指标名包含概念。
    """
    c = (concept or "").strip().lower()
    if not c:
        return True
    vocab = _schema_vocabulary(schema_context)
    # 过短的表名/列名片段（如 id）做包含匹配噪声太大；2 字符起
    if any(len(tok) >= 2 and (c in tok or tok in c) for tok in vocab):
        return True
    for key, cols in _SCHEMA_CONCEPT_ALIASES.items():
        if key in c and any(col in vocab for col in cols):
            return True
    return False


def assess_answerability(
    analysis: dict[str, Any], schema_context: dict[str, Any]
) -> tuple[bool, list[str], list[str]]:
    """把 curator 输出折叠成拒答决策（纯函数，可单测）。

    Returns:
        (should_refuse, missing_concepts, unresolved_concepts)

    决策表：
        answerable=false                       -> 拒（模型判不可回答）
        answerable=true + 缺失概念全部无映射   -> 拒（模型自相矛盾，不信自报）
        answerable=true + 缺失概念有映射       -> 不拒（防模型误报把可答题杀掉）
    """
    missing: list[str] = []
    for raw in analysis.get("missing_concepts") or []:
        concept = str(raw).strip()
        if concept and concept not in missing:
            missing.append(concept)
    unresolved = [c for c in missing if not concept_covered(c, schema_context)]

    if analysis.get("answerable") is False:
        return True, missing, unresolved
    if missing and len(unresolved) == len(missing):
        return True, missing, unresolved
    return False, missing, unresolved


def _answerability_refusal(missing: list[str]) -> str:
    if missing:
        return "无法回答：数据中没有 " + "、".join(missing) + "。"
    return "无法回答：现有数据表缺少回答该问题所需的字段或口径。"



def _step(node: str, started: float, **detail: Any) -> dict[str, Any]:
    latency_ms = int((time.perf_counter() - started) * 1000)
    return {"node": node, "latency_ms": latency_ms, "detail": detail}


def _fmt_schema_context(schema_context: dict[str, Any]) -> str:
    """把 schema_context 序列化进提示词（LLM 需要的全部事实）。"""
    lines = ["可用表："]
    for t in schema_context.get("tables", []):
        lines.append(f"  biz.{t['name']}({', '.join(t['columns'])})")
    metrics = schema_context.get("metrics", [])
    if metrics:
        lines.append("\n指标口径（必须遵守，【】里是该口径的形态，按形态写 SQL）：")
        for m in metrics:
            lines.append(
                f"  {m['metric_name']}【{m.get('usage_hint') or '整体类'}】: {m['definition']}"
                f"\n    SQL提示: {m['sql_hint']}"
            )
    return "\n".join(lines)


def _today_cn() -> str:
    """当前日期的中文可读形式（如 "2026-10-01 星期四"）——注入 LLM 推算相对时间。"""
    now = _dt.datetime.now()
    return now.strftime("%Y-%m-%d 星期") + "一二三四五六日"[now.weekday()]


async def _data_as_of() -> str:
    """数据截止日 = orders.max(created_at) 的日期。

    真实评测事故："昨天有多少订单"模型按系统今天推算，金标按数据截止日推算，
    两者差 3 天直接判错。BI 系统的正确口径就是按数据截止日解释相对时间，
    系统今天只回答"现在几点"这类问题。查询只读、索引友好。
    """
    try:
        async with admin_pool().acquire() as conn:
            return str(await conn.fetchval("SELECT max(created_at)::date FROM biz.orders"))
    except Exception as e:  # 降级：拿不到用系统今天，不阻塞主链路
        log.warning("data_as_of.fallback", extra={"context": {"reason": str(e)[:100]}})
        return _dt.date.today().isoformat()


# ---------------------------------------------------------------- SchemaCurator
async def schema_curator_node(state: AgentState) -> dict[str, Any]:
    """检索相关表+指标口径；发现口径歧义时 interrupt 向用户追问。"""
    started = time.perf_counter()
    question = state["question"]

    # 0. 写意图 / 资源滥用意图 → 确定性拒答（不调 LLM、不生成 SQL、不碰数据库）。
    #    为什么放在生成前：模型的常见"安全行为"是把 DELETE 改写成 SELECT 再作答，
    #    闸一次都没响，用户却拿到了一个"被满足成另一件事"的答复（DC-3）。
    intent = detect_forbidden_intent(question)
    if intent:
        refusal = INTENT_REFUSAL_TEXT[intent]
        log.info(
            "schema.intent_refused",
            extra={"context": {"question": question[:40], "kind": intent}},
        )
        return {
            "refusal": refusal,
            "schema_context": {"tables": [], "metrics": []},
            "status": "done",
            "insight": {"summary": refusal, "chart_data": []},
            "trace": [_step("schema_curator", started, refused=f"intent:{intent}")],
        }

    # 0.5 元数据类问题 → 直接拒答（不检索、不调 LLM、不生成 SQL）
    if is_metadata_question(question):
        log.info("schema.metadata_refused", extra={"context": {"question": question[:40]}})
        return {
            "refusal": METADATA_REFUSAL,
            "schema_context": {"tables": [], "metrics": []},
            "status": "done",
            "insight": {"summary": METADATA_REFUSAL, "chart_data": []},
            "trace": [_step("schema_curator", started, refused="metadata")],
        }

    # 1. 检索 schema（关键词匹配 + 维度值索引；表数少时全量注入，见 schema_search）
    schema_context = await search_schema(question, top_k=5)

    # 1.5 记忆检索（长期记忆读路径，降级为空 dict）：本会话已确认过的口径偏好。
    # 刻意**不**把偏好拼进提示词——真实模型看到"用户偏好 X"可能直接返回
    # ambiguity=null，那样 clarified_answer 不会被写入，口径反而丢了。
    # 偏好只作用于"要不要打断用户"这一个决策点，其余链路完全不变。
    session_id = state.get("session_id") or ""
    prefs = await load_preferences(session_id)

    # 2. LLM 分析歧义（已有澄清回答时把回答并入问题，消除歧义）
    analysis_question = question
    if state.get("clarified_answer"):
        analysis_question = f"{question}（用户已明确：{state['clarified_answer']}）"
    # 当前日期注入：相对时间（昨天/最近7天）必须可推算，否则真实模型会以
    # "不知道今天几号"为由追问（真实评测 22/25 触发追问的事故之一）
    today = _today_cn()
    as_of = await _data_as_of()
    analysis = await chat_json([
        {"role": "system", "content": (
            SCHEMA_CURATOR_SYSTEM
            + f"\n当前日期：{today}"
            + f"\n数据统计截止日：{as_of}（相对时间一律按它推算，不是系统今天）"
        )},
        {
            "role": "user",
            "content": (
                f"问题：{analysis_question}\n"
                f"{schema_context.get('tables') and _fmt_schema_context(schema_context)}"
            ),
        },
    ], tier="small")  # 歧义检测是轻任务（结构化小输出），走轻量模型——分级路由的真实调用点

    # 3. 可回答性闸门（DC-2）：先于歧义——数据里根本没有的字段，追问口径也答不了。
    #    走与元数据拒答相同的终态（status=done + insight.summary），条件边直达 END，
    #    不进入 sql_generator（否则模型会为不存在的列编 SQL，重试几次后失败）。
    should_refuse, missing, unresolved = assess_answerability(analysis, schema_context)
    if should_refuse:
        refusal = _answerability_refusal(missing)
        log.info(
            "schema.unanswerable_refused",
            extra={"context": {
                "question": question[:40], "missing": missing,
                "unresolved": unresolved, "model_answerable": analysis.get("answerable"),
            }},
        )
        return {
            "refusal": refusal,
            "schema_context": schema_context,
            "data_as_of": as_of,
            "status": "done",
            "insight": {"summary": refusal, "chart_data": []},
            "trace": [_step("schema_curator", started, refused="unanswerable",
                            missing=missing, unresolved=unresolved)],
        }

    # 4. 歧义处理：未澄清过才追问（澄清过的直接放行，防止死循环）
    ambiguity = analysis.get("ambiguity")
    if ambiguity and not state.get("clarified_answer"):
        term = str(ambiguity.get("term") or "").strip()
        options = [str(o) for o in (ambiguity.get("options") or [])]
        remembered = prefs.get(term) if term else None
        # 本轮问题里已经自己说出口径 -> 以当场表述为准，不套历史偏好
        stated = any(explicitly_stated(question, o) for o in options)

        if remembered and not stated:
            # ---- 记忆命中：不再打断用户，直接沿用上次确认过的口径 ----
            await mark_preference_used(session_id, term)
            log.info(
                "clarification.preference_applied",
                extra={"context": {"term": term, "choice": remembered[:40]}},
            )
            return {
                "schema_context": schema_context,
                "ambiguity": None,  # 已由偏好消解，本轮不存在未决歧义
                "clarified_answer": remembered,
                "data_as_of": as_of,
                "status": "running",
                "trace": [_step("schema_curator", started, clarified=True,
                                preference_applied=remembered)],
            }

        answer = interrupt({
            "type": "clarification",
            "question": ambiguity["question"],
            "options": options,
        })
        # ---- 恢复执行：用户回答注入状态，并**沉淀为长期记忆**（记忆写路径）----
        # 抽取不需要 LLM：interrupt payload 已经给了结构化的 term，用户回答就是 choice。
        # 这里同步 await（~1ms 的 upsert）：确定性可测优于 fire-and-forget，
        # 且失败静默降级，最坏结果是下次再问一遍。
        log.info("clarification.answered", extra={"context": {"answer": answer}})
        await remember_preference(session_id, term, str(answer))
        return {
            "schema_context": schema_context,
            "ambiguity": ambiguity,
            "clarified_answer": str(answer),
            "data_as_of": as_of,
            "status": "running",
            "trace": [_step("schema_curator", started, clarified=True)],
        }

    return {
        "schema_context": schema_context,
        "ambiguity": ambiguity if ambiguity else None,
        "data_as_of": as_of,
        "status": "running",
        "trace": [_step("schema_curator", started, tables=len(schema_context.get("tables", [])))],
    }


# ---------------------------------------------------------------- SQLGenerator
async def sql_generator_node(state: AgentState) -> dict[str, Any]:
    """生成 SQL；有 last_error 时进入自愈模式（错误回灌）。"""
    started = time.perf_counter()
    schema_ctx = _fmt_schema_context(state.get("schema_context", {}))

    system_prompt = render(SQL_GENERATOR_SYSTEM, schema_context=schema_ctx)
    as_of = state.get("data_as_of") or ""
    if as_of:
        system_prompt += (
            f"\n数据统计截止日：{as_of}。"
            "昨天/最近N天/上个月等相对时间一律按数据截止日推算，"
            f"（如昨天= {as_of} 前一天）。"
            "问题没提时间范围时，WHERE 禁止出现任何时间条件——默认全量统计。"
        )
    if state.get("last_error"):
        # 自愈模式：把执行错误回灌，让 LLM 修正
        prompt = render(
            SQL_GENERATOR_FIX_TEMPLATE,
            error=state["last_error"],
            sql=state.get("sql", ""),
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]
    else:
        question = state["question"]
        if state.get("clarified_answer"):
            question = f"{question}（用户已明确：{state['clarified_answer']}）"
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"问题：{question}"},
        ]

    try:
        result = await chat_json(messages)
    except json.JSONDecodeError:
        # 真实模型偶发把 JSON 包在自然语言里或输出被截断（评测实测：
        # "各城市的订单量排名" 23s 自愈失败于此）。立即原样重来一次，
        # 追加硬约束——比走面向 SQL 错误的自愈回路更对症。
        result = await chat_json(
            messages + [{"role": "user", "content": "严格只输出一个 JSON 对象，不要任何其他文字"}]
        )
    retry = state.get("retry_count", 0) + 1 if state.get("last_error") else 0
    return {
        "sql": result.get("sql", ""),
        "sql_reasoning": result.get("reasoning", ""),
        "retry_count": retry,
        "last_error": None,  # 清空错误，避免下一轮误入自愈模式
        "trace": [_step("sql_generator", started, retry=retry)],
    }


# ---------------------------------------------------------------- Executor
async def executor_node(state: AgentState) -> dict[str, Any]:
    """调 MCP sql_execute；高危 SQL interrupt 转人工审批。"""
    started = time.perf_counter()
    sql = state.get("sql") or ""
    result = await execute_sql(sql)

    if result.get("error_type") == "approval_required":
        decision = interrupt({
            "type": "approval",
            "reason": result.get("error"),
            "sql": sql,
        })
        # ---- 恢复执行：人工批准后重跑（approved=True）----
        if isinstance(decision, dict) and decision.get("approved"):
            result = await execute_sql(sql, approved=True)
        else:
            return {
                "status": "failed",
                "error": "用户拒绝了该查询",
                "trace": [_step("executor", started, approval="rejected")],
            }

    if not result.get("ok"):
        retry_count = state.get("retry_count", 0)
        if retry_count >= MAX_RETRIES:
            return {
                "status": "failed",
                "error": f"查询失败（已重试 {retry_count} 次）：{result.get('error')}",
                "trace": [_step("executor", started, retries_exhausted=True)],
            }
        # 错误回灌给 SQLGenerator 自愈
        return {
            "last_error": result.get("error", "未知错误"),
            "trace": [_step("executor", started, error=result.get("error_type"))],
        }

    return {
        "sql_result": result,
        "last_error": None,
        "status": "running",
        "trace": [_step("executor", started, row_count=result.get("row_count"))],
    }


# ---------------------------------------------------------------- InsightWriter
# token 预算（README §8 承诺的实现）：单请求最多喂 10 行；单格超 200 字符
# 截断；整段上下文超 4000 字符再截断——超限时 log.warning 告警。
# 防的是"单行 944KB 的 array_agg 把 prompt 撑爆"这类真实事故。
MAX_CELL_CHARS = 200
MAX_CONTEXT_CHARS = 4000


def _fmt_rows_for_llm(columns: list[str] | None, rows: list) -> str:
    """把查询结果格式化成给 LLM 的上下文（带 token 预算截断与告警）。"""
    clipped = [[str(c)[:MAX_CELL_CHARS] for c in row] for row in rows]
    text = f"列: {columns}\n数据（前{len(clipped)}行）: {clipped}"
    if len(text) > MAX_CONTEXT_CHARS:
        text = text[:MAX_CONTEXT_CHARS] + "…[超token预算已截断]"
        log.warning("insight.token_budget_exceeded", extra={"context": {"rows": len(clipped)}})
    elif any(len(str(c)) > MAX_CELL_CHARS for row in rows for c in row):
        log.warning("insight.cell_truncated", extra={"context": {"limit": MAX_CELL_CHARS}})
    return text

async def insight_writer_node(state: AgentState) -> dict[str, Any]:
    """把查询结果写成业务结论。"""
    started = time.perf_counter()
    result = state.get("sql_result") or {}
    rows = result.get("rows", [])[:10]  # 最多喂 10 行，控制 token
    result_ctx = _fmt_rows_for_llm(result.get("columns"), rows)

    # 结论是自由文本，不走 JSON 解析（与 SQLGenerator 的 JSON 输出不同）
    chat_fn = get_chat_fn()
    summary = await chat_fn([
        {"role": "system", "content": render(INSIGHT_WRITER_SYSTEM, result_context=result_ctx)},
        {"role": "user", "content": f"问题：{state['question']}"},
    ])

    # 图表降级策略：有结果就尝试出图；任何一步不满足条件（无数字列/超量/
    # 存储故障）都只跳过图表，绝不让图表失败炸掉整条问数链路——
    # 结论文本的价值高于图表，失败隔离是硬要求。
    chart_url = None
    chart_note = None
    try:
        chart_url, chart_note = await _maybe_chart(result, state["question"])
    except Exception as e:  # 兜底：图表是增强不是依赖
        chart_note = f"chart skipped: {type(e).__name__}"

    insight = {"summary": summary.strip(), "row_count": result.get("row_count")}
    if chart_url:
        insight["chart_url"] = chart_url
    # trace 里的 chart 字段是排障入口：出图了/为何跳过，一眼可见
    chart_detail = "generated" if chart_url else (chart_note or "skipped")
    return {
        "insight": insight,
        "status": "done",
        "trace": [_step("insight_writer", started, chart=chart_detail)],
    }


def _chart_points(result: dict) -> list[list] | None:
    """从查询结果提炼图表数据点：第一个文本列做标签，第一个数值列做值。

    只取前 10 行（图表可读性上限，与 prompt 喂 10 行一致），
    找不到数值列时返回 None（调用方跳过图表）。
    """
    columns = result.get("columns") or []
    rows = result.get("rows") or []
    if len(columns) < 2 or not rows:
        return None
    label_idx, value_idx = 0, None
    for j in range(1, len(columns)):  # 第 0 列固定做标签列
        sample = next((r[j] for r in rows if isinstance(r[j], (int, float))), None)
        if sample is not None:
            value_idx = j
            break
    if value_idx is None:
        return None
    points = []
    for r in rows[:10]:
        v = r[value_idx]
        if isinstance(v, (int, float)):
            points.append([str(r[label_idx]), v])
    return points or None


async def _maybe_chart(result: dict, question: str) -> tuple[str | None, str | None]:
    """有结构化结果就生成柱状图；返回 (url, note)。note 供 trace 排障。"""
    points = _chart_points(result)
    if points is None:
        return None, "skipped: no numeric column"
    chart = await generate_chart("bar", points, title=question[:30])
    if not chart.get("ok"):
        return None, f"skipped: {chart.get('error_type')}"
    return chart["url"], None
