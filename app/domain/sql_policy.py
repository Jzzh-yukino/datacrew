"""SQL 安全策略：函数白/黑名单、大表清单等业务规则（领域层，可单测）。

为什么策略常量放 domain 层：这是"业务决策"（哪些函数对这个问数系统是合法的），
不是技术细节。安全闸 sql_guard.py 只负责"怎么查"，这里决定"查什么算合法"。

设计哲学（最终审查后的修订）：
    黑名单永远追不完。query_to_xml() 能把任意 SQL 当字符串参数传进去执行，
    pg_terminate_backend() 能杀应用自己的连接池——它们都不在任何黑名单上，
    因为写黑名单时根本想不到。所以函数准入改为白名单：只放行这个系统
    确实需要的函数，其余一律拒绝。新函数要用？显式加进来并想清楚理由。
"""
from __future__ import annotations

import re

# 硬黑名单：即使将来误加进白名单也要拦的（纵深防御第二层，明确的拒绝消息）
HARD_BLOCK_FUNCTIONS = {
    # 睡眠/资源攻击
    "pg_sleep", "pg_sleep_for", "pg_sleep_until",
    # 文件系统读取
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "lo_import", "lo_export",
    # 服务端任意连接/执行（query_to_xml 一家：SQL 当字符串参数执行，
    # 一句话绕过表/列白名单——实测能倒出全表 838KB XML）
    "query_to_xml", "query_to_xml_and_xmlschema", "query_to_xmlschema",
    "dblink", "dblink_exec", "postgres_fdw",
    # 连接/会话级破坏（杀应用自己的连接池 = DoS）
    "pg_terminate_backend", "pg_cancel_backend", "pg_terminate_all_backends",
    # 配置改写
    "set_config", "current_setting", "set_config_by_name",
}

# 函数白名单：只放行问数场景确实需要的（聚合/标量/日期/字符串，全部只读无副作用）
ALLOWED_FUNCTIONS = {
    # 聚合（行数有界；数组/字符串收集类刻意不收，单行体积无界）
    "count", "sum", "avg", "min", "max", "stddev", "stddev_pop",
    "stddev_samp", "variance", "var_pop", "var_samp",
    # 分位数聚合：中位数/百分位是只读标量聚合，结果单行（DC-7 评测发现
    # "订单金额的中位数"被拒，错误信息还把 0.5 当成函数名——两个 bug 一起修）
    "percentile_cont", "percentile_disc",
    # 算术/标量
    "round", "floor", "ceil", "ceiling", "abs", "mod", "power", "sqrt",
    "sign", "trunc", "exp", "ln", "log",
    # 条件
    "coalesce", "nullif", "greatest", "least",
    # 日期时间
    "date_trunc", "date_part", "extract", "to_char", "to_date",
    "to_timestamp", "now", "current_date", "current_time",
    "current_timestamp", "localtimestamp", "localtime", "age", "epoch",
    # 字符串
    "lower", "upper", "initcap", "length", "char_length",
    "character_length", "trim", "ltrim", "rtrim", "concat", "concat_ws",
    "substring", "substr", "left", "right", "split_part", "replace",
    "position", "strpos", "lpad", "rpad", "format", "md5",
}

# 大表：无过滤条件的查询必须人工审批（防全表扫描/拖库）
BIG_TABLES = {"orders", "order_items", "traffic_logs"}

# 同义过滤：这些 WHERE 等价于"没有过滤"，骗不过大表审批
TRIVIAL_WHERE_PATTERNS = (
    "true",
    "1 = 1",
    "1=1",
)

MAX_SUBQUERY_DEPTH = 3
DEFAULT_ROW_LIMIT = 1000

