# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Metric grounding: qualifiers, certification, binding and clarification.

Three behaviours are pinned here, in the order they matter:

1. A question that *states* a qualifier ("含税营收") locks onto the definition
   carrying that qualifier instead of reporting the pair as ambiguous.
2. A certified definition beats an uncertified lookalike, so a signed-off
   formula is used rather than the user being asked to choose.
3. Only a certified SQL Attribute that is clearly alone becomes a *binding*
   ("reuse verbatim"); a Custom Analysis never does, because it teaches a
   pattern rather than a formula.
"""

from __future__ import annotations

import pytest

from auto_ontology.retrieval.text_to_sql.react.semantic import (
    format_metric_bindings_section,
    format_metric_semantics_section,
)
from auto_ontology.retrieval.text_to_sql.metric_grounding import (
    block_unresolved_metrics,
    build_metric_clarification_question,
    extract_qualifiers,
    ground_metric_semantics,
)

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


def _ir(metric: str) -> dict:
    return {
        "language": "zh",
        "question": f"查询{metric}",
        "question_type": "calculation",
        "metric_specs": [{"name": metric, "aggregation": "sum", "comparison": "none"}],
    }


# ---------------------------------------------------------------------------
# Qualifier extraction
# ---------------------------------------------------------------------------


def test_extract_qualifiers_treats_excluded_as_distinct_from_included() -> None:
    """ "不含税" must not also register as "含税" by substring."""
    assert extract_qualifiers("不含税营收") == frozenset({"tax_excluded"})
    assert extract_qualifiers("含税营收") == frozenset({"tax_included"})
    assert extract_qualifiers("营收") == frozenset()
    assert extract_qualifiers("net of tax") == frozenset({"tax_excluded"})


# ---------------------------------------------------------------------------
# Qualifier-aware selection
# ---------------------------------------------------------------------------


def test_a_stated_qualifier_selects_the_matching_definition() -> None:
    grounded = ground_metric_semantics(_ir("含税营收"), sql_attributes=[NET, GROSS])[0]

    assert grounded["status"] == "governed"
    assert grounded["selected"]["id"] == "sa-gross"
    assert grounded["qualifiers"] == ["tax_included"]
    assert grounded["resolved_by"] == "qualifier"


def test_a_stated_qualifier_selects_the_excluded_definition() -> None:
    grounded = ground_metric_semantics(_ir("不含税营收"), sql_attributes=[NET, GROSS])[
        0
    ]

    assert grounded["selected"]["id"] == "sa-net"
    assert grounded["resolved_by"] == "qualifier"


def test_no_qualifier_and_two_governed_definitions_needs_the_user() -> None:
    """Both certified, neither qualified by the question — a real choice."""
    grounded = ground_metric_semantics(_ir("营收"), sql_attributes=[NET, GROSS])[0]

    assert grounded["status"] == "ambiguous"
    assert grounded["binding"] == "none"


def test_the_ambiguity_question_names_the_differing_qualifier() -> None:
    grounded = ground_metric_semantics(_ir("营收"), sql_attributes=[NET, GROSS])

    question = build_metric_clarification_question(grounded)

    assert question is not None
    assert "口径" in question
    assert "含税" in question or "不含税" in question


# ---------------------------------------------------------------------------
# Certification as a tie-break
# ---------------------------------------------------------------------------


def test_a_certified_definition_wins_over_an_uncertified_lookalike() -> None:
    uncertified = {**NET, "id": "sa-draft", "certified": False}

    grounded = ground_metric_semantics(
        _ir("不含税营收"), sql_attributes=[uncertified, GROSS]
    )[0]

    # GROSS is certified but conflicts on the stated qualifier; the prompt
    # qualifier has to win, otherwise "不含税" would silently return a
    # tax-inclusive number.
    assert grounded["selected"]["id"] == "sa-draft"


def test_certification_breaks_a_qualifier_free_tie() -> None:
    """Same name and qualifiers; only one is signed off."""
    draft = {
        "id": "sa-draft",
        "name": "毛利率",
        "description": "毛利额除以不含税营收",
        "expression": "SELECT 1",
        "certified": False,
    }
    certified = {
        "id": "sa-cert",
        "name": "毛利率",
        "description": "毛利额除以不含税营收",
        "expression": "SELECT 2",
        "certified": True,
    }

    grounded = ground_metric_semantics(
        _ir("毛利率"), sql_attributes=[draft, certified]
    )[0]

    assert grounded["status"] == "governed"
    assert grounded["resolved_by"] == "certification"
    assert grounded["selected"]["id"] == "sa-cert"


# ---------------------------------------------------------------------------
# Binding
# ---------------------------------------------------------------------------


def test_a_single_certified_attribute_becomes_a_required_binding() -> None:
    margin = {
        "id": "sa-margin",
        "name": "毛利率",
        "description": "毛利额 / 不含税营收",
        "expression": (
            "SELECT SUM(il.lineprofit) / NULLIF(SUM(il.extendedprice - il.taxamount), 0) "
            "FROM sales.invoicelines il"
        ),
        "certified": True,
    }

    grounded = ground_metric_semantics(_ir("毛利率"), sql_attributes=[margin])[0]

    assert grounded["status"] == "governed"
    assert grounded["binding"] == "required"
    assert grounded["confidence"] >= 0.9


def test_an_uncertified_attribute_is_only_guidance() -> None:
    margin = {
        "id": "sa-margin",
        "name": "毛利率",
        "expression": "SELECT 1",
        "certified": False,
    }

    grounded = ground_metric_semantics(_ir("毛利率"), sql_attributes=[margin])[0]

    assert grounded["binding"] == "guidance"


def test_a_custom_analysis_is_never_made_mandatory() -> None:
    """It teaches a pattern, so pinning it as a formula would over-constrain."""
    analysis = {
        "id": "ca-rank",
        "name": "毛利率",
        "description": "毛利率排名范式",
        "sql": "SELECT 1",
        "certified": True,
    }

    grounded = ground_metric_semantics(_ir("毛利率"), custom_analyses=[analysis])[0]

    assert grounded["status"] == "governed"
    assert grounded["binding"] == "guidance"


def test_a_qualified_definition_is_not_silently_pinned_for_a_bare_metric() -> None:
    """Pinning "营收（不含税）" for a bare "营收" would choose for the user."""
    grounded = ground_metric_semantics(_ir("营收"), sql_attributes=[NET])[0]

    assert grounded["status"] == "governed"
    assert grounded["binding"] == "guidance"


def test_a_close_runner_up_prevents_binding() -> None:
    margin = {
        "id": "sa-margin",
        "name": "毛利率",
        "expression": "SELECT 1",
        "certified": True,
    }
    other = {
        "id": "sa-margin-2",
        "name": "毛利率",
        "expression": "SELECT 2",
        "certified": True,
    }

    grounded = ground_metric_semantics(_ir("毛利率"), sql_attributes=[margin, other])[0]

    assert grounded["binding"] == "none"


# ---------------------------------------------------------------------------
# Clarification gating
# ---------------------------------------------------------------------------


def test_a_bound_metric_is_never_escalated_to_the_user() -> None:
    margin = {
        "id": "sa-margin",
        "name": "毛利率",
        "expression": "SELECT 1",
        "certified": True,
    }
    grounded = ground_metric_semantics(_ir("毛利率"), sql_attributes=[margin])

    assert build_metric_clarification_question(grounded) is None


def test_unresolved_metrics_block_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("METRIC_GROUNDING_BLOCK_UNRESOLVED", raising=False)
    assert block_unresolved_metrics() is True

    grounded = ground_metric_semantics(_ir("神秘指标"))
    assert build_metric_clarification_question(grounded) is not None


def test_unresolved_metrics_can_be_allowed_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long-tail metric should be able to fall back to physical columns."""
    monkeypatch.setenv("METRIC_GROUNDING_BLOCK_UNRESOLVED", "false")

    grounded = ground_metric_semantics(_ir("神秘指标"))

    assert grounded[0]["status"] == "unresolved"
    assert build_metric_clarification_question(grounded) is None


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def test_binding_section_renders_the_certified_expression() -> None:
    margin = {
        "id": "sa-margin",
        "name": "毛利率",
        "expression": "SELECT SUM(il.lineprofit) FROM sales.invoicelines il",
        "certified": True,
    }
    grounded = ground_metric_semantics(_ir("毛利率"), sql_attributes=[margin])

    section = format_metric_bindings_section(grounded)

    assert "reuse these verbatim" in section
    assert "SELECT SUM(il.lineprofit)" in section
    assert "毛利率" in section


