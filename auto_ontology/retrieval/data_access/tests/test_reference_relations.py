# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Reference relations rank below fact sources, softly.

A view answers with someone else's join and filter; an archive duplicates every
entity that has more than one version, so a customer count doubles without
anything failing. Neither is a reason to hide the relation — a question about
change history needs the archive — so the demotion is a bounded penalty in the
same fusion as certification, not an exclusion.
"""

from __future__ import annotations

import pytest

from auto_ontology.retrieval.data_access import semantic_search
from auto_ontology.retrieval.data_access.semantic_search import _hybrid_rank_rows
from auto_ontology.semantic.relation_kind import (
    KIND_ARCHIVE,
    KIND_TABLE,
    KIND_VIEW,
    classify_name,
    classify_relation,
    infer_from_text,
    is_reference_relation,
)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "table_type", "expected"),
    [
        ("customers", "base table", KIND_TABLE),
        ("customers", "view", KIND_VIEW),
        ("v_customer_last_order", "view", KIND_VIEW),
        ("customers_archive", "base table", KIND_ARCHIVE),
        ("coldroomtemperatures_archive", "base table", KIND_ARCHIVE),
        ("user_history", "base table", KIND_ARCHIVE),
        # A view is a view whatever it is called.
        ("customers_archive", "view", KIND_VIEW),
        # Not a table; nothing claims archive-ness from a column-ish name.
        (None, None, KIND_TABLE),
    ],
)
def test_classify_relation(name, table_type, expected) -> None:
    assert classify_relation(name, table_type) == expected


def test_a_materialized_view_is_a_view() -> None:
    assert classify_relation("monthly_sales", "materialized view") == KIND_VIEW


def test_only_views_and_archives_are_references() -> None:
    assert is_reference_relation(KIND_VIEW) is True
    assert is_reference_relation(KIND_ARCHIVE) is True
    assert is_reference_relation(KIND_TABLE) is False
    assert is_reference_relation(None) is False


def test_a_compiled_term_name_marks_its_archive() -> None:
    """Compile names a term after the table's meaning, not its physical name."""
    assert classify_name("Archived City") == KIND_ARCHIVE
    assert classify_name("cities_archive") == KIND_ARCHIVE
    assert classify_name("Customer") == KIND_TABLE


def test_a_view_is_recognised_by_its_type_even_with_a_plain_name() -> None:
    assert classify_name("Customer", "view") == KIND_VIEW


def test_inference_reads_the_names_a_stored_row_carries() -> None:
    assert infer_from_text("cities_archive") == KIND_ARCHIVE
    assert infer_from_text("City", "cities_archive") == KIND_ARCHIVE
    # A qualified term name is split on the separators qualify_table uses.
    assert infer_from_text("City (db.application.cities_archive)") == KIND_ARCHIVE


