# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Chinese Semantic IR behavior tests."""

from auto_ontology.retrieval.text_to_sql.semantic_ir import build_semantic_ir


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
