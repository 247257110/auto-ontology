# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the data-calendar anchoring of relative time phrases."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from auto_ontology.retrieval.text_to_sql.db_probe import date_window
from auto_ontology.retrieval.text_to_sql.time_anchor import (
    DataWindow,
    ResolvedTimeRange,
    format_time_anchor_section,
    resolve_time_ranges,
)

# Wide World Importers stops here; every case below anchors on it.
WWI = DataWindow(
    start=date(2013, 1, 1),
    end=date(2016, 5, 31),
    source="sales.invoices.invoicedate",
)


def _resolve(expression: str, canonical: str, window: DataWindow = WWI):
    return resolve_time_ranges(
        [{"expression": expression, "canonical": canonical}], window
    )


# ---------------------------------------------------------------------------
# Rolling windows
# ---------------------------------------------------------------------------


def test_rolling_days_are_inclusive_of_the_last_day_with_data() -> None:
    (item,) = _resolve("最近30天", "rolling_30d")

    # 30 days ending on 2016-05-31 inclusive, half-open on the right.
    assert item.start == date(2016, 5, 2)
    assert item.end == date(2016, 6, 1)
    assert item.predicate("invoicedate") == (
        "invoicedate >= '2016-05-02' AND invoicedate < '2016-06-01'"
    )


def test_rolling_12_months_does_not_reach_into_2017() -> None:
    """The bug this module exists for: 12 months back from a 2016 snapshot."""
    (item,) = _resolve("最近12个月", "rolling_12mo")

    assert item.start == date(2015, 6, 1)
    assert item.end == date(2016, 6, 1)


def test_rolling_months_step_by_calendar_month_not_thirty_days() -> None:
    february = DataWindow(start=date(2020, 1, 1), end=date(2020, 3, 31))

    (item,) = _resolve("最近1个月", "rolling_1mo", february)

    assert item.start == date(2020, 3, 1)


def test_rolling_weeks_and_years() -> None:
    (week,) = _resolve("最近4周", "rolling_4w")
    assert week.start == date(2016, 5, 4)
    assert week.end == date(2016, 6, 1)

    (year,) = _resolve("最近1年", "rolling_1y")
    assert year.start == date(2015, 6, 1)
    assert year.end == date(2016, 6, 1)


def test_a_month_end_anchor_clamps_rather_than_overflowing() -> None:
    """2016-03-31 minus one month is 2016-02-29, not 2016-03-03."""
    anchor = DataWindow(start=date(2016, 1, 1), end=date(2016, 3, 31))

    (item,) = _resolve("最近1个月", "rolling_1mo", anchor)

    assert item.start == date(2016, 3, 1)


# ---------------------------------------------------------------------------
# Calendar phrases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("canonical", "start", "end"),
    [
        ("previous_calendar_year", date(2015, 1, 1), date(2016, 1, 1)),
        ("current_calendar_year", date(2016, 1, 1), date(2016, 6, 1)),
        ("two_years_ago", date(2014, 1, 1), date(2015, 1, 1)),
        ("previous_calendar_month", date(2016, 4, 1), date(2016, 5, 1)),
        ("current_calendar_month", date(2016, 5, 1), date(2016, 6, 1)),
        ("previous_calendar_quarter", date(2016, 1, 1), date(2016, 4, 1)),
        ("current_calendar_quarter", date(2016, 4, 1), date(2016, 6, 1)),
        ("previous_calendar_week", date(2016, 5, 23), date(2016, 5, 30)),
        ("current_calendar_week", date(2016, 5, 30), date(2016, 6, 1)),
        ("previous_calendar_day", date(2016, 5, 30), date(2016, 5, 31)),
        ("current_calendar_day", date(2016, 5, 31), date(2016, 6, 1)),
    ],
)
def test_calendar_phrases_resolve_against_the_data_end(
    canonical: str, start: date, end: date
) -> None:
    (item,) = _resolve("某短语", canonical)

    assert (item.start, item.end) == (start, end)


def test_current_periods_stop_at_the_data_end_not_the_period_end() -> None:
    """A predicate reaching past the last row describes rows that do not exist."""
    (item,) = _resolve("本月", "current_calendar_month")

    assert item.end == date(2016, 6, 1)  # day after the last day with data


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_no_window_means_no_resolution() -> None:
    """Guessing ``now()`` is exactly what must not happen."""
    assert _resolve("最近12个月", "rolling_12mo", DataWindow(None, None)) == []
    assert resolve_time_ranges([{"canonical": "rolling_12mo"}], None) == []


def test_an_unknown_canonical_is_dropped_rather_than_guessed() -> None:
    assert _resolve("上个财年", "unknown") == []


def test_an_empty_or_zero_rolling_window_is_dropped() -> None:
    assert _resolve("最近0天", "rolling_0d") == []


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_the_section_states_the_coverage_and_the_predicate() -> None:
    (item,) = _resolve("最近12个月", "rolling_12mo")

    section = format_time_anchor_section([item], WWI)

    assert "2013-01-01" in section
    assert "2016-05-31" in section
    assert "sales.invoices.invoicedate" in section
    # The measured column becomes the predicate target, not a placeholder.
    assert "invoicedate >= '2015-06-01'" in section
    assert "invoicedate < '2016-06-01'" in section
    assert "CURRENT_DATE" in section  # names the failure being prevented


