# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Scoping the prediction graph's table enrichment to one database.

Written after a two-database deployment (``wideworldimporters`` plus a retail
dataset) failed a churn question with ``sales.invoices could not be read``. The
chain was three links long and each one is a test below:

1. A retrieved PQL example cited ``invoices``. The corpus is global, verified
   against whichever database it was written on.
2. ``_enrich_relevant_tables`` resolved the bare name by table name alone, so it
   returned the *other* database's ``sales.invoices`` — and, because the lookup
   row carried no database name, dropped that information.
3. ``_load_relevant_frames`` fell back to the first configured connector when a
   table had no database name, so it ran the foreign schema-qualified name on
   the scoped connection.

The dependency-free tests use a fake lookup; the frame-loading test uses a fake
connector. No database is required.
"""

from __future__ import annotations

from typing import Any

import pytest

from auto_ontology.retrieval.text_to_sql.agents import prediction_graph
from auto_ontology.retrieval.kumo.predictor import (
    TableUnavailable,
    _load_relevant_frames,
)


def _table(name: str, schema: str, database: str) -> dict[str, Any]:
    return {
        "id": f"{database}.{schema}.{name}",
        "name": name,
        "schema_name": schema,
        "database_name": database,
        "description": "",
        "pk": [f"{name}_id"],
        "label": "TABLE",
    }


def _example(pql: str) -> dict[str, str]:
    return {"question": "how many?", "query": pql}


@pytest.fixture()
def scoped_tables() -> list[dict[str, Any]]:
    return [
        _table("customers", "retail", "retail_churn"),
        _table("orders", "retail", "retail_churn"),
    ]


def test_foreign_table_is_not_pulled_in_and_its_example_is_dropped(
    monkeypatch: pytest.MonkeyPatch, scoped_tables: list[dict[str, Any]]
) -> None:
    """The retail database has no ``invoices``; WWI does. Do not borrow it."""

    def lookup(name: str, database_name: str) -> dict[str, Any] | None:
        # Only the other database has this table.
        return (
            _table("invoices", "sales", "wideworldimporters")
            if name.lower() == "invoices"
            else None
        )

    monkeypatch.setattr(prediction_graph, "fetch_table_in_database", lookup)

    tables, examples = prediction_graph._enrich_relevant_tables(
        scoped_tables,
        [
            _example(
                "PREDICT COUNT(sales.invoices.*) = 0 FOR EACH customers.customer_id"
            ),
            _example(
                "PREDICT COUNT(orders.*, 0, 180, days) = 0 FOR EACH customers.customer_id"
            ),
        ],
    )

    assert [t["name"] for t in tables] == ["customers", "orders"]
    assert all(t["database_name"] == "retail_churn" for t in tables)
    assert len(examples) == 1, (
        "the example citing an absent table must not reach the model"
    )
    assert "orders" in examples[0]["query"]


def test_table_that_does_exist_in_scope_is_added_with_its_database(
    monkeypatch: pytest.MonkeyPatch, scoped_tables: list[dict[str, Any]]
) -> None:
    """Enrichment still works; it just has to say which database the table is in."""

    def lookup(name: str, database_name: str) -> dict[str, Any] | None:
        return (
            _table("invoices", "retail", "retail_churn")
            if name.lower() == "invoices"
            else None
        )

    monkeypatch.setattr(prediction_graph, "fetch_table_in_database", lookup)

    tables, examples = prediction_graph._enrich_relevant_tables(
        scoped_tables,
        [_example("PREDICT COUNT(invoices.*) = 0 FOR EACH customers.customer_id")],
    )

    added = {t["name"]: t for t in tables}["invoices"]
    assert added["database_name"] == "retail_churn", (
        "a table added without its database name makes the frame loader guess"
    )
    assert len(examples) == 1


def test_unscoped_tables_still_use_the_ambiguous_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single-database deployments carry legacy tables with no database name."""

    calls: list[str] = []

    def ambiguous(name: str) -> dict[str, Any] | None:
        calls.append(name)
        return _table("invoices", "public", "")

    monkeypatch.setattr(prediction_graph, "fetch_table_by_name", ambiguous)

    tables, _ = prediction_graph._enrich_relevant_tables(
        [{"id": "t1", "name": "customers", "schema_name": "public"}],
        [_example("PREDICT COUNT(invoices.*) = 0 FOR EACH customers.customer_id")],
    )

    assert calls == ["invoices"]
    assert {t["name"] for t in tables} == {"customers", "invoices"}


class _FakeConnector:
    def __init__(self, database_name: str) -> None:
        self.database_name = database_name
        self.read: list[str] = []

    def execute(self, sql: str) -> Any:  # pragma: no cover - not reached here
        self.read.append(sql)
        raise AssertionError("should not be asked to read an unroutable table")


class _Spend:
    def check_deadline(self, _label: str) -> None:
        return None

    def add_table(self, _name: str, _frame: Any) -> None:
        return None


def test_a_table_without_a_database_name_is_refused_when_two_are_connected() -> None:
    """Silently reading the first connector is how a foreign table gets queried."""

    connectors = [_FakeConnector("retail_churn"), _FakeConnector("wideworldimporters")]

    with pytest.raises(TableUnavailable) as raised:
        _load_relevant_frames(
            connectors,
            [{"name": "invoices", "schema_name": "sales"}],
            _Spend(),
        )

    assert "no database name" in str(raised.value)
    assert "target_db" in str(raised.value)
    assert not connectors[0].read, "must not fall back to the first connector"
