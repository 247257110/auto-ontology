# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""A prediction row names its entity by primary key; a reader needs the name.

`ENTITY=509` is exact and useless on its own — answering "who is 509?" meant
going back to the warehouse. These tests hold the one bounded read that fills the
name in, and just as much the cases where it must not: no name column, the wrong
connection, a key that cannot be written safely, a read that fails.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from auto_ontology.retrieval.kumo.predictor import (
    PredictionContext,
    _entity_labels,
    _format_result,
    _label_column,
    _sql_literal,
)
from auto_ontology.retrieval.kumo.telemetry import GraphIdentity

PQL = "PREDICT COUNT(orders.*, 0, 90, days) = 0 FOR EACH customers.customer_id"

COL_STYPES = {
    "customers": {
        "customer_id": "ID",
        "company_name": "categorical",
        "customercategoryname": "categorical",
        "industry": "categorical",
        "email": "categorical",
        "signup_date": "datetime",
    },
    "orders": {"order_id": "ID", "order_date": "datetime"},
}


class _Connector:
    """Answers the label read, and remembers what it was asked."""

    def __init__(self, frame: Any = None, *, fail: Exception | None = None) -> None:
        self.frame = frame
        self.fail = fail
        self.read: list[str] = []

    def execute(self, sql: str) -> Any:
        self.read.append(sql)
        if self.fail is not None:
            raise self.fail
        return self.frame


def _context(**overrides: Any) -> PredictionContext:
    base: dict[str, Any] = {
        "kumo_model": None,
        "connector": None,
        "graph_ddl": "",
        "graph_edges": [],
        "graph_col_stypes": {},
        "time_columns": {},
        "table_names": {},
        "entity_ids": {},
        "examples": [],
        "column_reference": "",
        "identity": GraphIdentity(),
    }
    base.update(overrides)
    return PredictionContext(**base)


def _rows(*keys: Any) -> list[dict[str, Any]]:
    return [
        {"ENTITY": key, "anchor_timestamp": "2025-03-25T00:00:00Z", "True_PROB": 0.9}
        for key in keys
    ]


def _labelled(
    connector: _Connector, rows: list[dict[str, Any]], **overrides: Any
) -> Any:
    base: dict[str, Any] = {
        "connector": connector,
        "graph_col_stypes": COL_STYPES,
        "table_names": {"customers": '"retail"."customers"'},
    }
    base.update(overrides)
    return _entity_labels(
        rows,
        pql=PQL,
        context=_context(**base),
        value_columns=["ENTITY", "anchor_timestamp", "True_PROB"],
    )


# -- choosing the column ----------------------------------------------------


def test_the_entitys_own_name_wins_over_a_category_that_also_ends_in_name() -> None:
    """``customercategoryname`` is a kind of customer; ``company_name`` is which one."""
    assert _label_column("Customers", COL_STYPES) == "company_name"


def test_a_table_with_no_name_shaped_column_is_left_alone() -> None:
    assert _label_column("Orders", COL_STYPES) is None


def test_contact_details_are_never_used_as_a_label() -> None:
    """A churn answer must not carry an email address next to a probability."""
    assert _label_column("Contacts", {"contacts": {"email": "categorical"}}) is None
    assert _label_column("People", {"people": {"first_name": "categorical"}}) is None


def test_a_bare_name_column_is_enough() -> None:
    assert _label_column("Regions", {"regions": {"name": "categorical"}}) == "name"


# -- the read ---------------------------------------------------------------


def test_the_name_is_read_back_for_the_keys_in_the_answer_and_placed_next_to_them() -> (
    None
):
    connector = _Connector(
        pd.DataFrame({"customer_id": [1, 2], "company_name": ["甲公司", "乙公司"]})
    )
    rows, columns = _labelled(connector, _rows(1, 2))

    assert list(rows[0]) == ["ENTITY", "company_name", "anchor_timestamp", "True_PROB"]
    assert rows[0]["company_name"] == "甲公司"
    assert rows[1]["company_name"] == "乙公司"
    assert columns == ["ENTITY", "company_name", "anchor_timestamp", "True_PROB"]