def test_the_section_names_the_real_date_column_when_known() -> None:
    item = ResolvedTimeRange(
        expression="最近30天",
        canonical="rolling_30d",
        start=date(2016, 5, 2),
        end=date(2016, 6, 1),
    )

    section = format_time_anchor_section([item], WWI, date_column="i.invoicedate")

    assert "i.invoicedate >= '2016-05-02'" in section


def test_a_stale_snapshot_is_called_out_explicitly() -> None:
    """The whole point: say the data's "now" is years behind today's."""
    (item,) = _resolve("本月", "current_calendar_month")

    section = format_time_anchor_section([item], WWI, today=date(2026, 1, 15))

    assert "today is 2026-01-15" in section
    assert "days after the last row" in section


def test_a_fresh_snapshot_gets_no_staleness_note() -> None:
    (item,) = _resolve("本月", "current_calendar_month")

    section = format_time_anchor_section([item], WWI, today=date(2016, 6, 1))

    assert "days after the last row" not in section


def test_no_section_without_anything_to_say() -> None:
    assert format_time_anchor_section([], WWI) == ""
    assert (
        format_time_anchor_section(
            [ResolvedTimeRange("x", "rolling_1d", None, None)],
            DataWindow(None, None),
        )
        == ""
    )


# ---------------------------------------------------------------------------
# Which columns get probed
# ---------------------------------------------------------------------------


def _table(columns: list[tuple[str, str]], name: str = "invoices") -> dict[str, Any]:
    return {
        "name": name,
        "schema_name": "sales",
        "database_name": "wideworldimporters",
        "columns": [{"name": c, "data_type": t} for c, t in columns],
    }


def test_only_date_columns_are_candidates() -> None:
    columns = date_window._candidate_columns(
        [_table([("invoiceid", "integer"), ("invoicedate", "timestamp")])], None
    )

    assert columns == [("sales.invoices", "invoicedate")]


def test_event_timestamps_outrank_incidental_dates() -> None:
    columns = date_window._candidate_columns(
        [
            _table(
                [
                    ("birth_date", "date"),
                    ("validto", "timestamp"),
                    ("invoicedate", "date"),
                ]
            )
        ],
        None,
    )

    assert [column for _table_name, column in columns] == [
        "invoicedate",
        "birth_date",
        "validto",
    ]


def test_the_probe_list_is_capped() -> None:
    columns = date_window._candidate_columns(
        [_table([(f"event_date_{i}", "date") for i in range(20)])], None
    )

    assert len(columns) == date_window._MAX_PROBE_COLUMNS


class _FakeConnector:
    database_name = "wideworldimporters"
    dialect = "postgres"

    def __init__(self, results: list[dict[str, Any]]) -> None:
        self._results = results
        self.statements: list[str] = []


@pytest.fixture()
def fake_probe(monkeypatch: pytest.MonkeyPatch):
    """Drive ``probe_data_window`` without a database."""

    def install(results: list[dict[str, Any]], *, on_run=None) -> _FakeConnector:
        connector = _FakeConnector(results)
        calls = {"n": 0}

        def fake_run(self, sql, purpose=""):
            connector.statements.append(sql)
            result = results[min(calls["n"], len(results) - 1)]
            calls["n"] += 1
            return result

        monkeypatch.setattr(
            "auto_ontology.retrieval.text_to_sql.db_probe.executor.ProbeExecutor.run",
            fake_run,
        )
        date_window._clear_cache()
        return connector

    yield install
    date_window._clear_cache()


def _ok(min_value, max_value) -> dict[str, Any]:
    return {"ok": True, "rows": [{"min_value": min_value, "max_value": max_value}]}


def test_probe_folds_every_column_into_one_window(fake_probe) -> None:
    connector = fake_probe(
        [
            _ok("2013-01-01", "2016-05-31"),
            _ok("2014-06-01", "2016-02-29"),
        ]
    )

    window = date_window.probe_data_window(
        connector, [_table([("invoicedate", "date")], "invoices")], cache=False
    )

    assert window is not None
    assert window.start == date(2013, 1, 1)
    assert window.end == date(2016, 5, 31)
    assert window.source == "sales.invoices.invoicedate"


def test_probe_accepts_a_timestamp_string(fake_probe) -> None:
    connector = fake_probe([_ok("2013-01-01 00:00:00", "2016-05-31 23:59:59.999999")])

    window = date_window.probe_data_window(
        connector, [_table([("invoicedate", "timestamp")])], cache=False
    )

    assert window is not None
    assert window.end == date(2016, 5, 31)


def test_a_failed_probe_yields_no_window_rather_than_a_guess(fake_probe) -> None:
    connector = fake_probe([{"ok": False, "rows": None, "error": "boom"}])

    assert (
        date_window.probe_data_window(
            connector, [_table([("invoicedate", "date")])], cache=False
        )
        is None
    )


def test_a_table_with_no_date_column_is_not_probed(fake_probe) -> None:
    connector = fake_probe([_ok("2013-01-01", "2016-05-31")])

    window = date_window.probe_data_window(
        connector,
        [_table([("invoiceid", "integer"), ("total", "numeric")])],
        cache=False,
    )

    assert window is None
    assert connector.statements == []


def test_the_window_is_cached_across_questions(fake_probe) -> None:
    connector = fake_probe([_ok("2013-01-01", "2016-05-31")])
    tables = [_table([("invoicedate", "date")])]

    first = date_window.probe_data_window(connector, tables)
    second = date_window.probe_data_window(connector, tables)

    assert first == second
    assert len(connector.statements) == 1
