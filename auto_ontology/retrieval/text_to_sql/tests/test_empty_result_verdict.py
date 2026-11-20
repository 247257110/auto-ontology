# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Zero rows is not always a bug: tell a data fact from a wrong question.

``EmptyResultValueRepairAgent`` used to treat every empty result as a suspect
query and spend one regeneration on it. That is wrong in both directions: a
legitimately empty answer ("how many credit notes are there?" when the column is
all ``false``) got rewritten into some other question, and a value that exists
but not in the column being filtered still needed the retry.

The distinction drawn here is narrow on purpose — only a column whose *whole*
value set is small enough to list can prove that a value does not occur.
"""

from __future__ import annotations

from typing import Any


from auto_ontology.retrieval.text_to_sql.db_probe.literal_check import (
    EmptyVerdict,
    LiteralVerdict,
    classify_empty_result,
)


def _verdict(*literals: tuple[str, str, tuple[str, ...]]) -> EmptyVerdict:
    return EmptyVerdict(
        literals=tuple(
            LiteralVerdict(
                table="t", column=column, used=used, status=status, candidates=cands
            )
            for column, used, status, cands in (
                (column, used, status, cands)
                for column, used, status, cands in literals
            )
        )
    )


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


def test_a_value_outside_a_listed_value_set_is_a_data_fact() -> None:
    verdict = _verdict(("variant", "refund", "absent_enum", ("invoice",)))

    assert verdict.is_data_fact is True
    assert [item.used for item in verdict.absent_values()] == ["refund"]


def test_a_value_that_is_present_explains_nothing() -> None:
    """Every literal matched, so a join (or something else) dropped the rows."""
    verdict = _verdict(("variant", "invoice", "present", ()))

    assert verdict.is_data_fact is False


def test_a_near_miss_is_a_typo_not_a_data_fact() -> None:
    verdict = _verdict(("variant", "invoce", "near_miss", ()))

    assert verdict.is_data_fact is False


def test_absence_on_an_open_column_proves_nothing() -> None:
    """The value may simply live in another column or table — retry is right."""
    verdict = _verdict(("city", "Pennsylvania", "absent_open", ()))

    assert verdict.is_data_fact is False


def test_an_unresolved_probe_proves_nothing() -> None:
    verdict = _verdict(("variant", "x", "unresolved", ()))

    assert verdict.is_data_fact is False


def test_no_literal_at_all_proves_nothing() -> None:
    assert EmptyVerdict().is_data_fact is False


def test_one_open_column_disqualifies_the_whole_verdict() -> None:
    verdict = _verdict(
        ("variant", "refund", "absent_enum", ("invoice",)),
        ("city", "Nowhere", "absent_open", ()),
    )

    assert verdict.is_data_fact is False


def test_a_present_literal_beside_an_absent_one_still_counts() -> None:
    verdict = _verdict(
        ("variant", "invoice", "present", ()),
        ("region", "Mars", "absent_enum", ("North", "South")),
    )

    assert verdict.is_data_fact is True


# ---------------------------------------------------------------------------
# The explanation
# ---------------------------------------------------------------------------


def test_the_explanation_names_the_value_and_the_real_ones() -> None:
    verdict = _verdict(("iscreditnote", "true", "absent_enum", ("false",)))

    text = verdict.explain()

    assert "iscreditnote" in text
    assert "true" in text
    assert "false" in text  # the real value, so the user can see why
    assert "数据事实" in text  # says it is not an SQL error


def test_the_explanation_is_empty_when_there_is_nothing_to_say() -> None:
    assert _verdict(("c", "x", "present", ())).explain() == ""
    assert _verdict(("c", "x", "absent_open", ())).explain() == ""


def test_as_dict_reports_the_verdict_for_the_response_layer() -> None:
    fact = _verdict(("c", "x", "absent_enum", ("a",))).as_dict()
    assert fact["verdict"] == "data_fact"
    assert fact["explanation"]

    undiagnosed = _verdict(("c", "x", "missing", ())).as_dict()
    assert undiagnosed["verdict"] == "undiagnosed"
    assert undiagnosed["explanation"] == ""


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------


class _Rows(dict):
    """A probe result dict, shaped like ``ProbeExecutor.run``'s return."""

    def __init__(self, values: list[str]) -> None:
        super().__init__()
        self.update(
            {
                "ok": True,
                "rows": [{"value": v} for v in values],
                "row_count": len(values),
                "error": None,
            }
        )


