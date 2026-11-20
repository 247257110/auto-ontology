# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Probe the calendar the connected data actually covers.

``time_anchor`` knows how to resolve a relative phrase once it has a window;
this finds the window. It runs at most a handful of ``SELECT MIN(c), MAX(c)``
probes per question, only when the question contains a relative time phrase,
and caches per (database, column set) so a conversation asks once.

Deliberately a live probe rather than a stored column statistic: the catalog has
no min/max fields, and adding them would mean a schema migration *and* a
reingest before any existing deployment could resolve a single date correctly.
A ``MIN``/``MAX`` off an indexed timestamp column is cheap, and the cache makes
repeat questions free.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import date, datetime
from typing import Any, Iterable, Sequence

from auto_ontology.retrieval.text_to_sql.db_probe.executor import ProbeExecutor
from auto_ontology.retrieval.text_to_sql.time_anchor import DataWindow
from auto_ontology.semantic.date_format import is_date_type

logger = logging.getLogger(__name__)

# How many date columns one question may probe. A MIN/MAX per column is cheap
# but not free on a wide warehouse, and the window is a property of the whole
# database — a couple of good columns describe it as well as twenty.
_MAX_PROBE_COLUMNS = 4

# Column names that look like an event timestamp. Preferred over an arbitrary
# date column because a `birth_date` or `validto` sentinel describes a person or
# a version window, not what the data *covers*.
_EVENT_NAME_RE = re.compile(
    r"(invoicedate|orderdate|occurred|created|_at$|timestamp|recorded|"
    r"transaction|signup|shipped|booking|payment)",
    re.IGNORECASE,
)

# Names whose max value is deliberately far from the data's coverage.
_SENTINEL_NAME_RE = re.compile(
    r"(validto|validfrom|expiry|expire|end_date|deleted|updated|modified)",
    re.IGNORECASE,
)

_CACHE_TTL_SECONDS = 900.0

_cache_lock = threading.Lock()
_cache: dict[tuple[str, tuple[str, ...]], tuple[float, DataWindow | None]] = {}


def _cache_key(connector: Any, columns: Sequence[tuple[str, str, str, str]]) -> tuple:
    database = str(getattr(connector, "database_name", "") or "")
    return (database, tuple(sorted(f"{t}.{c}" for _d, _s, t, c in columns)))


def _clear_cache() -> None:
    """Drop every cached window. Tests use this; nothing else needs it."""
    with _cache_lock:
        _cache.clear()


def _candidate_columns(
    tables: Iterable[dict],
    connector: Any,
) -> list[tuple[str, str]]:
    """``[(qualified_table, column)]`` worth probing, best-first, deduplicated.

    The qualified name comes from the connector's own ``qualify``, so a
    two-level engine (no separate schema) spells it the way it accepts.
    """
    qualify = getattr(connector, "qualify", None)
    scored: list[tuple[int, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for table in tables or []:
        if not isinstance(table, dict):
            continue
        name = str(table.get("name") or "")
        if not name:
            continue
        schema = str(table.get("schema_name") or "")
        qualified = (
            qualify(schema or None, name)
            if callable(qualify)
            else (f"{schema}.{name}" if schema else name)
        )
        for column in table.get("columns") or []:
            if not isinstance(column, dict):
                continue
            column_name = str(column.get("name") or "")
            if not column_name or not is_date_type(str(column.get("data_type") or "")):
                continue
            key = (qualified, column_name)
            if key in seen:
                continue
            seen.add(key)
            # Lower rank sorts first: an event timestamp beats a mere date
            # column, and a validity/sentinel column is a last resort.
            rank = 1
            if _EVENT_NAME_RE.search(column_name):
                rank = 0
            if _SENTINEL_NAME_RE.search(column_name):
                rank = 2
            scored.append((rank, qualified, column_name))

    scored.sort(key=lambda item: (item[0], item[1], item[2]))
    return [(qualified, column) for _rank, qualified, column in scored][
        :_MAX_PROBE_COLUMNS
    ]


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    # Accept an ISO timestamp or a bare date, which is what the connectors hand
    # back for date and timestamp columns alike.
    head = text.replace("T", " ").split(" ", 1)[0]
    try:
        return date.fromisoformat(head)
    except ValueError:
        return None


def probe_data_window(
    connector: Any,
    tables: Iterable[dict],
    *,
    cache: bool = True,
) -> DataWindow | None:
    """The inclusive date range *tables* cover, or ``None`` when unmeasurable.

    ``None`` is a normal answer: a schema with no date column, a probe that
    failed, or an unparseable value all mean "no window", and callers then keep
    their existing unanchored behaviour rather than anchoring on a guess.
    """
    if connector is None:
        return None

    columns = _candidate_columns(tables, connector)
    if not columns:
        logger.info("Time anchor: no date column among the relevant tables")
        return None

    entry = [
        (str(getattr(connector, "database_name", "")), "", t, c) for t, c in columns
    ]
    key = _cache_key(connector, entry)
    if cache:
        with _cache_lock:
            hit = _cache.get(key)
        if hit is not None and (time.monotonic() - hit[0]) < _CACHE_TTL_SECONDS:
            return hit[1]

    window = _probe(connector, columns)
    if cache:
        with _cache_lock:
            _cache[key] = (time.monotonic(), window)
    return window


def _probe(connector: Any, columns: Sequence[tuple[str, str]]) -> DataWindow | None:
    """Run the MIN/MAX probes and fold them into one window."""
    earliest: date | None = None
    latest: date | None = None
    latest_source = ""
    successes = 0

    with ProbeExecutor(connector) as executor:
        for qualified, column in columns:
            sql = (
                f"SELECT MIN({column}) AS min_value, MAX({column}) AS max_value "
                f"FROM {qualified}"
            )
            result = executor.run(sql, purpose=f"data window for {qualified}.{column}")
            if not result.get("ok") or not result.get("rows"):
                continue
            row = result["rows"][0]
            min_value = _as_date(row.get("min_value"))
            max_value = _as_date(row.get("max_value"))
            if min_value is None and max_value is None:
                continue
            successes += 1
            if min_value is not None and (earliest is None or min_value < earliest):
                earliest = min_value
            if max_value is not None and (latest is None or max_value > latest):
                latest = max_value
                latest_source = f"{qualified}.{column}"

    if successes == 0 or latest is None:
        logger.info("Time anchor: every date probe failed or returned nothing usable")
        return None

    window = DataWindow(start=earliest, end=latest, source=latest_source)
    logger.info(
        "Time anchor: data covers %s .. %s (from %s)",
        earliest.isoformat() if earliest else "-",
        latest.isoformat(),
        latest_source,
    )
    return window


__all__ = ["probe_data_window"]
