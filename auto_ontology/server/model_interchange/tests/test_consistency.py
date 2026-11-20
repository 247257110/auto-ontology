# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The consistency check catches what reading the exported YAML cannot.

Every case here is a defect that renders as a perfectly ordinary document: a
relationship naming a field that is not there, two metrics sharing one name, a
dataset with no fields, a term silently narrowed to one table, and whole object
types Ossie has no place for. A reviewer reading the file top to bottom sees
none of them.
"""

from __future__ import annotations

import textwrap

import pytest

from auto_ontology.server.model_interchange.consistency import (
    CODE_DANGLING_DATASET,
    CODE_DANGLING_FIELD,
    CODE_DUPLICATE_METRIC,
    CODE_EMPTY_DATASET,
    CODE_NOT_EXPRESSIBLE,
    CODE_PROJECTED_TERM,
    CODE_UNPARSEABLE,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    check_ossie_text,
)

CLEAN = textwrap.dedent(
    """\
    version: 0.2.0
    name: shop
    datasets:
    - name: invoice
      source: db.sales.invoices
      fields:
      - name: invoice_id
        datatype: Integer
      - name: customer_id
        datatype: Integer
    - name: customer
      source: db.sales.customers
      fields:
      - name: customer_id
        datatype: Integer
    relationships:
    - name: invoice_to_customer
      from: invoice
      to: customer
      from_columns: [customer_id]
      to_columns: [customer_id]
    metrics:
    - name: 营收
      description: total revenue
    """
)


def _codes(report) -> list[str]:
    return [finding.code for finding in report.findings]


# ---------------------------------------------------------------------------
# The clean case is the important one
# ---------------------------------------------------------------------------


def test_a_consistent_document_reports_nothing() -> None:
    report = check_ossie_text(CLEAN)

    assert report.findings == ()
    assert report.ok is True
    assert report.counts() == {"error": 0, "warning": 0, "info": 0}


def test_the_report_states_what_it_looked_at() -> None:
    """An empty finding list must read as "checked 2 datasets", not "did nothing"."""
    report = check_ossie_text(CLEAN)

    assert (report.datasets, report.relationships, report.metrics) == (2, 1, 1)
    assert report.fields == 3


# ---------------------------------------------------------------------------
# Relationship endpoints
# ---------------------------------------------------------------------------


def test_a_relationship_naming_a_missing_dataset_is_an_error() -> None:
    broken = CLEAN.replace("  to: customer", "  to: nowhere")

    report = check_ossie_text(broken)

    assert CODE_DANGLING_DATASET in _codes(report)
    finding = next(f for f in report.findings if f.code == CODE_DANGLING_DATASET)
    assert "nowhere" in finding.message
    assert finding.detail["side"] == "to"
    assert report.ok is False


def test_a_relationship_naming_a_missing_field_is_an_error() -> None:
    broken = CLEAN.replace(
        "[customer_id]\n  to_columns", "[no_such_column]\n  to_columns"
    )

    report = check_ossie_text(broken)

    assert CODE_DANGLING_FIELD in _codes(report)
    finding = next(f for f in report.findings if f.code == CODE_DANGLING_FIELD)
    assert "no_such_column" in finding.message
    assert finding.detail["missing"] == ["no_such_column"]


def test_both_ends_are_checked() -> None:
    broken = CLEAN.replace("  from_columns: [customer_id]", "  from_columns: [typo]")

    report = check_ossie_text(broken)

    finding = next(f for f in report.findings if f.code == CODE_DANGLING_FIELD)
    assert finding.detail["side"] == "from"


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_duplicate_metric_names_are_reported_with_the_count() -> None:
    """Two metrics with one name cannot both be imported."""
    duplicated = textwrap.dedent(
        """\
        version: 0.2.0
        datasets: []
        relationships: []
        metrics:
        - name: 营收
          description: first
        - name: 营收
          description: second
        """
    )

    report = check_ossie_text(duplicated)

    assert CODE_DUPLICATE_METRIC in _codes(report)
    finding = next(f for f in report.findings if f.code == CODE_DUPLICATE_METRIC)
    assert finding.detail["count"] == 2
    assert report.ok is False


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------


def test_a_dataset_with_no_fields_is_a_warning() -> None:
    empty = textwrap.dedent(
        """\
        version: 0.2.0
        datasets:
        - name: invoice
          fields: []
        relationships: []
        metrics: []
        """
    )

    report = check_ossie_text(empty)

    assert CODE_EMPTY_DATASET in _codes(report)
    assert report.ok is True  # a warning does not block a review
    assert report.counts()[SEVERITY_WARNING] == 1


# ---------------------------------------------------------------------------
# Unusable input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   \n\t"])
def test_an_empty_document_is_an_error(text: str) -> None:
    report = check_ossie_text(text)

    assert _codes(report) == [CODE_UNPARSEABLE]
    assert report.findings[0].severity == SEVERITY_ERROR


def test_invalid_yaml_is_an_error_not_a_crash() -> None:
    report = check_ossie_text("datasets: [unclosed\n  - oops")

    assert _codes(report) == [CODE_UNPARSEABLE]
    assert report.ok is False


def test_a_non_mapping_document_is_an_error() -> None:
    report = check_ossie_text("- just\n- a list\n")

    assert _codes(report) == [CODE_UNPARSEABLE]


# ---------------------------------------------------------------------------
# What only the native document knows
# ---------------------------------------------------------------------------


class _Term:
    def __init__(self, name: str, tables: int) -> None:
        self.name = name
        self.represents = [f"t{i}" for i in range(tables)]


class _SqlAttributes:
    manual = [1, 2, 3]
    table = []
    sql = [4, 5]
    bridge_table = []


class _SemanticLayer:
    def __init__(self) -> None:
        self.terms = [_Term("客户地域", 3), _Term("Customer", 1)]
        self.semantic_fks = [{"x": 1}, {"x": 2}]
        self.sql_attributes = _SqlAttributes()


class _Native:
    semantic_layer = _SemanticLayer()


def test_a_term_narrowed_to_one_table_is_reported() -> None:
    report = check_ossie_text(CLEAN, native=_Native())

    assert CODE_PROJECTED_TERM in _codes(report)
    finding = next(f for f in report.findings if f.code == CODE_PROJECTED_TERM)
    assert "客户地域" in finding.message
    assert finding.detail["represents"] == 3


def test_a_single_table_term_is_not_reported() -> None:
    report = check_ossie_text(CLEAN, native=_Native())

    subjects = [f.subject for f in report.findings if f.code == CODE_PROJECTED_TERM]
    assert subjects == ["客户地域"]


def test_objects_ossie_cannot_hold_are_counted() -> None:
    report = check_ossie_text(CLEAN, native=_Native())

    finding = next(f for f in report.findings if f.code == CODE_NOT_EXPRESSIBLE)
    assert finding.severity == SEVERITY_INFO
    assert finding.detail["sql_attributes"]["manual"] == 3
    assert finding.detail["semantic_fks"] == 2
    assert "5 条 SQL Attribute" in finding.message
    assert report.ok is True


def test_nothing_is_claimed_without_a_native_document() -> None:
    """The Ossie text is the *result* of dropping these; it cannot report them."""
    report = check_ossie_text(CLEAN)

    assert CODE_PROJECTED_TERM not in _codes(report)
    assert CODE_NOT_EXPRESSIBLE not in _codes(report)


# ---------------------------------------------------------------------------
# Locating a finding in the text (for a "jump to it" affordance)
# ---------------------------------------------------------------------------


def test_a_finding_carries_the_line_of_its_subject() -> None:
    broken = CLEAN.replace("  to: customer", "  to: nowhere")

    finding = next(
        f for f in check_ossie_text(broken).findings if f.code == CODE_DANGLING_DATASET
    )

    assert finding.line is not None
    lines = broken.splitlines()
    assert "invoice_to_customer" in lines[finding.line - 1]


def test_an_unlocatable_subject_reports_no_line_rather_than_a_guess() -> None:
    """A miss is stated as a miss; the finding is no less certain for it."""
    native = _Native()
    native.semantic_layer.terms = [_Term("客户地域", 3)]

    finding = next(
        f
        for f in check_ossie_text(CLEAN, native=native).findings
        if f.code == CODE_PROJECTED_TERM
    )

    assert finding.line is None


# ---------------------------------------------------------------------------
# Serialization for the endpoint
# ---------------------------------------------------------------------------


def test_the_report_serializes_to_the_endpoint_shape() -> None:
    payload = check_ossie_text(CLEAN).as_dict()

    assert set(payload) == {"ok", "counts", "shape", "findings"}
    assert payload["shape"] == {
        "datasets": 2,
        "fields": 3,
        "relationships": 1,
        "metrics": 1,
    }
    assert payload["counts"] == {"error": 0, "warning": 0, "info": 0}
