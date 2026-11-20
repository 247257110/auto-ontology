# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""A question about going quiet must not be answered by entities that never spoke.

``COUNT(events.*, 0, 90, days) = 0`` is true of a customer that never placed an
order for exactly the same reason it is true of one that stopped: no rows in the
window. Ranking both together puts unconverted leads at the top of a churn
shortlist, where each one displaces a customer someone could actually save.

So the population for an absence-of-activity question drops entities with no row
at all in the table being counted. These tests hold that line, and just as much
the boundary: a ``> 0`` question keeps them (there they are honest negatives), a
composite key is left alone, and a read that fails removes nobody.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from auto_ontology.retrieval.kumo.pql_gen import _resolve_population

SILENCE_PQL = (
    "PREDICT COUNT(invoices.*, 0, 90, days) = 0 FOR EACH customers.customer_id"
)
ACTIVITY_PQL = (
    "PREDICT COUNT(invoices.*, 0, 90, days) > 0 FOR EACH customers.customer_id"
)
VOLUME_PQL = (
    "PREDICT SUM(invoices.amount, 0, 90, days) >= 3 FOR EACH customers.customer_id"
)
EDGES = [
    ("invoices", "customer_id", "customers"),
    ("orders", "customer_id", "customers"),
]
TABLE_NAMES = {"invoices": '"retail"."invoices"', "customers": '"retail"."customers"'}


class _Source:
    """Answers the history read with the keys it was told exist."""

    def __init__(self, present: list[Any], *, fail: Exception | None = None) -> None:
        self.present = present
        self.fail = fail
        self.read: list[str] = []

    def execute(self, sql: str) -> Any:
        self.read.append(sql)
        if self.fail is not None:
            raise self.fail
        return pd.DataFrame({"customer_id": self.present})


def _population(
    connector: _Source,
    pql: str = SILENCE_PQL,
    *,
    ids: list[Any] | None = None,
    edges: list[tuple[str, str, str]] | None = None,
    max_entities: int = 1000,
) -> tuple[list[Any], int, str | None]:
    return _resolve_population(
        pql,
        None,
        connector,  # type: ignore[arg-type]
        max_entities,
        TABLE_NAMES,
        {"customers": ids if ids is not None else [1, 2, 3]},
        graph_edges=EDGES if edges is None else edges,
        col_stypes=None,
    )


def test_an_entity_with_no_history_is_dropped_from_a_silence_question() -> None:
    kept, population, note = _population(_Source([2, 3]))

    assert kept == [2, 3]
    assert population == 2
    assert note is not None and "1" in note


def test_the_read_is_scoped_to_the_entity_keys_and_uses_the_real_child_table() -> None:
    connector = _Source([2, 3])

    _population(connector)

    assert len(connector.read) == 1
    sql = connector.read[0]
    assert 'FROM "retail"."invoices"' in sql
    assert '"customer_id" IN (1, 2, 3)' in sql


def test_an_activity_question_keeps_every_entity() -> None:
    """`> 0` asks whether they will act; a lead that never did is a valid no."""
    connector = _Source([2, 3])

    kept, population, note = _population(connector, ACTIVITY_PQL)

    assert kept == [1, 2, 3]
    assert population == 3
    assert note is None
    assert connector.read == []


def test_a_volume_question_keeps_every_entity() -> None:
    connector = _Source([2, 3])

    kept, _population_size, note = _population(connector, VOLUME_PQL)

    assert kept == [1, 2, 3]
    assert note is None
    assert connector.read == []


def test_without_an_edge_the_keys_are_not_touched() -> None:
    """Linking the wrong column would drop live entities; no edge means no guess."""
    connector = _Source([2, 3])

    kept, _population_size, note = _population(
        connector, edges=[("order_lines", "order_id", "orders")]
    )

    assert kept == [1, 2, 3]
    assert note is None
    assert connector.read == []


def test_a_failed_read_removes_nobody() -> None:
    connector = _Source([2, 3], fail=RuntimeError("permission denied"))

    kept, population, note = _population(connector)

    assert kept == [1, 2, 3]
    assert population == 3
    assert note is None


def test_a_read_that_would_empty_the_population_is_ignored() -> None:
    """Every key missing means the read is wrong, not that nobody ever ordered."""
    kept, population, note = _population(_Source([]))

    assert kept == [1, 2, 3]
    assert population == 3
    assert note is None


def test_the_filter_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUMO_NEVER_ACTIVE_FILTER", "0")
    connector = _Source([2, 3])

    kept, _population_size, note = _population(connector)

    assert kept == [1, 2, 3]
    assert note is None
    assert connector.read == []


def test_a_population_larger_than_one_chunk_is_read_in_pieces() -> None:
    connector = _Source(list(range(0, 1200)))

    kept, _population_size, _note = _population(
        connector, ids=list(range(0, 1200)), max_entities=5000
    )

    assert len(kept) == 1200
    assert len(connector.read) == 3
