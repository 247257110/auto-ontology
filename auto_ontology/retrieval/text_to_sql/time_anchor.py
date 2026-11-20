# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Resolve relative time phrases against the data's own calendar.

A warehouse is a snapshot: Wide World Importers stops at 2016-05-31, a churn
dataset at 2025-06-30. "最近12个月" asked against it today means *the twelve
months ending at the last row*, not the twelve months before `now()` — the
latter matches nothing and comes back as an empty result that reads as a
data-quality problem rather than a date problem.

:func:`resolve_time_ranges` turns the labels ``semantic_ir`` produces
(``rolling_12mo``, ``previous_calendar_year``, …) into concrete, half-open
date intervals anchored on :class:`DataWindow`, and
:func:`format_time_anchor_section` renders them as the predicates the SQL
generator should use. Everything here is pure: the window is probed elsewhere
(``db_probe.date_window``) so this stays testable without a database.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable, Sequence

# Rolling windows expressed as a (unit, count) pair. Months and years are
# stepped with `_shift_months` so the day-of-month cannot drift the way
# repeated 30-day subtractions would.
_ROLLING_RE = re.compile(r"^rolling_(\d+)(d|w|mo|y)$")

_CALENDAR_UNITS = {
    "year": "y",
    "quarter": "q",
    "month": "mo",
    "week": "w",
    "day": "d",
}

# Past this many days, the data's end and today are different "now"s and the
# prompt says so explicitly. A week's lag is ordinary ETL; a year's is not.
_STALE_GAP_DAYS = 7


@dataclass(frozen=True)
class DataWindow:
    """The calendar the connected data actually covers, inclusive on both ends.

    ``end`` is the anchor every relative phrase resolves against: it is the last
    moment the data describes, which is the only sensible stand-in for "now"
    when the snapshot is old. ``source`` names what was measured, so a prompt
    can say *why* it is anchoring there instead of asserting it.
    """

    start: date | None
    end: date | None
    source: str = ""

    @property
    def usable(self) -> bool:
        return self.end is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "source": self.source,
        }


@dataclass(frozen=True)
class ResolvedTimeRange:
    """One relative phrase turned into a half-open interval on the data calendar."""

    expression: str
    canonical: str
    #: Inclusive first day, ``None`` when the phrase has no lower bound.
    start: date | None
    #: Exclusive last day — the interval is ``[start, end)``, which is what
    #: keeps a month boundary from double-counting the last day.
    end: date | None

    def predicate(self, column: str) -> str:
        """The SQL predicate this range implies for *column*."""
        parts: list[str] = []
        if self.start is not None:
            parts.append(f"{column} >= '{self.start.isoformat()}'")
        if self.end is not None:
            parts.append(f"{column} < '{self.end.isoformat()}'")
        return " AND ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "expression": self.expression,
            "canonical": self.canonical,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
        }


def _shift_months(anchor: date, months: int) -> date:
    """Move *anchor* by whole months, clamping the day to the target month."""
    month_index = anchor.month - 1 + months
    year = anchor.year + month_index // 12
    month = month_index % 12 + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _month_start(anchor: date, offset: int = 0) -> date:
    return _shift_months(anchor.replace(day=1), offset)


def _quarter_start(anchor: date, offset: int = 0) -> date:
    first_month = (anchor.month - 1) // 3 * 3 + 1
    return _shift_months(date(anchor.year, first_month, 1), offset * 3)


def _week_start(anchor: date) -> date:
    """Monday of the week containing *anchor* (ISO weeks)."""
    return anchor - timedelta(days=anchor.weekday())


def _resolve_calendar(canonical: str, anchor: date) -> tuple[date, date] | None:
    """``[start, end)`` for a calendar phrase, relative to *anchor*.

    ``current_*`` ends the day after *anchor* rather than at the natural end of
    the period: the data stops where it stops, and a predicate reaching past it
    would be honest SQL describing rows that do not exist.
    """
    day_after = anchor + timedelta(days=1)

    if canonical == "current_calendar_year":
        return date(anchor.year, 1, 1), day_after
    if canonical == "previous_calendar_year":
        return date(anchor.year - 1, 1, 1), date(anchor.year, 1, 1)
    if canonical == "two_years_ago":
        return date(anchor.year - 2, 1, 1), date(anchor.year - 1, 1, 1)

    if canonical == "current_calendar_month":
        return _month_start(anchor), day_after
    if canonical == "previous_calendar_month":
        return _month_start(anchor, -1), _month_start(anchor)
    if canonical == "next_calendar_month":
        # The data has no future rows, but resolving the phrase keeps the
        # interval explicit rather than leaving the model to invent one.
        return _month_start(anchor, 1), _month_start(anchor, 2)

    if canonical == "current_calendar_quarter":
        return _quarter_start(anchor), day_after
    if canonical == "previous_calendar_quarter":
        return _quarter_start(anchor, -1), _quarter_start(anchor)
    if canonical == "next_calendar_quarter":
        return _quarter_start(anchor, 1), _quarter_start(anchor, 2)

    if canonical == "current_calendar_week":
        return _week_start(anchor), day_after
    if canonical == "previous_calendar_week":
        return _week_start(anchor) - timedelta(days=7), _week_start(anchor)
    if canonical == "next_calendar_week":
        start = _week_start(anchor) + timedelta(days=7)
        return start, start + timedelta(days=7)

    if canonical == "current_calendar_day":
        return anchor, day_after
    if canonical == "previous_calendar_day":
        return anchor - timedelta(days=1), anchor
    if canonical == "next_calendar_day":
        return anchor + timedelta(days=1), anchor + timedelta(days=2)

    return None