def test_no_binding_section_without_a_binding() -> None:
    assert format_metric_bindings_section([]) == ""
    assert format_metric_bindings_section([{"name": "x", "binding": "guidance"}]) == ""


def test_metric_semantics_section_marks_certification_and_qualifiers() -> None:
    grounded = ground_metric_semantics(_ir("含税营收"), sql_attributes=[NET, GROSS])

    section = format_metric_semantics_section(
        {"semantic_ir": _ir("含税营收"), "grounded_metrics": grounded}
    )

    assert "qualifiers=tax_included" in section
    assert "certified" in section


# ---------------------------------------------------------------------------
# Dimensions (concept-level terms)
# ---------------------------------------------------------------------------

REGION_CONCEPT = {
    "id": "sa-region",
    "name": "客户地域（州省维度）",
    "description": "客户按州省聚合，deliverycityid → cities → stateprovinces",
    "expression": (
        "SELECT sp.stateprovincename FROM sales.customers c "
        "JOIN application.cities ct ON ct.cityid = c.deliverycityid "
        "JOIN application.stateprovinces sp "
        "ON sp.stateprovinceid = ct.stateprovinceid "
        "GROUP BY sp.stateprovincename"
    ),
    "certified": True,
}


def _ir_with_dimension(dimension: str, metric: str = "营收") -> dict:
    return {
        "metric_specs": [{"name": metric, "aggregation": "sum", "comparison": "none"}],
        "dimensions": [dimension],
    }