# ---- 意图闸门（DC-3）：在 LLM 之前确定性识别"不该执行"的请求 ----------------
#
# 为什么需要：should_refuse 基线 50%。模型面对"帮我把所有订单删掉"的常见行为
# 不是生成 DELETE（会被闸拦），而是**改写成一条无害 SELECT 并作答**——用户拿到
# 一个数字，危险意图被"满足"成了另一件事，安全闸一次都没响。这类请求的正确
# 结局在生成前就定了：拒答，不花 LLM 调用。
#
# 词表是**领域策略数据**（放 domain 层，与函数白名单同理），判据是"祈使/动作
# 形态"而不是关键词命中——「删除率」「修改次数」「更新频率」是正常统计问题，
# 不能被误伤（验收要求）。所以每条写意图正则后面都跟统计后缀的否定前瞻。
_WRITE_VERBS = (
    r"(?:删除|删掉|删了|清空|截断|清除|腾空|导出|修改|更新|改成|改掉|插入|写入|"
    r"建表|授权|drop|delete|update|insert|truncate|alter|grant|create\s+table)"
)
# 动作后面接这些字，说明是统计名词而不是祈使动作（删除率 / 修改次数 / 更新频率）
_STAT_SUFFIX = r"(?!率|次|量|占|统|分|情|趋|明|频|周|间隔|数量)"
# 写意图的对象：表/库/数据/记录/敏感列。只问“更新过地址的用户数”没有这些对象，
# 不能仅凭“请/帮我 + 更新”就拒（RW-4 误拒）。
_WRITE_OBJECT = r"(?:库|数据|schema|手机号|密码|账号|凭据|(?:所有|全部)订单|(?<!列)表)"
WRITE_INTENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "把/将/帮我/请/麻烦/给我 … 动作 + 对象"（含"查订单数然后把用户表删了"）
    re.compile(
        r"(?:把|将|帮我|请|麻烦|给我).{0,20}(?:"
        + _WRITE_VERBS + r".{0,10}" + _WRITE_OBJECT
        + "|" + _WRITE_OBJECT + r".{0,10}" + _WRITE_VERBS + ")"
    ),
    # 祈使句以写动作开头 + 对象："删除用户表""更新订单数据"
    re.compile(r"^[　\s]*" + _WRITE_VERBS + _STAT_SUFFIX + r".{0,10}" + _WRITE_OBJECT),
    # 写动作 + 表/库对象："删除用户表""清空订单表"（排除“列表”）
    re.compile(_WRITE_VERBS + r".{0,6}(?<!列)(?:表|库)"),
    # 改订单状态是写操作（"把未支付订单的状态改成已支付"）
    re.compile(r"订单.{0,8}状态.{0,6}(?:改成|更新为|修改为)"),
)

# 资源滥用：睡眠拖库 / 全部字段无过滤 / 超深嵌套——请求本身就在攻击数据库
RESOURCE_ABUSE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:睡眠|睡一会|睡一下|让数据库忙|忙一会|忙一下|占用|拖慢|拖一下|卡死|压测|压力测试|pg_sleep|死循环)"),
    re.compile(r"(?:全部字段|所有字段|select\s*\*|SELECT\s*\*)"),
    re.compile(r"嵌套.{0,10}(?:十几|十多|[4-9]|\d{2,})"),
)

# 越权探测：表清单 / 系统表 / 评测表。**计划书原方案是保留给七道闸拦**，但真实
# 实测（2 轮）里模型两次都把"数据库里有哪些表"改写成无害业务查询并作答（evaded）
# ——闸根本没机会响。既然正确结局是拒答，就在生成前确定性拒掉；七道闸的拦截
# 能力仍由 55 条 test_sql_guard 单测与 sale_amount 自愈链路覆盖。
PROBE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 判据是 schema，不是某个出题词：非 biz schema 的对象（eval / 系统表）
    re.compile(r"\b(?:eval|information_schema|pg_catalog|pg_tables|pg_class)\b", re.I),
    re.compile(r"(?:系统表|元数据表)"),
    re.compile(r"(?:列出|查看|显示|所有|全部).{0,8}schema", re.I),
    re.compile(r"(?:数据库|库里).{0,8}(?:哪些|什么|所有).{0,4}表"),
    re.compile(r"(?:有哪些|有什么|都有哪些|都有什么).{0,4}表"),
    re.compile(r"表名"),
    re.compile(r"(?:密码|凭据|连接串|connection\s*string)", re.I),
)

INTENT_KIND_WRITE = "write"
INTENT_KIND_RESOURCE = "resource_abuse"
INTENT_KIND_PROBE = "probe"

INTENT_REFUSAL_TEXT = {
    INTENT_KIND_WRITE: (
        "这类请求要求修改数据或数据库结构（删除/清空/更新/插入/建表/授权），"
        "超出了只读问数的范围，无法执行。可以问我业务口径的统计，"
        "例如「上个月各渠道的实付销售额是多少」。"
    ),
    INTENT_KIND_RESOURCE: (
        "这类请求会消耗或攻击数据库资源（睡眠函数/全部字段无过滤/超深嵌套），"
        "超出只读问数的安全边界，无法执行。"
    ),
    INTENT_KIND_PROBE: (
        "这类问题在探测数据库的表清单或元数据（系统表/评测表），属于越权范围，"
        "无法回答。可以问我业务口径的数据，例如「上个月各渠道的实付销售额是多少」。"
    ),
}


def detect_forbidden_intent(question: str) -> str | None:
    """确定性意图判定（纯函数，可单测）：write / resource_abuse / probe / None。"""
    q = (question or "").strip()
    if not q:
        return None
    for pattern in WRITE_INTENT_PATTERNS:
        if pattern.search(q):
            return INTENT_KIND_WRITE
    for pattern in RESOURCE_ABUSE_PATTERNS:
        if pattern.search(q):
            return INTENT_KIND_RESOURCE
    for pattern in PROBE_PATTERNS:
        if pattern.search(q):
            return INTENT_KIND_PROBE
    return None
