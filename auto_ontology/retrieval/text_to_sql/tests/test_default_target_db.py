# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Database pinning for the chat paths.

Written after a deployment with ``wideworldimporters`` (51 tables, a demo
warehouse) plus ``retail_churn`` (11 tables, a churn library) answered
"客户营收排名Top10" from the churn library: both databases produce hits for the
same question and the vote broke the tie on the database name.

Two pins, each covering a different failure:

* ``CHAT_DEFAULT_TARGET_DB`` keeps the SQL path on the warehouse.
* ``CHAT_PREDICTION_TARGET_DB`` keeps the PQL path on the library it was
  trained and scored against — left to the vote, a churn question lands on the
  warehouse (more matching columns, no churn signal) and the run dies resolving
  ``sales.customers`` against a connection that only has ``retail.customers``.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from auto_ontology.retrieval.text_to_sql import database_scope


@dataclass
class _Connector:
    """The only attribute the scope logic reads off a connector."""

    database_name: str


@pytest.fixture()
def connectors() -> list[_Connector]:
    return [
        _Connector("retail_churn"),
        _Connector("wideworldimporters"),
    ]


def _resolve(
    connectors: list[_Connector],
    *,
    requested: str | None = None,
    question_type: str | None = "calculation",
    question: str = "客户营收排名Top10",
    default: str | None = "wideworldimporters",
    prediction_default: str | None = None,
) -> tuple[str | None, str]:
    return database_scope.resolve_effective_target_db(
        requested=requested,
        question_type=question_type,
        question=question,
        connectors=connectors,
        default=default,
        prediction_default=prediction_default,
    )


def test_unset_default_keeps_the_vote(connectors: list[_Connector]) -> None:
    assert _resolve(connectors, default=None) == (None, database_scope.SCOPE_NONE)
    assert _resolve(connectors, default="   ") == (None, database_scope.SCOPE_NONE)


def test_default_pins_a_calculation(connectors: list[_Connector]) -> None:
    assert _resolve(connectors) == ("wideworldimporters", database_scope.SCOPE_DEFAULT)


def test_explicit_request_beats_both_pins(connectors: list[_Connector]) -> None:
    assert _resolve(connectors, requested="retail_churn") == (
        "retail_churn",
        database_scope.SCOPE_REQUESTED,
    )
    assert _resolve(
        connectors,
        requested="wideworldimporters",
        question_type="prediction",
        prediction_default="retail_churn",
    ) == ("wideworldimporters", database_scope.SCOPE_REQUESTED)


def test_prediction_pins_to_its_own_library(connectors: list[_Connector]) -> None:
    """A forecast belongs to the library it was trained and scored against."""
    assert _resolve(
        connectors, question_type="prediction", prediction_default="retail_churn"
    ) == ("retail_churn", database_scope.SCOPE_PREDICTION_DEFAULT)


def test_prediction_without_its_own_setting_keeps_the_vote(
    connectors: list[_Connector],
) -> None:
    assert _resolve(connectors, question_type="prediction") == (
        None,
        database_scope.SKIP_PREDICTION,
    )


def test_prediction_ignores_a_default_that_no_connector_serves(
    connectors: list[_Connector],
) -> None:
    assert _resolve(
        connectors,
        question_type="prediction",
        prediction_default="retail_churn_asof_20240630",
    ) == (None, database_scope.SKIP_UNKNOWN_DB)


def test_prediction_naming_another_database_is_not_pinned(
    connectors: list[_Connector],
) -> None:
    """Asking about the warehouse's own future must not be scoped to churn."""
    assert _resolve(
        connectors,
        question_type="prediction",
        question="wideworldimporters 里未来 90 天哪些商品会出库最多",
        prediction_default="retail_churn",
    ) == (None, database_scope.SKIP_NAMES_OTHER_DB)


def test_question_naming_another_database_is_not_pinned(
    connectors: list[_Connector],
) -> None:
    """Saying the library's name must reach it without a request parameter."""
    assert _resolve(connectors, question="retail_churn 里 2024 年有多少客户") == (
        None,
        database_scope.SKIP_NAMES_OTHER_DB,
    )


def test_question_naming_the_default_itself_still_pins(
    connectors: list[_Connector],
) -> None:
    assert _resolve(
        connectors, question="wideworldimporters 里 2014 年的月度营收趋势"
    ) == ("wideworldimporters", database_scope.SCOPE_DEFAULT)


def test_pins_are_matched_case_insensitively(connectors: list[_Connector]) -> None:
    assert _resolve(connectors, default="WideWorldImporters") == (
        "wideworldimporters",
        database_scope.SCOPE_DEFAULT,
    )
    assert _resolve(
        connectors, question_type="prediction", prediction_default="Retail_Churn"
    ) == ("retail_churn", database_scope.SCOPE_PREDICTION_DEFAULT)


def test_default_no_connector_serves_is_ignored(connectors: list[_Connector]) -> None:
    """Falling back to the vote beats pinning a database nothing can query."""
    assert _resolve(connectors, default="retail_churn_asof_20240630") == (
        None,
        database_scope.SKIP_UNKNOWN_DB,
    )


def test_env_knobs_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    for knob, reader in (
        (
            database_scope.ENV_DEFAULT_TARGET_DB,
            database_scope.configured_default_target_db,
        ),
        (
            database_scope.ENV_PREDICTION_TARGET_DB,
            database_scope.configured_prediction_target_db,
        ),
    ):
        monkeypatch.delenv(knob, raising=False)
        assert reader() is None

        monkeypatch.setenv(knob, "  wideworldimporters ")
        assert reader() == "wideworldimporters"
