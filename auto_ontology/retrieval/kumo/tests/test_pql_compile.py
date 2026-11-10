# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The PQL → task-spec compiler that the hosted backend relies on."""

from __future__ import annotations

import pytest

from auto_ontology.retrieval.kumo.pql_compile import (
    BINARY,
    LINK,
    MULTICLASS,
    REGRESSION,
    PqlCompileError,
    compile_pql,
)

CUSTOMERS = {"customers": {"customercategoryid": "categorical", "creditlimit": "numerical"}}


@pytest.mark.parametrize(
    ("pql", "kind", "table", "column", "func"),
    [
        (
            "PREDICT COUNT(orders.*, 0, 90, days) > 0 FOR EACH customers.customerid",
            BINARY,
            "orders",
            None,
            "COUNT",
        ),
        (
            "PREDICT COUNT(invoices.*, 0, 90, days) = 0 FOR EACH customers.customerid "
            "WHERE customers.customercategoryid = 3",
            BINARY,
            "invoices",
            None,
            "COUNT",
        ),
        (
            "PREDICT SUM(sales.invoicelines.extendedprice, 0, 30, days) "
            "FOR EACH customers.customerid",
            REGRESSION,
            "invoicelines",
            "extendedprice",
            "SUM",
        ),
        (
            "PREDICT COUNT(stockitemtransactions.* "
            "WHERE stockitemtransactions.quantity < 0, 0, 90, days) "
            "FOR EACH stockitems.stockitemid",
            REGRESSION,
            "stockitemtransactions",
            None,
            "COUNT",
        ),
    ],
)
def test_windowed_aggregations(pql, kind, table, column, func):
    spec = compile_pql(pql, col_stypes=CUSTOMERS)

    assert spec.kind == kind
    assert spec.target.func == func
    assert spec.target.table == table
    assert spec.target.column == column
    assert spec.target.window is not None
    assert (spec.target.window.start, spec.target.window.end) == (0, 30 if func == "SUM" else 90)


def test_schema_prefix_is_stripped_to_the_bare_table():
    spec = compile_pql(
        "PREDICT sales.invoicelines.extendedprice FOR EACH sales.customers.customerid"
    )

    assert spec.entity_table == "customers"
    assert spec.target.direct == ("invoicelines", "extendedprice")


def test_entity_filter_and_single_entity_form():
    population = compile_pql(
        "PREDICT COUNT(orders.*, 0, 90, days) > 0 FOR EACH customers.customerid "
        "WHERE customers.region = 'apac'"
    )
    single = compile_pql(
        "PREDICT COUNT(orders.*, 0, 90, days) > 0 FOR customers.customerid = 1"
    )

    assert population.entity_filter == "customers.region = 'apac'"
    assert population.single_entity is False
    assert single.single_entity is True
    assert single.entity_filter == "customerid = 1"


def test_categorical_column_is_multiclass_and_numeric_stays_regression():
    categorical = compile_pql(
        "PREDICT customers.customercategoryid FOR EACH customers.customerid",
        col_stypes=CUSTOMERS,
    )
    numeric = compile_pql(
        "PREDICT customers.creditlimit FOR EACH customers.customerid", col_stypes=CUSTOMERS
    )

    assert categorical.kind == MULTICLASS
    assert numeric.kind == REGRESSION


def test_link_target_compiles_but_is_left_for_the_caller_to_refuse():
    spec = compile_pql(
        "PREDICT LIST_DISTINCT(invoicelines.stockitemid, 0, 90, days) RANK TOP 5 "
        "FOR EACH customers.customerid"
    )

    assert spec.kind == LINK
    assert spec.rank_top == 5


def test_forecast_count_is_kept_on_the_spec():
    spec = compile_pql(
        "PREDICT SUM(invoicelines.extendedprice, 0, 1, months) FORECAST 6 TIMEFRAMES "
        "FOR customers.customerid = 1"
    )

    assert spec.forecast == 6
    assert spec.entity_pk == "customerid"


@pytest.mark.parametrize(
    "pql",
    [
        # Two conditions in one target: the endpoint evaluates one.
        "PREDICT COUNT(orders.*, 0, 90, days) = 0 AND SUM(orders.amount, 0, 90, days) > 5 "
        "FOR EACH customers.customerid",
        # The window arguments are what make the aggregation evaluable.
        "PREDICT COUNT(orders.*, 0, 90) FOR EACH customers.customerid",
        # Not PQL at all.
        "SELECT * FROM orders",
        # A counterfactual the endpoint cannot express.
        "PREDICT COUNT(orders.*, 0, 90, days) > 0 ASSUMING customers.creditlimit = 100 "
        "FOR EACH customers.customerid",
        # No entity population.
        "PREDICT COUNT(orders.*, 0, 90, days) > 0",
        # `*` counts rows; it cannot be summed.
        "PREDICT SUM(orders.*, 0, 90, days) FOR EACH customers.customerid",
        # RANK only ranks a link or a multi-class target.
        "PREDICT COUNT(orders.*, 0, 90, days) RANK TOP 5 FOR EACH customers.customerid",
        # An unsupported time unit.
        "PREDICT COUNT(orders.*, 0, 90, decades) FOR EACH customers.customerid",
    ],
)
def test_unsupported_shapes_are_refused_with_a_repair_message(pql):
    with pytest.raises(PqlCompileError) as caught:
        compile_pql(pql, col_stypes=CUSTOMERS)

    assert str(caught.value).strip()