def test_only_the_keys_being_shown_are_asked_for() -> None:
    connector = _Connector(pd.DataFrame({"customer_id": [372], "company_name": ["丙"]}))

    _labelled(connector, _rows(372))

    assert len(connector.read) == 1
    sql = connector.read[0]
    assert 'FROM "retail"."customers"' in sql
    assert '"customer_id"' in sql and '"company_name"' in sql
    assert "IN (372)" in sql


def test_a_key_containing_a_quote_cannot_end_the_literal_early() -> None:
    connector = _Connector(pd.DataFrame())

    _labelled(connector, _rows("a'b"))

    assert "'a''b'" in connector.read[0]
    assert _sql_literal(None) is None
    assert _sql_literal(True) is None


# -- when it must not happen -------------------------------------------------


def test_a_failed_read_leaves_the_answer_as_it_was() -> None:
    connector = _Connector(fail=RuntimeError("permission denied"))

    rows, columns = _labelled(connector, _rows(1))

    assert rows == _rows(1)
    assert columns == ["ENTITY", "anchor_timestamp", "True_PROB"]


def test_a_table_read_through_another_connection_is_not_read_back() -> None:
    """No routing entry means no read — a same-named table elsewhere is not this table."""
    connector = _Connector(pd.DataFrame({"customer_id": [1], "company_name": ["甲"]}))

    rows, columns = _labelled(
        connector,
        _rows(1),
        table_connectors={"orders": _Connector(pd.DataFrame())},
    )

    assert connector.read == []
    assert rows == _rows(1)
    assert columns == ["ENTITY", "anchor_timestamp", "True_PROB"]


def test_the_routed_connection_is_the_one_used() -> None:
    wrong = _Connector(pd.DataFrame({"customer_id": [1], "company_name": ["wrong-db"]}))
    right = _Connector(pd.DataFrame({"customer_id": [1], "company_name": ["甲"]}))

    rows, _columns = _labelled(wrong, _rows(1), table_connectors={"Customers": right})

    assert wrong.read == []
    assert rows[0]["company_name"] == "甲"


def test_a_grouped_result_has_no_entity_key_to_look_up() -> None:
    connector = _Connector(pd.DataFrame({"customer_id": [1], "company_name": ["甲"]}))

    rows, columns = _entity_labels(
        [{"segment": "SMB", "True_PROB": 0.4}],
        pql=PQL,
        context=_context(connector=connector, graph_col_stypes=COL_STYPES),
        value_columns=["segment", "True_PROB"],
    )

    assert connector.read == []
    assert columns == ["segment", "True_PROB"]


def test_a_column_the_prediction_already_returned_is_not_overwritten() -> None:
    connector = _Connector(pd.DataFrame({"customer_id": [1], "company_name": ["甲"]}))
    rows = [{"ENTITY": 1, "company_name": "already-there", "True_PROB": 0.9}]

    labelled, _columns = _labelled(connector, rows)

    assert labelled[0]["company_name"] == "already-there"


def test_labels_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUMO_ENTITY_LABELS", "0")
    connector = _Connector(pd.DataFrame({"customer_id": [1], "company_name": ["甲"]}))

    rows, _columns = _labelled(connector, _rows(1))

    assert connector.read == []
    assert rows == _rows(1)


def test_without_a_context_the_answer_is_unchanged() -> None:
    """The formatter is called before a graph exists in some paths and in tests."""
    result = _format_result(_Answer())

    assert result["sql_columns"] == ["ENTITY", "anchor_timestamp", "True_PROB"]
    assert result["sql_response_from_db"] == _rows(1)


def test_the_formatted_answer_carries_both_the_key_and_the_name() -> None:
    connector = _Connector(pd.DataFrame({"customer_id": [1], "company_name": ["甲"]}))
    context = _context(
        connector=connector,
        graph_col_stypes=COL_STYPES,
        table_names={"customers": '"retail"."customers"'},
    )

    result = _format_result(_Answer(), context)

    assert result["sql_columns"] == [
        "ENTITY",
        "company_name",
        "anchor_timestamp",
        "True_PROB",
    ]
    assert result["sql_response_from_db"][0]["company_name"] == "甲"
    assert result["sql_response_from_db"][0]["ENTITY"] == 1


class _Answer:
    success = True
    question = "哪些客户下季度会流失？"
    pql = PQL
    note = None
    num_entities = 1
    population = 1
    truncated = False
    columns = ["ENTITY", "anchor_timestamp", "True_PROB"]
    rows = _rows(1)
