"""DC-2 可回答性闸门：纯函数判据 + 至少 5 例可回答问题不被误拒。

设计要点（也是面试可讲的判据）：
- 模型判（answerable/missing_concepts）负责语义映射，规则兜底负责"不信自报"：
  answerable=true 但缺失概念在 schema 里全无映射 -> 仍拒；
  模型报缺失但概念其实能映射到已有列/口径 -> 不拒（防误杀可答题）。
"""
from __future__ import annotations

import json

from app.agents.llm_mock import mock_chat
from app.agents.nodes import (
    _schema_vocabulary,
    assess_answerability,
    concept_covered,
)

SCHEMA = {
    "tables": [
        {"name": "orders", "columns": [
            "id", "user_id", "channel", "gmv_amount", "pay_amount",
            "order_status", "created_at", "pay_time",
        ]},
        {"name": "users", "columns": ["id", "name", "city", "channel_source", "created_at"]},
        {"name": "products", "columns": ["id", "name", "category", "status", "list_price"]},
        {"name": "order_items", "columns": ["id", "order_id", "product_id", "quantity", "price"]},
        {"name": "traffic_logs", "columns": ["id", "user_id", "event_type", "ts"]},
    ],
    "metrics": [
        {"metric_name": n}
        for n in (
            "订单量", "实付销售额", "客单价", "退款率", "复购率", "销量",
            "件单价", "加购到购买转化用户数", "下单到支付转化率", "购买用户数",
        )
    ],
}

UNANSWERABLE = [
    "商品的颜色分布是怎样的",
    "用户的年龄分布",
    "用户的手机号列表",
    "各商品的当前库存",
    "昨天的物流单号有多少",
    "商品的评分分布",
    "优惠券的核销情况",
    "客服工单数量",
    "退货原因分布",
    "供应商交货及时率",
]

ANSWERABLE = [
    "各渠道的订单量分别是多少",
    "各城市的订单量排名",
    "各商品品类的销量",
    "昨天有多少订单",
    "各渠道的实付销售额是多少",
    "平均订单金额是多少",
    "各订单状态的订单数",
    "各渠道的平均客单价",
]


def _curator(question: str) -> dict:
    """用 mock 的 curator 输出当"模型返回"，避免测试里手写理想化的假输出。"""
    raw = mock_chat([
        {"role": "system", "content": "你是电商数据分析助手的数据规划专家。"},
        {"role": "user", "content": f"问题：{question}\n可用表：biz.orders(...)"},
    ])
    return json.loads(raw)


def test_十个不可回答问题全部拒答且带缺失概念():
    for question in UNANSWERABLE:
        analysis = _curator(question)
        should, missing, unresolved = assess_answerability(analysis, SCHEMA)
        assert should, f"应拒答却放行：{question} -> {analysis}"
        assert missing, f"拒答必须能说出缺什么：{question}"
        assert unresolved, f"缺失概念必须真的无映射：{question}"


def test_至少五个可回答问题不被误拒():
    # 验收要求 ≥5 例；这里放 8 例，覆盖口径歧义题（销售额）与相对时间题
    checked = 0
    for question in ANSWERABLE:
        should, missing, unresolved = assess_answerability(_curator(question), SCHEMA)
        assert not should, f"可回答问题被误拒：{question} -> {missing}/{unresolved}"
        assert not missing
        checked += 1
    assert checked >= 5


def test_模型自报可答但列出不存在概念_确定性兜底拒答():
    analysis = {"answerable": True, "missing_concepts": ["颜色"], "ambiguity": None}
    should, missing, unresolved = assess_answerability(analysis, SCHEMA)
    assert should is True, "模型说可答不算数：概念在 schema 里无映射就必须拒"
    assert missing == ["颜色"] and unresolved == ["颜色"]


def test_模型报缺失但概念能映射到已有列_不误拒():
    for concept in ("渠道", "城市", "销售额", "支付转化率", "商品", "订单量"):
        analysis = {"answerable": True, "missing_concepts": [concept]}
        should, _, unresolved = assess_answerability(analysis, SCHEMA)
        assert should is False, f"概念 {concept} 能映射却被拒：{unresolved}"


def test_概念覆盖判定的边界():
    assert concept_covered("渠道", SCHEMA)      # 别名 -> channel
    assert concept_covered("订单量", SCHEMA)    # 口径名精确命中
    assert concept_covered("商品", SCHEMA)      # 别名 -> products/product_id
    assert not concept_covered("颜色", SCHEMA)
    assert not concept_covered("物流单号", SCHEMA)
    assert not concept_covered("供应商", SCHEMA)


def test_schema_词表包含表列口径():
    vocab = _schema_vocabulary(SCHEMA)
    assert {"orders", "channel", "pay_amount", "city", "category"} <= vocab
    # 列名拆词：order_status -> order / status，方便中文概念半匹配
    assert "status" in vocab
    assert "订单量" in vocab


def test_拒绝话术包含缺失概念且不空():
    from app.agents.nodes import _answerability_refusal

    msg = _answerability_refusal(["颜色", "库存"])
    assert "无法回答" in msg and "颜色" in msg and "库存" in msg
    assert "字段或口径" in _answerability_refusal([])
