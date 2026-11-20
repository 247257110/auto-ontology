# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Dropping ``RANK TOP k`` when the target cannot be ranked.

Observed end to end on a churn question that ended with "列出 20 家客户": the
model wrote

::

    PREDICT COUNT(orders.*, 0, 180, days) = 0
    RANK TOP 20
    FOR EACH customers.customer_id

and the backend answered *"RANK TOP only applies to a link prediction
(LIST_DISTINCT) or a multi-class column target. A binary/regression target needs
no RANK — its scores already rank entities."* The graph had already been built
and uploaded by then, so the refusal wasted the expensive half of the run.

The prompt states this rule twice; the model breaks it anyway. A deterministic
strip is the only reliable fix, and it costs nothing: a prediction scores every
entity, and the formatter orders by that score.
"""

from __future__ import annotations

import pytest

from auto_ontology.retrieval.kumo.pql_gen import drop_unrankable_top_k


def test_binary_target_loses_rank() -> None:
    pql = (
        "PREDICT COUNT(orders.*, 0, 180, days) = 0 \n"
        "RANK TOP 20 \n"
        "FOR EACH customers.customer_id"
    )

    assert drop_unrankable_top_k(pql) == (
        "PREDICT COUNT(orders.*, 0, 180, days) = 0 \nFOR EACH customers.customer_id"
    )


def test_regression_target_loses_rank() -> None:
    pql = "PREDICT SUM(invoices.amount, 0, 90, days) RANK TOP 5 FOR EACH customers.customer_id"

    cleaned = drop_unrankable_top_k(pql)

    assert "RANK" not in cleaned
    assert "FOR EACH customers.customer_id" in cleaned


def test_single_line_strip_keeps_the_words_apart() -> None:
    """The clause's own spacing must not be taken out of the sentence.

    The pattern matches ``[ \\t]*RANK TOP k[ \\t]*``, i.e. the whitespace on both
    sides belongs to the match. Splicing the halves back together therefore
    produced ``= 0FOR EACH``, and the backend rejected the whole prediction with
    "No `FOR EACH <entity>.<primary_key>` found" — after paying for the graph.
    """
    pql = "PREDICT COUNT(orders.*, 0, 180, days) = 0 RANK TOP 20 FOR EACH customers.customer_id"

    assert drop_unrankable_top_k(pql) == (
        "PREDICT COUNT(orders.*, 0, 180, days) = 0 FOR EACH customers.customer_id"
    )


def test_leading_rank_does_not_leave_a_double_space() -> None:
    pql = "PREDICT RANK TOP 20 COUNT(orders.*, 0, 180, days) = 0 FOR EACH customers.customer_id"

    assert drop_unrankable_top_k(pql) == (
        "PREDICT COUNT(orders.*, 0, 180, days) = 0 FOR EACH customers.customer_id"
    )


def test_link_prediction_keeps_rank() -> None:
    pql = (
        "PREDICT LIST_DISTINCT(invoices.invoice_id, 0, 90, days) RANK TOP 3 "
        "FOR EACH customers.customer_id"
    )

    assert drop_unrankable_top_k(pql) == pql


def test_bare_categorical_column_keeps_rank() -> None:
    """A bare column may be multi-categorical, and ranking those is legitimate."""
    pql = "PREDICT customers.segment RANK TOP 3 FOR EACH customers.customer_id"
    col_stypes = {"customers": {"segment": "CATEGORICAL"}}

    assert drop_unrankable_top_k(pql, col_stypes) == pql


def test_bare_numeric_column_loses_rank() -> None:
    pql = "PREDICT customers.credit_limit RANK TOP 3 FOR EACH customers.customer_id"
    col_stypes = {"customers": {"credit_limit": "NUMERICAL"}}

    assert "RANK" not in drop_unrankable_top_k(pql, col_stypes)


@pytest.mark.parametrize(
    "pql",
    [
        "PREDICT COUNT(orders.*, 0, 180, days) = 0 FOR EACH customers.customer_id",
        "PREDICT LIST_DISTINCT(products.product_id, 0, 30, days) RANK TOP 2 "
        "FOR EACH customers.customer_id",
    ],
)
def test_pql_without_rank_is_untouched(pql: str) -> None:
    assert drop_unrankable_top_k(pql) == pql
