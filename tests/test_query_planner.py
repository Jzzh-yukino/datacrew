"""L4 组合查询规划提示：命中问法才注入，简单题不污染。"""
from app.agents.query_planner import build_query_plan_hint


def test_多维分组():
    hint = build_query_plan_hint("各城市各渠道的订单量")
    assert hint and "GROUP BY 维度1, 维度2" in hint


def test_topN热销():
    hint = build_query_plan_hint("母婴亲子品类 Top10 热销商品")
    assert hint and "LIMIT 10" in hint and "products.category" in hint


def test_占比条件聚合():
    hint = build_query_plan_hint("家居家装品类贡献的销售额占比")
    assert hint and "SUM(CASE WHEN" in hint


def test_双条件用户品类():
    hint = build_query_plan_hint("来自 miniapp 的用户买了多少手机数码")
    assert hint and "两个条件必须同时成立" in hint


def test_转化率同口径():
    hint = build_query_plan_hint("各城市浏览到下单的转化率")
    assert hint and "NULLIF" in hint and "traffic_logs" in hint


def test_简单题不注入():
    assert build_query_plan_hint("订单量是多少") is None
    assert build_query_plan_hint("各城市的订单量") is None
    assert build_query_plan_hint("各城市客单价排名") is not None
    assert build_query_plan_hint("最近的订单量") is None