def _resolve_rolling(canonical: str, anchor: date) -> tuple[date, date] | None:
    """``[start, end)`` for ``rolling_N<unit>``, counting back from *anchor*.

    The window is inclusive of *anchor*: "最近30天" ends on the last day with
    data, so the interval is ``[anchor - 29d, anchor + 1d)`` — 30 days, not 31.
    """
    match = _ROLLING_RE.match(canonical or "")
    if not match:
        return None
    count = int(match.group(1))
    unit = match.group(2)
    if count <= 0:
        return None

    day_after = anchor + timedelta(days=1)
    if unit == "d":
        return anchor - timedelta(days=count - 1), day_after
    if unit == "w":
        return anchor - timedelta(days=count * 7 - 1), day_after
    if unit == "mo":
        return _shift_months(anchor, -count) + timedelta(days=1), day_after
    if unit == "y":
        return _shift_months(anchor, -12 * count) + timedelta(days=1), day_after
    return None


def resolve_time_ranges(
    time_ranges: Sequence[dict[str, Any]] | None,
    window: DataWindow | None,
) -> list[ResolvedTimeRange]:
    """Turn IR time phrases into concrete intervals on :class:`DataWindow`.

    Returns ``[]`` when there is no window to anchor on, which is the honest
    answer: guessing ``now()`` is precisely the behaviour this module exists to
    replace, and a caller that gets nothing back falls through to the previous
    (unanchored) behaviour instead of a confidently wrong date.
    """
    if not window or not window.usable or not time_ranges:
        return []
    anchor = window.end
    assert anchor is not None  # narrowed by `usable`

    resolved: list[ResolvedTimeRange] = []
    for item in time_ranges:
        canonical = str(item.get("canonical") or "")
        expression = str(item.get("expression") or "")
        bounds = _resolve_rolling(canonical, anchor) or _resolve_calendar(
            canonical, anchor
        )
        if bounds is None:
            continue
        start, end = bounds
        resolved.append(
            ResolvedTimeRange(
                expression=expression,
                canonical=canonical,
                start=start,
                end=end,
            )
        )
    return resolved


def format_time_anchor_section(
    resolved: Iterable[ResolvedTimeRange] | None,
    window: DataWindow | None,
    *,
    date_column: str | None = None,
    today: date | None = None,
) -> str:
    """Render the resolved intervals as the predicates the generator should use.

    Explicit literals rather than prose: the failure this prevents is a model
    writing ``CURRENT_DATE - INTERVAL '12 months'``, which is syntactically
    fine and silently returns nothing.
    """
    items = list(resolved or [])
    if not items or not window or not window.usable:
        return ""

    today = today or date.today()
    end = window.end.isoformat()  # type: ignore[union-attr]
    coverage = f"{window.start.isoformat()} through {end}" if window.start else end
    source = f" (latest value in `{window.source}`)" if window.source else ""

    lines = [
        "## Time anchoring",
        f"The connected data covers {coverage}{source}.",
        "Resolve every relative time phrase against that boundary, not against "
        "today's date. A window written relative to CURRENT_DATE/CURRENT_TIMESTAMP "
        "returns no rows on a snapshot that stopped earlier.",
    ]

    gap_days = _gap_days(window, today)
    if gap_days and gap_days > _STALE_GAP_DAYS:
        lines.append(
            f"Note: today is {today.isoformat()}, {gap_days} days after the last "
            'row. "本月"/"去年"/"最近N天" therefore describe the end of '
            "the data, not the present."
        )

    column = date_column or _default_date_column(window)
    for item in items:
        lines.append(
            f"- `{item.expression}` ({item.canonical}) means "
            f"{item.start.isoformat() if item.start else '-'} .. "
            f"{item.end.isoformat() if item.end else '-'} (exclusive): "
            f"`{item.predicate(column)}`"
        )
    return "\n".join(lines) + "\n"


def _default_date_column(window: DataWindow) -> str:
    """The column the window was measured from, as a predicate target.

    Better than a placeholder: the model has that column in its table context,
    so a concrete predicate is one it can copy rather than one it must map.
    """
    if window.source:
        return window.source.rsplit(".", 1)[-1]
    return "<date column>"


def _gap_days(window: DataWindow, today: date) -> int | None:
    """How far *today* is past the last row, or ``None`` when unmeasurable."""
    if window.end is None:
        return None
    return (today - window.end).days


__all__ = [
    "DataWindow",
    "ResolvedTimeRange",
    "format_time_anchor_section",
    "resolve_time_ranges",
]
