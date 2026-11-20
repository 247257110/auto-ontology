# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Chinese Semantic IR behavior tests."""

from auto_ontology.retrieval.text_to_sql.semantic_ir import build_semantic_ir
from auto_ontology.retrieval.text_to_sql.metric_grounding import (
    build_metric_clarification_question,
    ground_metric_semantics,
)


def test_chinese_ir_preserves_metric_dimension_time_and_ranking() -> None:
    ir = build_semantic_ir(
        "查询去年各地区客户收入最高的前5名",
        question_type="calculation",
        calculation_subtype="ranking",
    )

    assert ir.language == "zh"
    assert ir.metrics == ["收入"]
    assert ir.dimensions == ["地区"]
    assert ir.time_ranges[0].expression == "去年"
    assert ir.time_ranges[0].kind == "calendar"
    assert ir.time_ranges[0].canonical == "previous_calendar_year"
    assert ir.sort_order == "desc"
    assert ir.limit == 5
    assert ir.retrieval_entities()[:2] == ["收入", "地区"]


def test_chinese_ir_distinguishes_rolling_window_and_numeric_filter() -> None:
    ir = build_semantic_ir(
        "统计最近30天订单数大于100的客户",
        question_type="calculation",
        calculation_subtype="counting",
    )

    assert [(item.expression, item.kind) for item in ir.time_ranges] == [
        ("最近30天", "rolling")
    ]
    assert ir.filters[0].field == "订单数"
    assert ir.filters[0].operator == ">"
    assert ir.filters[0].value == "100"
    assert ir.filters[0].value_type == "number"


def test_ir_records_ambiguity_without_inventing_sql_identifiers() -> None:
    ir = build_semantic_ir(
        "比较收入和营收",
        question_type="calculation",
        calculation_subtype="comparison",
    )

    assert ir.metrics == ["收入", "营收"]
    assert ir.ambiguities
    assert all("." not in value for value in ir.retrieval_entities())


def test_metric_semantics_capture_aggregation_unit_and_comparison() -> None:
    ir = build_semantic_ir(
        "统计最近30天每个地区的人均收入同比和转化率",
        question_type="calculation",
        calculation_subtype="comparison",
    )

    specs = {item.name: item for item in ir.metric_specs}
    assert specs["收入"].aggregation == "avg"
    assert specs["收入"].unit == "per_person"
    assert specs["收入"].comparison == "yoy"
    assert specs["转化率"].aggregation == "ratio"
    assert specs["转化率"].unit == "percent"


def test_metric_semantics_preserve_explicit_formula_and_currency_scale() -> None:
    ir = build_semantic_ir(
        "计算万元收入，利润率=利润/收入",
        question_type="calculation",
        calculation_subtype="numeric_computation",
    )

    specs = {item.name: item for item in ir.metric_specs}
    assert specs["收入"].unit == "currency:万元"
    assert specs["利润率"].formula_hint == "利润/收入"


def test_metric_grounding_prefers_governed_sql_attribute() -> None:
    ir = build_semantic_ir(
        "查询客户收入",
        question_type="calculation",
        calculation_subtype="aggregation",
    )

    grounded = ground_metric_semantics(
        ir.model_dump(mode="json"),
        sql_attributes=[
            {
                "id": "sa-revenue",
                "name": "客户收入",
                "description": "已治理的收入指标",
                "expression": "SUM(order_total - refund_total)",
            }
        ],
        column_attributes=[
            {"id": "ca-revenue", "name": "收入", "description": "物理收入列"}
        ],
    )

    assert grounded[0]["status"] == "governed"
    assert grounded[0]["selected"]["id"] == "sa-revenue"


def test_metric_grounding_marks_missing_definition_unresolved() -> None:
    ir = build_semantic_ir(
        "查询客户收入",
        question_type="calculation",
        calculation_subtype="aggregation",
    )

    grounded = ground_metric_semantics(ir.model_dump(mode="json"))

    assert grounded[0]["status"] == "unresolved"
    assert grounded[0]["confidence"] == 0.0
    assert grounded[0]["selected"] is None


def test_unresolved_metric_produces_one_focused_chinese_question() -> None:
    question = build_metric_clarification_question(
        [{"name": "客户收入", "status": "unresolved", "matches": []}]
    )

    assert question == (
        "暂未找到指标“客户收入”的明确治理定义。请补充该指标的业务含义、"
        "对应字段，或提供计算公式后再查询。"
    )


def test_dimension_phrases_drop_counters_and_verb_phrases() -> None:
    """``按客户类别统计订单数`` is the dimension 客户类别, not 客户类别统计."""
    from auto_ontology.retrieval.text_to_sql.semantic_ir import build_semantic_ir

    def dimensions(question: str) -> list[str]:
        return build_semantic_ir(
            question, question_type="calculation", calculation_subtype="aggregation"
        ).dimensions

    assert dimensions("查询最近12个月每个地区的营收") == ["地区"]
    assert dimensions("按客户类别统计订单数") == ["客户类别"]
    assert dimensions("每个销售员的营收") == ["销售员"]
    assert dimensions("按品类看毛利率") == ["品类"]
    assert dimensions("各地区客户数") == ["地区"]
