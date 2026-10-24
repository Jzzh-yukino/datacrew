"""Schema 注册表：Agent 可见的表/列白名单（安全闸闸4/闸5 的数据源）。

加载链（三级缓存，最终审查后与 README 对齐的真实链路）：
    L1 进程内 TTL 缓存（30s）→ L2 Redis（TTL 1h）→ L3 PostgreSQL information_schema
为什么三级：schema 每条问数都要用（闸4/闸5）；L1 消掉 Redis 往返，L2 跨
进程/跨重启共享，L3 是真相源。每一级 miss 才落下一级，Redis 故障自动降级
到直查库（cache.py 的降级路径），缓存是优化不是依赖。
为什么从 DB 内省而不是手写配置：手写配置会随 DDL 漂移失效，内省永远与真实 schema 一致。
"""
from __future__ import annotations

import re
import time

from app.core.logging import get_logger
from app.infra.cache import cache_get, cache_set, get_redis
from app.infra.db import admin_pool

log = get_logger(__name__)

CACHE_KEY = "schema:allowed_tables"
CACHE_TTL_S = 3600
L1_TTL_S = 30  # 进程内缓存短 TTL：要能及时看到 DDL 变更，又要挡掉单进程内的重复读

# L1：进程内 (数据, 过期时间戳)。多 worker 各自一份，TTL 短，牺牲几秒的
# 新鲜度换每条问数一次 Redis 往返——评测实测 schema 加载是 curator 的固定开销。
_l1_allowed: tuple[dict[str, set[str]], float] | None = None
_l1_metrics: tuple[list[dict], float] | None = None


def _l1_fresh(bucket: list) -> bool:
    """注意判的是 bucket[0] 不是 bucket：调用方传 [全局变量]，未命中时是
    [None]——早期版只判 bool(bucket)，[None] 也"非空"，None[1] 直接
    TypeError 把 schema_curator 节点炸掉（全量测试踩出）。"""
    return bool(bucket) and bucket[0] is not None and bucket[0][1] > time.monotonic()


def _l1_invalidate() -> None:
    """进程内缓存失效（invalidate_cache 时调用，本 worker 立即生效）。"""
    global _l1_allowed, _l1_metrics
    _l1_allowed = None
    _l1_metrics = None


async def load_allowed_tables(use_cache: bool = True) -> dict[str, set[str]]:
    """返回 {表名: {列名集合}}，如 {"users": {"id", "name", ...}, ...}。"""
    global _l1_allowed
    if use_cache and _l1_fresh([_l1_allowed]):
        return {t: set(cols) for t, cols in _l1_allowed[0].items()}
    if use_cache:
        cached = await cache_get(CACHE_KEY)
        if cached:
            out = {t: set(cols) for t, cols in cached.items()}
            _l1_allowed = (out, time.monotonic() + L1_TTL_S)
            return {t: set(cols) for t, cols in out.items()}

    async with admin_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'biz'
            ORDER BY table_name, ordinal_position
            """
        )
    tables: dict[str, set[str]] = {}
    for r in rows:
        tables.setdefault(r["table_name"], set()).add(r["column_name"])

    if use_cache and tables:
        await cache_set(CACHE_KEY, {t: sorted(cols) for t, cols in tables.items()}, CACHE_TTL_S)
        _l1_allowed = (tables, time.monotonic() + L1_TTL_S)
    log.info("schema.loaded", extra={"context": {"tables": sorted(tables)}})
    return tables


async def invalidate_cache() -> None:
    """DDL 变更后调用（灌数据脚本/迁移流程结尾）。本进程 L1 立即失效，
    Redis 两级 key 也删（其他 worker 的 L1 最多 30s 后自然过期）。"""
    _l1_invalidate()
    await get_redis().delete(CACHE_KEY, METRICS_CACHE_KEY)


METRICS_CACHE_KEY = "schema:metric_definitions"


async def load_metric_definitions(use_cache: bool = True) -> list[dict]:
    """指标口径注册表（Agent 的"业务知识"，SchemaCurator 的检索对象）。

    D2 会灌入 embedding 做向量检索；当前先返回全量（量小，<100 条）。
    带 Redis 缓存（多级缓存第二级）：口径表每条问数都会全量注入 prompt，
    但内容几乎不变——与 schema 注册表同理，缓存 miss 才查库。
    重灌 mock 数据/改口径后 invalidate_cache() 会连它一起清。
    """
    global _l1_metrics
    if use_cache and _l1_fresh([_l1_metrics]):
        return list(_l1_metrics[0])
    if use_cache:
        cached = await cache_get(METRICS_CACHE_KEY)
        if cached:
            _l1_metrics = (list(cached), time.monotonic() + L1_TTL_S)
            return cached
    async with admin_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT metric_name, definition, sql_hint, usage_hint
            FROM biz.metric_definitions
            ORDER BY metric_name
            """
        )
    metrics = [dict(r) for r in rows]
    if use_cache and metrics:
        await cache_set(METRICS_CACHE_KEY, metrics, CACHE_TTL_S)
        _l1_metrics = (metrics, time.monotonic() + L1_TTL_S)
    return metrics


