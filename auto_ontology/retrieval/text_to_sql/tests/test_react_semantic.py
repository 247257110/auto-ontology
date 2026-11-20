# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The ReAct agent's Chinese semantic context: IR, grounding and prompt sections."""

from __future__ import annotations

from typing import Any

from auto_ontology.retrieval.text_to_sql.react import semantic
from auto_ontology.semantic.constants import LABEL_SQL_ATTRIBUTE

GROSS = {
    "id": "sa-gross",
    "name": "营收（含税）",
    "description": "发票行含税金额合计",
    "expression": "SELECT SUM(il.extendedprice) FROM sales.invoicelines il",
    "certified": True,
}
NET = {
    "id": "sa-net",
    "name": "营收（不含税）",
    "description": "发票行不含税金额合计",
    "expression": (
        "SELECT SUM(il.extendedprice - il.taxamount) FROM sales.invoicelines il"
    ),
    "certified": True,
}


def _patch_search(monkeypatch, hits: list[dict[str, Any]]) -> None:
    def fake_search(
        retriever,
        query,
        label_filter=None,
        per_label_k=None,
        database_name=None,
    ):
        if label_filter == [LABEL_SQL_ATTRIBUTE]:
            return hits
        return []

    monkeypatch.setattr(semantic, "search_semantic_index", fake_search)
    monkeypatch.setattr(
        semantic,
        "fetch_sql_attributes_with_sql",
        lambda ids: [hit for hit in hits if str(hit.get("id")) in ids],
    )


def test_a_non_chinese_question_builds_no_context(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise AssertionError("no search should run for an English question")

    monkeypatch.setattr(semantic, "search_semantic_index", fail)

    assert semantic.build_semantic_context("revenue by region", object()).is_empty


def test_a_chinese_metric_is_grounded_and_pinned(monkeypatch) -> None:
    _patch_search(monkeypatch, [GROSS])

    context = semantic.build_semantic_context("查询含税营收", object())

    assert context.ir is not None
    assert context.ir["metrics"] == ["含税营收"]
    assert context.grounded_metrics
    grounded = context.grounded_metrics[0]
    assert grounded["status"] == "governed"
    assert grounded["binding"] == "required"
    assert context.clarification is None

    section = semantic.format_semantic_context(context)
    assert "Certified metric definitions" in section
    assert "SELECT SUM(il.extendedprice)" in section
    assert "营收（含税）" in section


def test_a_metric_without_a_qualifier_asks_which_caliber(monkeypatch) -> None:
    _patch_search(monkeypatch, [GROSS, NET])

    context = semantic.build_semantic_context("查询营收", object())

    assert context.grounded_metrics[0]["status"] == "ambiguous"
    assert context.clarification is not None
    assert "多个口径" in context.clarification


def test_a_failed_search_leaves_the_agent_unchanged(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(semantic, "search_semantic_index", fail)

    context = semantic.build_semantic_context("查询含税营收", object())

    assert context.ir is not None
    assert context.grounded_metrics == []
    assert context.clarification is None


def test_an_empty_context_renders_nothing() -> None:
    assert semantic.format_semantic_context(None) == ""
    assert semantic.format_semantic_context(semantic.SemanticContext()) == ""


def test_a_relative_time_range_is_anchored_on_the_data_window(monkeypatch) -> None:
    from datetime import date

    from auto_ontology.retrieval.text_to_sql.time_anchor import DataWindow

    monkeypatch.setattr(
        semantic,
        "probe_data_window",
        lambda connector, tables: DataWindow(
            start=date(2024, 1, 1),
            end=date(2026, 11, 17),
            source="orders.order_date",
        ),
    )
    context = semantic.SemanticContext(
        ir={
            "time_ranges": [
                {
                    "expression": "最近12个月",
                    "canonical": "rolling_12mo",
                    "kind": "rolling",
                    "granularity": "month",
                }
            ]
        }
    )

    section = semantic.build_time_anchor(context, object(), [{"name": "orders"}])

    assert "2026-11-17" in section
    assert "order_date >=" in section
    assert "CURRENT_DATE" in section


def test_the_time_anchor_is_computed_once(monkeypatch) -> None:
    from auto_ontology.retrieval.text_to_sql.react.context import ToolContext
    from auto_ontology.retrieval.text_to_sql.react.retrieve import _time_anchor

    ctx = ToolContext(None, [])
    ctx.semantic_context = semantic.SemanticContext(
        ir={"time_ranges": [{"expression": "最近30天", "canonical": "rolling_30d"}]}
    )
    monkeypatch.setattr(
        "auto_ontology.retrieval.text_to_sql.react.retrieve.build_time_anchor",
        lambda *a, **k: "## Time anchoring",
    )

    assert _time_anchor(ctx) == "## Time anchoring"
    assert _time_anchor(ctx) == ""