def test_a_dimension_is_grounded_alongside_the_metric() -> None:
    grounded = ground_metric_semantics(
        _ir_with_dimension("地域"), sql_attributes=[REGION_CONCEPT]
    )

    roles = {entry["name"]: entry["role"] for entry in grounded}
    assert roles["营收"] == "metric"
    assert roles["地域"] == "dimension"


def test_a_certified_dimension_concept_becomes_bindable() -> None:
    """This is what makes a join path enforceable instead of advisory."""
    grounded = ground_metric_semantics(
        _ir_with_dimension("地域"), sql_attributes=[REGION_CONCEPT]
    )
    dimension = next(e for e in grounded if e["role"] == "dimension")

    assert dimension["status"] == "governed"
    assert dimension["binding"] == "required"


def test_an_uncertified_dimension_stays_guidance() -> None:
    draft = {**REGION_CONCEPT, "certified": False}
    grounded = ground_metric_semantics(
        _ir_with_dimension("地域"), sql_attributes=[draft]
    )
    dimension = next(e for e in grounded if e["role"] == "dimension")

    assert dimension["binding"] == "guidance"


def test_a_dimension_never_blocks_the_query() -> None:
    """'按地域' with no governed definition is ordinary — it is still a column."""
    grounded = ground_metric_semantics(
        {"metric_specs": [], "dimensions": ["地域"]}, sql_attributes=[]
    )
    dimension = next(e for e in grounded if e["role"] == "dimension")

    assert dimension["status"] == "unresolved"
    assert build_metric_clarification_question(grounded) is None


def test_a_phrase_that_is_both_is_grounded_once_as_the_metric() -> None:
    grounded = ground_metric_semantics(
        {
            "metric_specs": [{"name": "营收", "aggregation": "sum"}],
            "dimensions": ["营收"],
        },
        sql_attributes=[],
    )

    assert [entry["name"] for entry in grounded] == ["营收"]
    assert grounded[0]["role"] == "metric"


def test_the_dimension_binding_section_renders_the_join_path() -> None:
    from auto_ontology.retrieval.text_to_sql.react.semantic import (
        format_dimension_bindings_section,
        format_metric_bindings_section,
    )

    grounded = ground_metric_semantics(
        _ir_with_dimension("地域"), sql_attributes=[REGION_CONCEPT]
    )

    section = format_dimension_bindings_section(grounded)

    assert "dimension" in section.lower()
    assert "deliverycityid" in section
    assert "stateprovinceid" in section
    # A dimension is a path, not a formula: it must not claim the metric too.
    assert format_metric_bindings_section(grounded) == ""