# ---- 规则优先澄清（DC-4）----------------------------------------------------
#
# 为什么规则优先：ambiguity 基线 10%。模型对"销售额"这类词时问时不问，行为不
# 稳定；而"同一个业务术语对应多个已注册口径"是**确定性事实**——命中即追问，
# 不花 LLM 调用。LLM 歧义判定降为补充（规则没命中才看模型输出）。
#
# 选项来自口径注册表（metric_definitions.metric_name）；注册表只登记了一种算法
# 的术语（客单价/取消率）在选项里显式标注"注册口径"与另一种常见算法，让用户
# 做选择而不是由模型猜。新增术语/口径时同步补这张表——
# tests/test_clarification_rules.py 用评测集锁住"该问的 8 问、不该问的不问"。
# 通用业务名词后缀（形态词，不是题目术语）：规则由注册表数据推导——
# 同一术语在注册表里对应 >=2 个不同 SQL 口径，就判为歧义。
# 不再手写"用户数/客单价/..."八条规则；新增口径只需进注册表。
_AMBIGUITY_TERMS: tuple[str, ...] = (
    "销售额", "订单金额", "用户数", "转化率", "客单价", "取消率", "退款率",
    "复购率", "订单量", "销量", "件单价", "占比", "频次", "人数", "金额",
)


def derive_clarification_rules(metrics: list[dict]) -> list[dict]:
    """从 metric_definitions 推导歧义规则（纯函数，可单测）。"""
    rules: list[dict] = []
    for term in _AMBIGUITY_TERMS:
        matched = [
            m for m in metrics
            if (term in m.get("metric_name", "")
                # 英文缩写指标（GMV）允许用定义命中：GMV 的中文说法就是销售额
                or (m.get("metric_name", "").isascii()
                    and term in m.get("definition", "")))
            # 占比/排名类是派生指标，不是同一业务词的另一口径
            and not str(m.get("usage_hint", "")).startswith(("占比类", "排名类"))
        ]
        # 同一术语至少两种不同 SQL 口径才算歧义；如果注册表里就有
        # 与术语同名的规范口径（如“订单量”），说明它是单一语义，不问
        if any(m.get("metric_name") == term for m in matched):
            continue
        hints = {m.get("sql_hint") or m.get("definition") for m in matched}
        if len(hints) < 2:
            continue
        qualifiers: set[str] = {"整体"}
        for m in matched:
            name = m.get("metric_name", "")
            if term in name:
                qualifier = name.replace(term, "").strip()
                if qualifier:
                    qualifiers.add(qualifier)
            elif name:
                qualifiers.add(name)
        options = tuple(m["metric_name"] for m in matched)
        rules.append({
            "term": term,
            "options": options,
            "question": (
                f"您说的“{term}”按哪个已注册口径统计？可选："
                + " / ".join(options)
            ),
            "qualifier_keywords": tuple(sorted(qualifiers)),
        })
    # 长词优先：避免“金额”截胡“销售额/订单金额”
    rules.sort(key=lambda r: -len(r["term"]))
    return rules


# 兼容旧导入：不再有手写规则，规则从 schema_context.metrics 实时推导
CLARIFICATION_RULES: tuple[dict, ...] = ()

_SCALAR_MARKERS = ("是多少", "有多少", "怎么样", "多少", "如何", "多大")
_GROUPING_RE = re.compile(r"(?:各|按|每|分\s*组|分渠道|分城市|分品类|分组)")


def match_clarification_rule(
    question: str, schema_context: dict
) -> dict | None:
    """规则优先澄清：命中返回口径规则，否则 None（纯函数，可单测）。

    规则不再手写：`derive_clarification_rules` 从 schema_context.metrics
    （metric_definitions）实时推导——同一术语有 >=2 个不同 SQL 口径即歧义。
    问题已含候选口径限定词（实付/GMV/日活…）时不问，避免过度追问。
    """
    q = (question or "").strip()
    if not q:
        return None
    metrics = schema_context.get("metrics") or []
    # 分组/排名/占比类问法已给出维度或派生口径，规则不追问（旧版
    # grouped_is_qualified 的通用化）；歧义交给 LLM 补充判定。
    if _GROUPING_RE.search(q) or re.search(
        r"(?:占比|比例|排名|最大|最小|前\s*\d+|Top|分布|趋势|贡献"
        r"|今天|昨天|最近|本月|上月|上个月|当天|周末|工作日"
        r"|双11|双 11|618|国庆|季度|\d+\s*月)",
        q, re.I,
    ):
        return None
    for rule in derive_clarification_rules(metrics):
        if rule["term"] not in q:
            continue
        if any(k in q for k in rule.get("qualifier_keywords", ())):
            continue
        return rule
    return None