def test_inference_does_not_invent_views() -> None:
    """A view leaves no trace in a name; guessing would demote a real table."""
    assert infer_from_text("Customer (db.website.customers)") == KIND_TABLE
    assert infer_from_text("Customer") == KIND_TABLE


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _default_penalty(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("RETRIEVAL_REFERENCE_PENALTY", raising=False)


def _pool(reference_kind: str, *, certified: bool = False) -> list[dict]:
    """A pool of the size retrieval produces, reference row one place ahead.

    Position matters: the penalty is worth ``penalty * len(rows)`` positions in
    the fusion, so a two-row pool cannot express the effect at all.
    """
    rows: list[dict] = [
        {
            "id": "reference",
            "text": "ColumnAttribute: 客户数",
            "relation_kind": reference_kind,
            **({"certified": True} if certified else {}),
        }
    ]
    rows.extend(
        {"id": f"filler{i}", "text": "ColumnAttribute: 客户数"} for i in range(7)
    )
    rows.append({"id": "live", "text": "ColumnAttribute: 客户数"})
    # The live table sits one vector position behind the reference row.
    rows[1], rows[8] = rows[8], rows[1]
    return rows


def test_an_archive_loses_to_a_live_table_at_equal_lexical_evidence() -> None:
    rows = _pool(KIND_ARCHIVE)

    ranked = _hybrid_rank_rows("客户数", rows, len(rows))
    ids = [row["id"] for row in ranked]

    assert ids.index("live") < ids.index("reference")
    penalized = next(row for row in ranked if row["id"] == "reference")
    assert penalized["reference_penalty"] == pytest.approx(
        semantic_search.REFERENCE_RELATION_PENALTY
    )


def test_a_view_loses_to_a_live_table_at_equal_lexical_evidence() -> None:
    rows = _pool(KIND_VIEW)

    ranked = _hybrid_rank_rows("客户数", rows, len(rows))
    ids = [row["id"] for row in ranked]

    assert ids.index("live") < ids.index("reference")


def test_a_named_archive_still_wins() -> None:
    """Asking for the archive by name outranks a table that matches nothing."""
    rows = [
        {
            "id": "archive",
            "text": "Term: 变更历史。cities_archive 的历史版本",
            "name": "变更历史",
            "relation_kind": KIND_ARCHIVE,
        },
        {"id": "table", "text": "Term: 客户。完全无关的描述"},
    ]

    ranked = _hybrid_rank_rows("变更历史", rows, len(rows))

    assert ranked[0]["id"] == "archive"


def test_certification_does_not_rescue_an_archive() -> None:
    """Certified means the wording is right, not that the relation is the source."""
    rows = _pool(KIND_ARCHIVE, certified=True)

    ranked = _hybrid_rank_rows("客户数", rows, len(rows))
    ids = [row["id"] for row in ranked]

    assert ids.index("live") < ids.index("reference")


def test_the_penalty_can_be_switched_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment whose archives are the real fact source needs an escape."""
    rows = _pool(KIND_ARCHIVE)
    penalized = [row["id"] for row in _hybrid_rank_rows("客户数", rows, len(rows))]
    assert penalized.index("reference") > penalized.index("live")

    monkeypatch.setenv("RETRIEVAL_REFERENCE_PENALTY", "0")
    rows = _pool(KIND_ARCHIVE)
    plain = _hybrid_rank_rows("客户数", rows, len(rows))
    ids = [row["id"] for row in plain]

    # Vector order restored: the reference row is back where the index put it.
    assert ids.index("reference") < ids.index("live")
    assert "reference_penalty" not in next(r for r in plain if r["id"] == "reference")


def test_a_non_numeric_override_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RETRIEVAL_REFERENCE_PENALTY", "lots")

    assert semantic_search._reference_penalty() == pytest.approx(
        semantic_search.REFERENCE_RELATION_PENALTY
    )


def test_the_penalty_outweighs_certification() -> None:
    """Otherwise a certified archive definition beats a live plain column."""
    assert (
        semantic_search.REFERENCE_RELATION_PENALTY
        > semantic_search.CERTIFIED_RANK_BONUS
    )


def test_the_penalty_is_below_an_exact_lexical_match() -> None:
    """A question that names the archive must keep it in reach."""
    assert semantic_search.REFERENCE_RELATION_PENALTY < 1.0


# ---------------------------------------------------------------------------
# Rows embedded before the flag existed
# ---------------------------------------------------------------------------


def test_an_old_row_is_classified_from_the_names_it_carries() -> None:
    """Re-embedding a whole deployment is not a precondition for ranking."""
    rows = [
        {
            "id": "old-attr",
            "text": "ColumnAttribute: City Name of Term City",
            "table_name": "cities_archive",
        },
        {
            "id": "old-term",
            "text": "Term: Archived City. Historical versions",
            "name": "Archived City",
        },
        {"id": "live", "text": "Term: Customer", "name": "Customer"},
    ]

    kinds = {row["id"]: semantic_search._row_relation_kind(row) for row in rows}

    assert kinds == {
        "old-attr": KIND_ARCHIVE,
        "old-term": KIND_ARCHIVE,
        "live": KIND_TABLE,
    }


def test_an_exempt_relation_is_never_demoted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The live twin can be empty while the archive holds the whole history.

    Wide World Importers keeps 4 rows in ``coldroomtemperatures`` and 1.2M in
    its archive, so the archive really is the source for that question. A name
    convention cannot tell it from ``customers_archive``, and the deployment can.
    """
    monkeypatch.setenv(
        "RETRIEVAL_REFERENCE_EXEMPT", "coldroomtemperatures_archive, Some Term"
    )

    rows = [
        {"id": "a", "table_name": "coldroomtemperatures_archive"},
        {"id": "b", "name": "Some Term"},
        {"id": "c", "table_name": "customers_archive"},
    ]

    kinds = {row["id"]: semantic_search._row_relation_kind(row) for row in rows}

    assert kinds["a"] == KIND_TABLE
    assert kinds["b"] == KIND_TABLE
    assert kinds["c"] == KIND_ARCHIVE


def test_exemption_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETRIEVAL_REFERENCE_EXEMPT", "ColdRoomTemperatures_Archive")

    kind = semantic_search._row_relation_kind(
        {"table_name": "coldroomtemperatures_archive"}
    )

    assert kind == KIND_TABLE


def test_no_exemption_list_demotes_as_usual(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RETRIEVAL_REFERENCE_EXEMPT", raising=False)

    kind = semantic_search._row_relation_kind(
        {"table_name": "coldroomtemperatures_archive"}
    )

    assert kind == KIND_ARCHIVE
