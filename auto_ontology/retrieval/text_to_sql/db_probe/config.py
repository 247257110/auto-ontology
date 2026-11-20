# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Configuration for live DB probing.

All knobs are environment-driven so probing can be tuned without code changes.
The empty-result value-repair node is always wired in. The proactive
pre-execution value check is **on by default** in its ``values`` mode (literal
and case matching); ``DB_PROBE_PROACTIVE`` selects the mode and can turn it off
entirely. The join-path and JSONB-path checks remain opt-in.
"""

from __future__ import annotations

import os

_TRUTHY = {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


#: How much of the proactive value check runs before execution.
#:
#: ``off``     — nothing; wrong literals are only caught after an empty result.
#: ``values``  — literal/case matching plus the pure integer-division check.
#: ``all``     — adds the numeric-scale heuristic (% vs fraction).
PROACTIVE_OFF = "off"
PROACTIVE_VALUES = "values"
PROACTIVE_ALL = "all"

# Words that mean "run everything". The legacy boolean spellings stay here
# because that is exactly what they used to turn on.
_ALL_WORDS = {"all", "full", "true", "1", "yes", "on"}
_OFF_WORDS = {"off", "false", "0", "no", "none"}
_VALUES_WORDS = {"values", "value", "literals"}


def proactive_mode() -> str:
    """Which proactive value checks run, from ``DB_PROBE_PROACTIVE``.

    **On by default**, as ``values``. The previous arrangement defaulted the
    whole check to off and left an *empty result* as the only signal that a
    filter literal was wrong — which is the worst possible signal, because it
    also fires for a question whose answer is legitimately zero rows and never
    fires at all for a literal that matches data but not the data the user
    meant. Probing a handful of ``DISTINCT`` values before execution is cheap
    (bounded by ``DB_PROBE_MAX_CALLS``) and catches both.

    The numeric-scale heuristic stays out of the default: it infers a unit
    mismatch (a "60%" threshold against a 0–1 fraction) from an out-of-range
    literal, and a wrong guess there rejects a query that was right.

    ``true``/``1``/``yes`` keep their old meaning and select ``all``, so any
    deployment that had explicitly enabled the flag keeps exactly the behaviour
    it asked for.
    """
    raw = os.environ.get("DB_PROBE_PROACTIVE", "").strip().lower()
    if not raw:
        return PROACTIVE_VALUES
    if raw in _ALL_WORDS:
        return PROACTIVE_ALL
    if raw in _OFF_WORDS:
        return PROACTIVE_OFF
    if raw in _VALUES_WORDS:
        return PROACTIVE_VALUES
    return PROACTIVE_VALUES


def is_db_probe_proactive() -> bool:
    """Whether the pre-execution value check is wired into the graph at all.

    True unless ``DB_PROBE_PROACTIVE`` explicitly disables it — see
    :func:`proactive_mode` for what the default does and why.
    """
    return proactive_mode() != PROACTIVE_OFF


def is_db_probe_numeric_scale() -> bool:
    """Whether the numeric-scale (% vs fraction) heuristic runs.

    Opt-in via ``DB_PROBE_PROACTIVE=all``; the value/literal and
    integer-division checks do not need it.
    """
    return proactive_mode() == PROACTIVE_ALL


def is_db_probe_jsonb_path_check() -> bool:
    """Whether the pre-execution JSONB key-path check is wired in.

    Off by default: it runs a few cheap ``jsonb_object_keys`` probes on every
    query that navigates JSONB via ``->``/``->>``, to catch a hallucinated
    key/container that would otherwise silently return NULL instead of erroring.
    Higher per-question cost than the value check, and narrower (JSONB only).
    Opt-in via ``DB_PROBE_JSONB_PATH_CHECK``. Postgres-only regardless of this
    flag.
    """
    return os.environ.get("DB_PROBE_JSONB_PATH_CHECK", "").strip().lower() in _TRUTHY


def is_db_probe_join_path_check() -> bool:
    """Whether the pre-execution join-path check is wired in.

    Off by default: on the common case (join already correct) it's a handful of
    cheap existence checks against the ``SEMANTIC_FK`` graph per query; it only
    falls back to a live value-overlap probe (a couple of extra DB
    round-trips) when the graph has no edge for a predicate at all. Catches
    a hallucinated join between two plausible-looking identifier columns,
    which is syntactically/semantically valid SQL and so silently returns
    the wrong rows instead of erroring. Opt-in via
    ``DB_PROBE_JOIN_PATH_CHECK``.
    """
    return os.environ.get("DB_PROBE_JOIN_PATH_CHECK", "").strip().lower() in _TRUTHY


# Hard caps — keep Phase 1 cheap and always-on-safe.
# Total read-only queries allowed per question.
DB_PROBE_MAX_CALLS = _int_env("DB_PROBE_MAX_CALLS", 40)
# Max rows returned per probe (also the auto-appended LIMIT).
DB_PROBE_MAX_ROWS = _int_env("DB_PROBE_MAX_ROWS", 50)
# Per-probe wall-clock timeout (seconds).
DB_PROBE_TIMEOUT_S = _float_env("DB_PROBE_TIMEOUT_S", 5.0)
# Only probe the first N relevant tables.
DB_PROBE_MAX_TABLES = _int_env("DB_PROBE_MAX_TABLES", 8)
# Only probe the first N columns per table (per probe kind).
DB_PROBE_MAX_COLS_PER_TABLE = _int_env("DB_PROBE_MAX_COLS_PER_TABLE", 8)
# A DISTINCT probe is kept only if the column has <= this many distinct values.
DB_PROBE_LOW_CARD_THRESHOLD = _int_env("DB_PROBE_LOW_CARD_THRESHOLD", 20)


__all__ = [
    "is_db_probe_proactive",
    "is_db_probe_numeric_scale",
    "proactive_mode",
    "is_db_probe_jsonb_path_check",
    "is_db_probe_join_path_check",
    "PROACTIVE_OFF",
    "PROACTIVE_VALUES",
    "PROACTIVE_ALL",
    "DB_PROBE_MAX_CALLS",
    "DB_PROBE_MAX_ROWS",
    "DB_PROBE_TIMEOUT_S",
    "DB_PROBE_MAX_TABLES",
    "DB_PROBE_MAX_COLS_PER_TABLE",
    "DB_PROBE_LOW_CARD_THRESHOLD",
]
