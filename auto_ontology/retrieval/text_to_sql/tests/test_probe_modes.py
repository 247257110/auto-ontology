# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The proactive value check is on by default, in a bounded mode.

Two properties matter, and they pull against each other:

* a wrong filter literal must be caught **before** execution, because the only
  post-hoc signal (an empty result) is also what a legitimately-zero answer
  looks like;
* turning that on must not turn on the numeric-scale *heuristic*, which infers
  a unit mismatch and can reject a query that was right.
"""

from __future__ import annotations

from typing import Any

import pytest

from auto_ontology.retrieval.text_to_sql.db_probe.config import (
    PROACTIVE_ALL,
    PROACTIVE_OFF,
    PROACTIVE_VALUES,
    is_db_probe_numeric_scale,
    is_db_probe_proactive,
    proactive_mode,
)


# ---------------------------------------------------------------------------
# Mode selection
# ---------------------------------------------------------------------------


def test_the_value_check_is_on_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DB_PROBE_PROACTIVE", raising=False)

    assert proactive_mode() == PROACTIVE_VALUES
    assert is_db_probe_proactive() is True


def test_the_default_mode_does_not_include_the_scale_heuristic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DB_PROBE_PROACTIVE", raising=False)

    assert is_db_probe_numeric_scale() is False


@pytest.mark.parametrize("value", ["all", "full", "true", "1", "yes", "on"])
def test_explicit_enabling_still_means_everything(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """``true`` was the old boolean; it must keep meaning what it meant."""
    monkeypatch.setenv("DB_PROBE_PROACTIVE", value)

    assert proactive_mode() == PROACTIVE_ALL
    assert is_db_probe_numeric_scale() is True


@pytest.mark.parametrize("value", ["values", "value", "literals"])
def test_the_word_values_selects_the_bounded_mode(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("DB_PROBE_PROACTIVE", value)

    assert proactive_mode() == PROACTIVE_VALUES
    assert is_db_probe_numeric_scale() is False


@pytest.mark.parametrize("value", ["off", "false", "0", "no", "none"])
def test_the_check_can_still_be_turned_off_entirely(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("DB_PROBE_PROACTIVE", value)

    assert proactive_mode() == PROACTIVE_OFF
    assert is_db_probe_proactive() is False


def test_an_unrecognised_value_falls_back_to_the_bounded_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo must not silently turn the check off — or silently widen it."""
    monkeypatch.setenv("DB_PROBE_PROACTIVE", "maybe")

    assert proactive_mode() == PROACTIVE_VALUES


def test_case_and_whitespace_are_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PROBE_PROACTIVE", "  VALUES  ")

    assert proactive_mode() == PROACTIVE_VALUES


def test_mode_is_read_per_call_not_cached_at_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The graph reads it once at startup; this module must not add a second cache."""
    monkeypatch.setenv("DB_PROBE_PROACTIVE", "off")
    assert proactive_mode() == PROACTIVE_OFF

    monkeypatch.setenv("DB_PROBE_PROACTIVE", "all")
    assert proactive_mode() == PROACTIVE_ALL


# ---------------------------------------------------------------------------
# The agent honours the mode
# ---------------------------------------------------------------------------


class _FakeExecutor:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def __enter__(self) -> "_FakeExecutor":
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def run(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "rows": [], "row_count": 0, "error": None}


def _run_agent(monkeypatch: pytest.MonkeyPatch, mode: str) -> dict[str, list[str]]:
    """Run the agent once in *mode*, reporting which finders were called."""
    from auto_ontology.retrieval.text_to_sql.agents import proactive_value_check

    called: dict[str, list[str]] = {"literal": [], "scale": [], "division": []}

    monkeypatch.setenv("DB_PROBE_PROACTIVE", mode)
    monkeypatch.setattr(proactive_value_check, "ProbeExecutor", _FakeExecutor)
    monkeypatch.setattr(
        proactive_value_check,
        "find_literal_mismatches",
        lambda *a, **k: called["literal"].append("x") or [],
    )
    monkeypatch.setattr(
        proactive_value_check,
        "find_numeric_scale_mismatches",
        lambda *a, **k: called["scale"].append("x") or [],
    )
    monkeypatch.setattr(
        proactive_value_check,
        "find_integer_division_mismatches",
        lambda *a, **k: called["division"].append("x") or [],
    )

    agent = proactive_value_check.ProactiveValueCheckAgent()
    # Bypass BaseAgent.__init__'s logger wiring concerns by calling execute directly.
    agent.execute(
        {
            "connectors": [],
            "path_state": {
                "sql_code": "SELECT 1 FROM t WHERE c = 'x'",
                "relevant_tables": [],
            },
        }
    )
    return called


def test_the_default_mode_runs_literal_and_division_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = _run_agent(monkeypatch, "values")

    assert called["literal"] == ["x"]
    assert called["division"] == ["x"]
    assert called["scale"] == []


def test_the_all_mode_adds_the_scale_heuristic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = _run_agent(monkeypatch, "all")

    assert called["literal"] == ["x"]
    assert called["division"] == ["x"]
    assert called["scale"] == ["x"]


def test_the_agent_itself_is_mode_agnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gating lives in ``CombinedPrecheckAgent``, which owns the graph node.

    This agent runs every finder it is given; the *mode* decides which finders
    the node calls it with, and whether the node exists at all. Pinned here so
    the split stays deliberate: putting the gate in both places is how the two
    drift and a check silently stops running.
    """
    called = _run_agent(monkeypatch, "off")

    assert called["literal"] == ["x"]
    assert called["division"] == ["x"]


def test_the_off_mode_keeps_the_node_out_of_the_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What ``off`` actually does: no precheck node, so nothing runs."""
    from auto_ontology.retrieval.text_to_sql.text_to_sql_graph import create_graph

    monkeypatch.setenv("DB_PROBE_PROACTIVE", "off")
    monkeypatch.delenv("DB_PROBE_JOIN_PATH_CHECK", raising=False)
    monkeypatch.delenv("DB_PROBE_JSONB_PATH_CHECK", raising=False)

    assert "precheck_combined" not in create_graph().nodes


def test_the_default_mode_puts_the_node_in_the_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from auto_ontology.retrieval.text_to_sql.text_to_sql_graph import create_graph

    monkeypatch.delenv("DB_PROBE_PROACTIVE", raising=False)
    monkeypatch.delenv("DB_PROBE_JOIN_PATH_CHECK", raising=False)
    monkeypatch.delenv("DB_PROBE_JSONB_PATH_CHECK", raising=False)

    assert "precheck_combined" in create_graph().nodes