class _ScriptedExecutor:
    """Answers DISTINCT probes from a script, and refuses anything else."""

    def __init__(self, script: dict[str, list[str]]) -> None:
        self.script = script
        self.statements: list[str] = []

    @property
    def budget_left(self) -> int:
        return 50

    def run(self, sql: str, purpose: str = "") -> dict[str, Any]:
        self.statements.append(sql)
        lowered = sql.lower()
        for column, values in self.script.items():
            if f"distinct {column}" in lowered or f".{column} " in lowered:
                return _Rows(values)
        return {"ok": False, "rows": None, "row_count": 0, "error": "no such column"}


# Deliberately not a near-miss of any listed value (difflib's cutoff is 0.6,
# and "credit_note" would be a near miss of "credit" — a typo, not a fact).
SQL_ONE_FILTER = "SELECT * FROM invoices WHERE variant = 'refund'"
SQL_TWO_FILTERS = "SELECT * FROM invoices WHERE variant = 'refund' AND region = 'Mars'"


def test_a_closed_value_set_hides_the_literal_and_says_so() -> None:
    executor = _ScriptedExecutor({"variant": ["invoice", "credit"]})

    verdict = classify_empty_result(executor, "postgres", SQL_ONE_FILTER)

    assert verdict.is_data_fact is True
    assert verdict.absent_values()[0].used == "refund"


def test_the_same_literal_against_an_open_column_is_not_decidable() -> None:
    """More than the cardinality threshold of values → no verdict."""
    many = [f"v{i}" for i in range(50)]
    executor = _ScriptedExecutor({"variant": many})

    verdict = classify_empty_result(executor, "postgres", SQL_ONE_FILTER)

    assert verdict.is_data_fact is False
    assert verdict.literals[0].status == "absent_open"


def test_one_open_column_blocks_the_verdict_across_the_whole_query() -> None:
    executor = _ScriptedExecutor(
        {"variant": ["invoice"], "region": [f"r{i}" for i in range(50)]}
    )

    verdict = classify_empty_result(executor, "postgres", SQL_TWO_FILTERS)

    assert verdict.is_data_fact is False


def test_a_query_with_no_string_filter_gets_no_verdict() -> None:
    executor = _ScriptedExecutor({})

    verdict = classify_empty_result(executor, "postgres", "SELECT COUNT(*) FROM t")

    assert verdict.literals == ()
    assert verdict.is_data_fact is False
    assert executor.statements == []


def test_unparseable_sql_gets_no_verdict() -> None:
    executor = _ScriptedExecutor({"variant": ["invoice"]})

    verdict = classify_empty_result(executor, "postgres", "NOT SQL AT ALL (((")

    assert verdict.is_data_fact is False
    assert executor.statements == []


def test_the_cache_is_shared_with_the_repair_pass() -> None:
    """Two callers, one probe per column — that is what the cache is for."""
    from auto_ontology.retrieval.text_to_sql.db_probe.literal_check import (
        find_literal_mismatches,
    )

    executor = _ScriptedExecutor({"variant": ["invoice", "credit"]})
    cache: dict[Any, Any] = {}

    mismatches = find_literal_mismatches(
        executor,
        "postgres",
        SQL_ONE_FILTER,
        result_was_empty=True,
        distinct_cache=cache,
    )
    classify_empty_result(executor, "postgres", SQL_ONE_FILTER, distinct_cache=cache)

    distinct_probes = [s for s in executor.statements if "DISTINCT" in s.upper()]
    assert mismatches == []
    assert len(distinct_probes) == 1
