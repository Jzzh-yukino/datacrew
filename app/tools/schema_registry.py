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
CLARIFICATION_RULES: tuple[dict, ...] = (
    {
        "term": "用户数",
        "options": ("下单用户数", "购买用户数", "日活用户"),
        "question": "您说的“用户数”是指下单用户、购买用户，还是日活用户？",
        "qualifier_keywords": (
            "下单", "购买", "日活", "活跃", "登录", "浏览", "加购", "新增",
            "老", "复购", "首单", "去重", "只", "仅", "单",
        ),
        "require_scalar": True,
        "grouped_is_qualified": True,
        "dimension_is_qualified": True,
    },
    {
        "term": "转化率",
        "options": ("下单转化率", "下单到支付转化率", "加购到购买转化用户数"),
        "question": "您说的“转化率”是下单转化、下单到支付转化，还是加购到购买转化？",
        "qualifier_keywords": ("下单", "支付", "加购", "购买", "浏览", "到", "→"),
        "require_scalar": False,
    },
    {
        "term": "客单价",
        "options": (
            "客单价（注册口径：实付销售额 / 支付成功订单数）",
            "客单价（不含退款单）",
        ),
        "question": "客单价按注册口径（实付销售额 ÷ 支付成功订单数）还是不含退款单统计？",
        "qualifier_keywords": (
            "整体", "各", "平均", "每", "当天", "双11", "双 11", "月", "年", "近",
        ),
        "require_scalar": True,
        "grouped_is_qualified": True,
        "dimension_is_qualified": True,
    },
    {
        "term": "取消率",
        "options": ("取消率（按订单数：取消订单 / 全部订单）", "取消率（按取消金额占比）"),
        "question": "取消率按订单数还是按金额口径统计？",
        "qualifier_keywords": ("整体", "各", "按", "每", "金额", "订单数"),
        "require_scalar": True,
    },
    {
        "term": "活跃用户",
        "options": ("日活用户（当日有行为）", "月活用户（近 30 天有行为）"),
        "question": "您说的“活跃用户”是日活（当日有行为）还是月活（近 30 天有行为）？",
        "qualifier_keywords": ("日活", "月活", "30 天", "近 30 天"),
        "require_scalar": True,
    },
    {
        "term": "消费金额",
        "options": ("GMV（含取消/退款）", "实付销售额（支付成功）"),
        "question": "“消费金额”按 GMV（含取消/退款）还是实付销售额统计？",
        "qualifier_keywords": ("GMV", "实付"),
        "require_scalar": False,
        "grouped_is_qualified": True,
        "dimension_is_qualified": True,
    },
    {
        "term": "销售情况",
        "options": ("销量（件数）", "GMV（下单金额）", "实付销售额（实付金额）"),
        "question": "“销售情况”看销量（件数）、GMV 还是实付销售额？",
        "qualifier_keywords": ("销量", "GMV", "实付", "销售额"),
        "require_scalar": False,
    },
    {
        "term": "新用户",
        "options": ("新注册用户数（按注册时间）", "首次下单用户数（按首单时间）"),
        "question": "“新用户”指新注册用户，还是首次下单用户？",
        "qualifier_keywords": ("新增", "注册", "首次", "下单用户"),
        "require_scalar": False,
    },
)

_SCALAR_MARKERS = ("是多少", "有多少", "怎么样", "多少", "如何", "多大")
_GROUPING_RE = re.compile(r"(?:各|按|每|分\s*组|分渠道|分城市|分品类|分组)")


def match_clarification_rule(
    question: str, schema_context: dict
) -> dict | None:
    """规则优先澄清：命中返回口径规则，否则 None（纯函数，可单测）。

    四条保护（防"过度追问"事故复发）：
    1. 问题已含候选口径的限定词（实付/GMV/日活…）-> 已限定，不问
    2. 标量型术语要求问题确实在问一个数（是多少/有多少/怎么样）-> 不问列表型
    3. 分组问题（各/按/每）对"用户数/客单价"来说已限定维度 -> 不问
    4. 问题里出现已知维度取值（北京/app…）-> 是带过滤的查询，不问
    """
    q = (question or "").strip()
    if not q:
        return None
    matched_values = set((schema_context.get("matched_values") or {}).keys())
    for rule in CLARIFICATION_RULES:
        if rule["term"] not in q:
            continue
        if any(k in q for k in rule.get("qualifier_keywords", ())):
            continue
        if rule.get("require_scalar") and not any(m in q for m in _SCALAR_MARKERS):
            continue
        if rule.get("grouped_is_qualified") and _GROUPING_RE.search(q):
            continue
        if rule.get("dimension_is_qualified") and any(v and v in q for v in matched_values):
            continue
        return rule
    return None
