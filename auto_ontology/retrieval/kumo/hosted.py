# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Run predictions against the hosted Kumo Relational API.

The self-hosted Universal TFM NIM accepts a PQL query and resolves it
server-side. The hosted endpoint on ``build.nvidia.com`` accepts only the
*resolved* task: a task kind, a target column, the entity table to score, an
anchor-time column, and ``context``/``predict`` frames already sliced per
instance. This module supplies the missing translation:

    PQL → TaskSpec (pql_compile) → per-instance frames → HTTP → DataFrame

Two things make that translation safe rather than approximate:

* **Rolling anchors.** Context instances are cut at several historical anchors
  and labelled from the window that follows each one, so the model sees the
  target occur rather than inferring it from one static snapshot. The predict
  instances sit at the latest anchor, after every context window has closed.
* **No look-ahead.** A related row enters an instance only when its timestamp is
  at or before that instance's anchor, so a context label can never be read off
  its own future.

A shape the endpoint cannot express is refused with the repairable message from
:mod:`auto_ontology.retrieval.kumo.pql_compile` instead of being sent as a
different question.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import pandas as pd

from auto_ontology.retrieval.kumo.pql_compile import (
    BINARY,
    LINK,
    REGRESSION,
    PqlCompileError,
    TaskSpec,
    Window,
    compile_pql,
    describe,
)

logger = logging.getLogger(__name__)

DEFAULT_HOSTED_URL = (
    "https://ai.api.nvidia.com/v1/structured-data/nvidia/kumo-relational/predictions"
)
DEFAULT_TIMEOUT = 300.0
DEFAULT_HISTORY_ANCHORS = 8
DEFAULT_MAX_INSTANCES = 2_000
DEFAULT_MAX_RELATED_ROWS = 400_000
# The endpoint rejects a body over 31,457,280 bytes with a 413; a body that size
# through a local proxy dies as a broken pipe instead. Stay under both.
DEFAULT_MAX_PAYLOAD_BYTES = 28 * 1024 * 1024

# How far behind each anchor the related rows may reach. Zero means "all of it",
# which is the honest default for a warehouse whose history is short. Long
# histories mostly add upload time: a customer's behaviour two years ago says
# little about the next 90 days.
DEFAULT_LOOKBACK_DAYS = 0

# Instance bookkeeping names. ``instance_id`` and ``anchor_time`` are not ours to
# choose: the endpoint requires ``schema.instance_table.primary_key`` to be exactly
# ``instance_id``, and the task points at the anchor column by name. Real columns
# that collide are renamed on the way in (see ``_safe_columns``).
_INSTANCE_ID = "instance_id"
_ANCHOR = "anchor_time"
_LABEL = "label"
_ENTITY_KEY = "__kumo_entity_key"
_ROW = "__kumo_row"
_RESERVED = frozenset({_INSTANCE_ID, _ANCHOR, _LABEL, _ENTITY_KEY})

# Aggregations whose empty result is still a number rather than a missing value.
_ZERO_ON_EMPTY = frozenset({"COUNT", "SUM"})

# How far up a parent chain to look for the timestamp a detail table lacks.
_MAX_TIME_HOPS = 3


def hosted_url() -> str | None:
    """The hosted endpoint this deployment talks to, or ``None`` when unset.

    ``KUMO_HOSTED_API_URL`` is the switch for this path. The literal value
    ``public`` selects the Prototype endpoint on ``build.nvidia.com``, whose URL
    is long enough to be worth spelling once.
    """
    raw = (os.environ.get("KUMO_HOSTED_API_URL") or "").strip()
    if not raw:
        return None
    return DEFAULT_HOSTED_URL if raw.lower() == "public" else raw


def hosted_enabled() -> bool:
    return hosted_url() is not None


@dataclass(frozen=True)
class HostedConfig:
    """Endpoints and caps for one hosted run."""

    url: str
    api_key: str | None
    timeout: float
    history_anchors: int
    max_instances: int
    max_related_rows: int
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES
    lookback_days: int = DEFAULT_LOOKBACK_DAYS

    @classmethod
    def from_env(cls) -> "HostedConfig | None":
        url = hosted_url()
        if url is None:
            return None
        return cls(
            url=url,
            api_key=(os.environ.get("KUMO_HOSTED_API_KEY") or "").strip() or None,
            timeout=_float_env("KUMO_HOSTED_TIMEOUT", DEFAULT_TIMEOUT),
            history_anchors=max(
                1, _int_env("KUMO_HISTORY_ANCHORS", DEFAULT_HISTORY_ANCHORS)
            ),
            max_instances=max(1, _int_env("KUMO_MAX_INSTANCES", DEFAULT_MAX_INSTANCES)),
            max_related_rows=max(
                1, _int_env("KUMO_MAX_RELATED_ROWS", DEFAULT_MAX_RELATED_ROWS)
            ),
            max_payload_bytes=max(
                1, _int_env("KUMO_MAX_PAYLOAD_BYTES", DEFAULT_MAX_PAYLOAD_BYTES)
            ),
            lookback_days=max(0, _int_env("KUMO_LOOKBACK_DAYS", DEFAULT_LOOKBACK_DAYS)),
        )


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Ignoring non-numeric %s=%r", name, raw)
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Ignoring non-numeric %s=%r", name, raw)
        return default


# ---------------------------------------------------------------------------
# Window arithmetic
# ---------------------------------------------------------------------------


def _shift(anchor: pd.Timestamp, amount: int, unit: str) -> pd.Timestamp:
    """Move ``anchor`` by ``amount`` of ``unit``.

    Months use a calendar offset rather than a fixed number of days, so a
    ``(0, 1, months]`` window lands on the same day next month — what a reader
    of the PQL means by it.
    """
    if unit in ("month", "months"):
        return anchor + pd.DateOffset(months=int(amount))
    scales = {
        "week": "W",
        "weeks": "W",
        "day": "D",
        "days": "D",
        "hour": "h",
        "hours": "h",
        "minute": "m",
        "minutes": "m",
        "second": "s",
        "seconds": "s",
    }
    return anchor + pd.to_timedelta(int(amount), unit=scales.get(unit, "s"))


def _window_width(window: Window) -> pd.Timedelta:
    base = pd.Timestamp("2001-01-01")
    return _shift(base, window.end, window.unit) - _shift(
        base, window.start, window.unit
    )


def _as_timestamp(value: Any) -> str | None:
    """Timestamp → RFC3339 with Z, the only form the endpoint accepts."""
    if value is None:
        return None
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if stamp is pd.NaT or pd.isna(stamp):
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert("UTC").tz_localize(None)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Scalar and dtype conversion
# ---------------------------------------------------------------------------


def _scalar(value: Any) -> Any:
    """Convert a pandas cell into something ``json`` can encode without NaN."""
    if value is None:
        return None
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, Decimal):
        # Postgres ``numeric`` arrives as Decimal even though pandas types the
        # column as object. Send a real number so it matches a `numerical`
        # declaration instead of leaking a quoted string into the frame.
        return None if value.is_nan() or value.is_infinite() else float(value)
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (pd.Timestamp, datetime)):
        return _as_timestamp(value)
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _scalar(item())
        except (TypeError, ValueError):
            pass
    return str(value)


def _is_decimal_series(series: pd.Series) -> bool:
    """True when an object column holds decimals the warehouse left untyped.

    Postgres ``numeric`` arrives as :class:`decimal.Decimal` objects, which
    pandas types as ``object``. Read as a string, a measure looks like free
    text: it would be declared categorical, dropped once it is wide, and every
    value would reach the endpoint quoted. It is a number, so say so.
    """
    if series.dtype != object:
        return False
    values = series.dropna()
    if values.empty:
        return False
    return all(isinstance(value, Decimal) for value in values)


def _dtype_of(series: pd.Series) -> tuple[str, str]:
    dtype = series.dtype
    if pd.api.types.is_bool_dtype(dtype):
        return "bool", "categorical"
    if pd.api.types.is_integer_dtype(dtype):
        return "int64", "numerical"
    if pd.api.types.is_float_dtype(dtype):
        return "float64", "numerical"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "timestamp[us]", "timestamp"
    if _is_decimal_series(series):
        return "float64", "numerical"
    return "string", "categorical"


# The endpoint rejects a categorical column whose cardinality exceeds this.
_MAX_CATEGORICAL = 10_000

# ``422 {"code":"UNSUPPORTED_SHAPE","detail":"categorical cardinality 12960
# exceeds limit 10000."}`` — the shape is too wide, but it is fixable by sending
# fewer columns, which is what :meth:`HostedKumoModel._repair_shape` does.
_CARDINALITY_RE = re.compile(
    r"categorical cardinality (\d+) exceeds limit (\d+)", re.IGNORECASE
)

# Hosted validation is stricter than the local graph for a few source-specific
# string columns.  The endpoint names the offending column, which lets us drop
# an optional descriptive feature while preserving keys and relationships.
_SEMANTIC_COLUMN_RE = re.compile(
    r"column\s+['\"]([^'\"]+)['\"]\s+declares\s+semantic",
    re.IGNORECASE,
)

_NUMERIC_DTYPES = frozenset({"int64", "float64"})

# The most ``(entity, anchor)`` rows the endpoint accepts in the context table:
# ``422 UNSUPPORTED_SHAPE: N_train 12960 exceeds limit 10000``.
_MAX_CONTEXT_INSTANCES = 10_000

# The most entities the endpoint will score in one request:
# ``422 UNSUPPORTED_SHAPE: predict.instance_table.rows has 1800; maximum is 1000``.
_MAX_PREDICT_INSTANCES = 1_000


def _cardinality(series: pd.Series) -> int:
    """Distinct non-null values. Only called for columns about to be categorical."""
    return int(series.nunique(dropna=True))


def _column_spec(
    series: pd.Series, declared: str | None, *, where: str | None = None
) -> dict[str, Any] | None:
    """The endpoint's declaration for one column, or ``None`` if it cannot be sent.

    ``None`` is a string column too wide to be a category. The local engine
    calls that column ``text``, but the hosted SDM reads no ``text`` semantic
    type — declaring it is refused outright with ``422 UNSUPPORTED_DTYPE`` —
    and categorical stops at :data:`_MAX_CATEGORICAL`. Such a column is a
    free-text feature the model could not have used as a category anyway, so
    the caller leaves it out of the payload (see
    :meth:`HostedKumoModel._unreadable_columns`) rather than fail the question.
    """
    dtype, stype = _dtype_of(series)
    if declared:
        stype = declared
        if declared == "timestamp":
            dtype = "timestamp[us]"
        elif declared == "numerical" and dtype == "string":
            dtype = "float64"
    if stype.casefold() == "text":
        # A semantic layer may declare `text` directly, and wide strings
        # resolve to it locally. The hosted endpoint has no such type, so send
        # what the column can be there: a measure when numeric, a category when
        # the endpoint can encode it (checked below).
        stype = "numerical" if dtype in _NUMERIC_DTYPES else "categorical"
    if stype == "categorical":
        distinct = _cardinality(series)
        if distinct > _MAX_CATEGORICAL:
            # Ask for what the column actually is, not for a category it cannot
            # be within the endpoint's ceiling. A numeric column — an id or a
            # measure the semantic layer called a dimension — is `numerical`.
            # A string has no fallback: `text` is not a type the endpoint
            # reads, so it is left out of the payload.
            if dtype not in _NUMERIC_DTYPES:
                return None
            stype = "numerical"
            if where:
                logger.info(
                    "kumo: %s has %d distinct values (%s) — declaring it %s rather than "
                    "categorical, which the endpoint caps at %d",
                    where,
                    distinct,
                    dtype,
                    stype,
                    _MAX_CATEGORICAL,
                )
    spec: dict[str, Any] = {"dtype": dtype, "stype": stype}
    if stype == "ID":
        spec["nullable"] = False
    return spec


# ---------------------------------------------------------------------------
# Graph metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TableMeta:
    """What the materialiser needs to know about one graph table."""

    name: str
    frame: pd.DataFrame
    primary_keys: tuple[str, ...]
    time_column: str | None


class HostedKumoModel:
    """A ``KumoModel``-shaped seam over the hosted endpoint.

    Satisfies the two methods ``pql_gen`` calls (:meth:`validate_pql` and
    :meth:`predict`), so generation and repair are unchanged: a query the hosted
    path cannot express raises the same ``ValueError`` the cheap local parse did.
    """

    def __init__(
        self,
        graph: Any,
        frames: dict[str, pd.DataFrame],
        config: HostedConfig,
        *,
        col_stypes: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self._graph = graph
        self._frames = frames
        self._config = config
        self._col_stypes = col_stypes or {}
        self._tables = self._collect_tables()
        self._links = self._collect_links()
        self._time_cache: dict[str, pd.Series | None] = {}
        self.last_spec: TaskSpec | None = None

    # -- the model seam -----------------------------------------------------

    def validate_pql(self, query: str) -> TaskSpec:
        """Compile the PQL — the hosted path's stand-in for the cheap parse."""
        spec = compile_pql(query, col_stypes=self._col_stypes)
        if spec.kind == LINK:
            raise PqlCompileError(
                "Recommendation targets (LIST_DISTINCT ... RANK TOP) need a two-entity graph the "
                "hosted endpoint builds differently, so they are not available here. Ask a "
                "classification question instead — e.g. `COUNT(<items>.*, 0, 90, days) > 0` for "
                '"which customers are likely to buy something"; the probabilities rank them.'
            )
        if spec.forecast:
            raise PqlCompileError(
                "A multi-period FORECAST is not available against the hosted endpoint. Ask for one "
                "future period instead: a plain aggregation over `(0, N, days/weeks/months)` gives "
                "the expected total for that window."
            )
        return spec

    def predict(
        self,
        query: str,
        indices: list[Any] | None = None,
        *,
        run_mode: str = "fast",
        num_retries: int = 1,
        anchor_time: Any = None,
        **kwargs: Any,
    ) -> pd.DataFrame:
        """Materialise the task, send it, and return a prediction frame.

        The frame speaks the vocabulary the local engine used (``ENTITY``,
        ``True_PROB``, ``*_PRED``), so ranking, preview and formatting stay
        shared between the two backends.
        """
        spec = self.validate_pql(query)
        self.last_spec = spec
        payload, plan = self._payload(spec, indices=indices, anchor_time=anchor_time)
        started = time.perf_counter()
        response = self._predict_response(payload)
        logger.info(
            "kumo-hosted: %s answered in %.2fs (context=%d, predict=%d instances, %d related rows)",
            describe(spec),
            time.perf_counter() - started,
            plan["context_instances"],
            plan["predict_instances"],
            plan["related_rows"],
        )
        return self._to_frame(spec, response, plan)

    def _predict_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send the request, narrowing its shape once if the endpoint says it is too wide.

        ``422 UNSUPPORTED_SHAPE: categorical cardinality N exceeds limit M`` is
        deterministic — the same body fails forever — but it is also repairable:
        the request is wider than the endpoint will encode, not wrong. Dropping
        the columns that carry the most distinct values keeps the question, the
        entities and every relationship intact, and gives up only columns the
        model could not have used as categories anyway. Each drop is logged, so
        the narrowing is visible rather than silent, and the schema and the rows
        are always edited together.
        """
        try:
            return self._post(payload)
        except RuntimeError as error:
            match = _CARDINALITY_RE.search(str(error))
            if match is None:
                semantic = _SEMANTIC_COLUMN_RE.search(str(error))
                if semantic is None:
                    raise
                column = semantic.group(1)
                targets = {
                    (table, column)
                    for table, declaration in (
                        (payload.get("schema") or {}).get("related_tables") or {}
                    ).items()
                    if column in (declaration.get("columns") or {})
                }
                targets -= _joined_columns(payload)
                if not targets:
                    raise
                logger.warning(
                    "kumo: hosted validation rejected optional column %s; retrying without it",
                    ", ".join(sorted(f"{table}.{name}" for table, name in targets)),
                )
                _drop_columns(payload, targets)
                return self._post(payload)
            limit = int(match.group(2))
            dropped_any = False
            for dropped in self._repair_steps(payload, limit):
                logger.warning(
                    "kumo: the endpoint refused this shape (%s); widest columns were %s; "
                    "retrying without %s",
                    match.group(0),
                    _widest_columns(payload),
                    ", ".join(sorted(f"{table}.{column}" for table, column in dropped)),
                )
                _drop_columns(payload, dropped)
                dropped_any = True
                try:
                    return self._post(payload)
                except RuntimeError as again:
                    if _CARDINALITY_RE.search(str(again)) is None:
                        raise
                    error = again
            if not dropped_any:
                raise
            raise RuntimeError(
                "hosted prediction failed: the request stays too wide for the endpoint even "
                f"after dropping its widest columns ({error})"
            ) from error

    def _repair_steps(
        self, payload: dict[str, Any], limit: int
    ) -> list[set[tuple[str, str]]]:
        """Which columns to drop, as cumulative batches, least loss first.

        A categorical column over the limit is a feature that cannot be encoded
        as a category; an ``ID`` column of a *related* table over the limit is
        that table's own row identity, which the model does not need to
        recognise a row. Both are dropped only when no relationship joins on
        them.

        ``text`` remains in the first batch defensively: the payload no longer
        sends it (the hosted SDM does not read it, see :func:`_column_spec`),
        but if any path ever does, it is still a column too wide to be a
        category.
        """
        report = _shape_report(payload)
        related = set((payload.get("schema") or {}).get("related_tables") or {})
        joined = _joined_columns(payload)
        wide = {
            (table, column)
            for table, columns in report.items()
            if table in related
            for column, (stype, distinct) in columns.items()
            if stype in ("categorical", "text")
            and distinct > limit
            and (table, column) not in joined
        }
        identities = {
            (table, column)
            for table, columns in report.items()
            if table in related
            for column, (stype, distinct) in columns.items()
            if stype == "ID" and distinct > limit and (table, column) not in joined
        }
        steps: list[set[tuple[str, str]]] = []
        seen: set[frozenset[tuple[str, str]]] = set()
        for step in (wide, wide | identities):
            key = frozenset(step)
            if step and key not in seen:
                seen.add(key)
                steps.append(step)
        return steps

    # -- graph access -------------------------------------------------------

    def _collect_tables(self) -> dict[str, TableMeta]:
        by_frame = {str(k).casefold(): k for k in self._frames}
        metas: dict[str, TableMeta] = {}
        for name, table in getattr(self._graph, "tables", {}).items():
            frame_key = by_frame.get(str(name).casefold())
            if frame_key is None:
                frame_key = by_frame.get(str(name).split(".")[-1].casefold())
            if frame_key is None:
                continue
            frame = self._frames[frame_key]
            metas[str(name).casefold()] = TableMeta(
                name=str(name),
                frame=frame,
                primary_keys=tuple(self._primary_keys(table, frame)),
                time_column=self._time_column(table, frame),
            )
        return metas

    @staticmethod
    def _primary_keys(table: Any, frame: pd.DataFrame) -> list[str]:
        declared = tuple(getattr(table, "primary_key_columns", ()) or ())
        candidates = [str(c) for c in declared]
        if not candidates:
            single = getattr(getattr(table, "primary_key", None), "name", None)
            if single:
                candidates = [str(single)]
        return [
            c
            for c in candidates
            if c in frame.columns and not c.startswith("__kumo_key")
        ]

    @staticmethod
    def _time_column(table: Any, frame: pd.DataFrame) -> str | None:
        declared = getattr(getattr(table, "time_column", None), "name", None)
        if declared and declared in frame.columns:
            return str(declared)
        return None

    def _collect_links(self) -> list[tuple[str, str, str]]:
        links: list[tuple[str, str, str]] = []
        for edge in getattr(self._graph, "edges", []):
            fkey = str(edge.fkey)
            if fkey.startswith("__kumo_key"):
                continue
            links.append(
                (str(edge.src_table).casefold(), fkey, str(edge.dst_table).casefold())
            )
        return links

    def _meta(self, name: str) -> TableMeta | None:
        folded = name.casefold()
        return self._tables.get(folded) or self._tables.get(folded.split(".")[-1])

    def _table_time(self, meta: TableMeta) -> pd.Series | None:
        """When each row of ``meta`` happened, following a key to a dated parent.

        Detail rows routinely carry no timestamp of their own — an invoice line's
        date lives on its invoice. Reading the parent's date keeps such a table
        usable as an event table, and, just as importantly, keeps the anchor cut
        honest: without a time, every row would look eternally current.
        """
        key = meta.name.casefold()
        if key in self._time_cache:
            return self._time_cache[key]
        self._time_cache[key] = None  # breaks a cyclic parent chain
        resolved = self._resolve_time(meta, {key}, 0)
        self._time_cache[key] = resolved
        return resolved

    def _resolve_time(
        self, meta: TableMeta, seen: set[str], depth: int
    ) -> pd.Series | None:
        if meta.time_column is not None and meta.time_column in meta.frame.columns:
            times = meta.frame[meta.time_column]
            if not pd.api.types.is_datetime64_any_dtype(times):
                times = pd.to_datetime(times, errors="coerce")
            return pd.Series(times.to_numpy(), index=range(len(meta.frame)))
        if depth >= _MAX_TIME_HOPS:
            return None
        for src, fkey, dst in self._links:
            if src == meta.name.casefold():
                neighbour = self._meta(dst)
                left_column = fkey
                right_column = (
                    neighbour.primary_keys[0]
                    if neighbour is not None and len(neighbour.primary_keys) == 1
                    else None
                )
            elif dst == meta.name.casefold():
                neighbour = self._meta(src)
                right_column = fkey
                left_column = (
                    meta.primary_keys[0] if len(meta.primary_keys) == 1 else None
                )
            else:
                continue
            if neighbour is None or not left_column or not right_column:
                continue
            if (
                left_column not in meta.frame.columns
                or right_column not in neighbour.frame.columns
            ):
                continue
            neighbour_key = neighbour.name.casefold()
            if neighbour_key in seen:
                continue
            inherited = self._resolve_time(neighbour, seen | {neighbour_key}, depth + 1)
            if inherited is None:
                continue
            lookup = dict(
                zip(neighbour.frame[right_column].tolist(), inherited.tolist())
            )
            mapped = meta.frame[left_column].map(lookup)
            return pd.Series(mapped.to_numpy(), index=range(len(meta.frame)))
        return None

    def _declared_stype(self, table: str, column: str) -> str | None:
        found = self._col_stypes.get(table.casefold()) or self._col_stypes.get(
            table.split(".")[-1].casefold()
        )
        if not found:
            return None
        return found.get(column.casefold())

    # -- key paths ----------------------------------------------------------

    def _key_path(self, source: str, entity: str) -> list[tuple[str, str, str]] | None:
        """Hops carrying ``source``'s rows to ``entity``, breadth-first.

        Each hop is ``(from_column, to_table, to_column)``: the column read on
        the current frame, the table it lands in, and the column it joins. The
        first hop's ``from_column`` is a column of ``source``; every later hop
        reads the key the previous hop produced.
        """
        if source.casefold() == entity.casefold():
            return []
        adjacency: dict[str, list[tuple[str, str, str]]] = {}
        for src, fkey, dst in self._links:
            dst_meta = self._meta(dst)
            dst_key = (
                dst_meta.primary_keys[0]
                if dst_meta and len(dst_meta.primary_keys) == 1
                else fkey
            )
            # Forward: src.fkey → dst.<pk>. Reverse: dst.<pk> → src.fkey.
            adjacency.setdefault(src, []).append((fkey, dst, dst_key))
            adjacency.setdefault(dst, []).append((dst_key, src, fkey))

        seen = {source.casefold()}
        queue: list[tuple[str, list[tuple[str, str, str]]]] = [(source.casefold(), [])]
        while queue:
            table, path = queue.pop(0)
            for from_col, to_table, to_col in adjacency.get(table, []):
                if to_table in seen:
                    continue
                hop = (from_col, to_table, to_col)
                if to_table == entity.casefold():
                    return path + [hop]
                seen.add(to_table)
                queue.append((to_table, path + [hop]))
        return None

    def _attach_entity_key(
        self, meta: TableMeta, path: list[tuple[str, str, str]], entity: TableMeta
    ) -> pd.Series | None:
        """Resolve each row of ``meta`` to the entity key it belongs to.

        Returns a Series aligned to ``meta.frame.index``; rows with no route to
        an entity get ``NaN`` and drop out downstream.
        """
        if not path:
            if len(entity.primary_keys) != 1:
                return None
            column = entity.primary_keys[0]
            if column not in meta.frame.columns:
                return None
            return meta.frame[column]

        current = meta.frame.reset_index(drop=True).copy()
        current[_ROW] = range(len(current))
        source_column = path[0][0]
        key_column: str | None = None
        for index, (_from_column, to_table, to_col) in enumerate(path):
            right_meta = self._meta(to_table)
            if right_meta is None or source_column not in current.columns:
                return None
            if to_col not in right_meta.frame.columns:
                return None
            # The next hop reads a column of *this* hop's table, so it has to cross
            # in the same merge. Reading it afterwards has nothing left to read it
            # from — which is how a two-hop route silently joined invoice ids to
            # customer ids and attributed almost every detail row to nobody.
            carry = path[index + 1][0] if index + 1 < len(path) else None
            hop_column = f"__kumo_hop{index}"
            carried = f"__kumo_carry{index}"
            wanted = [to_col] + ([carry] if carry and carry != to_col else [])
            if any(name not in right_meta.frame.columns for name in wanted):
                return None
            rename = {to_col: hop_column}
            if carry and carry != to_col:
                rename[carry] = carried
            right = (
                right_meta.frame[wanted]
                .drop_duplicates(subset=[to_col])
                .rename(columns=rename)
            )
            current = current.merge(
                right, left_on=source_column, right_on=hop_column, how="left"
            )
            key_column = hop_column
            if carry:
                source_column = hop_column if carry == to_col else carried
        keyed = current.set_index(_ROW)[key_column]
        return keyed.reindex(range(len(meta.frame)))

    # -- anchors ------------------------------------------------------------

    def _history_span(
        self, spec: TaskSpec, entity: TableMeta
    ) -> tuple[pd.Timestamp, pd.Timestamp] | None:
        """The window of time the data covers, from the target and entity tables."""
        stamps: list[pd.Series] = []
        for meta in (self._meta(spec.target_table), entity):
            if meta is None:
                continue
            times = self._table_time(meta)
            if times is not None and times.notna().any():
                stamps.append(times)
        if not stamps:
            return None
        low = min(s.min() for s in stamps)
        high = max(s.max() for s in stamps)
        if pd.isna(low) or pd.isna(high) or low >= high:
            return None
        return pd.Timestamp(low), pd.Timestamp(high)

    def _anchors(
        self, spec: TaskSpec, entity: TableMeta, predict_anchor: Any
    ) -> tuple[list[pd.Timestamp], pd.Timestamp]:
        """Historical context anchors, plus the anchor the prediction is made at.

        A caller-supplied anchor wins outright; the end of the data is only the
        fallback. The caller anchors forward forecasts at today (see
        ``pql_gen._forecast_anchor``), and ``[anchor, anchor + horizon]`` is
        deliberately the future — clamping it back to the last row would answer
        a different question.
        """
        span = self._history_span(spec, entity)
        latest = None
        if span is not None:
            latest = span[1]
        if predict_anchor is not None:
            try:
                candidate = pd.Timestamp(predict_anchor)
                if candidate.tzinfo is not None:
                    candidate = candidate.tz_convert("UTC").tz_localize(None)
                latest = candidate
            except (TypeError, ValueError):
                pass
        if latest is None:
            latest = pd.Timestamp.utcnow().tz_localize(None)

        window = spec.target.window
        count = self._config.history_anchors if spec.target.is_aggregation else 1
        if window is None or count <= 1:
            return [latest], latest

        width = _window_width(window)
        if span is None:
            return [latest], latest
        start = span[0]
        # The last context anchor must leave a full window to label from, and the
        # predict anchor must sit beyond it so no context label peeks at it.
        last = min(latest - width, latest)
        usable = last - start
        if usable <= pd.Timedelta(0):
            return [latest], latest
        step = usable / count
        anchors = [start + step * (i + 1) for i in range(count)]
        anchors = [a for a in anchors if a + width <= latest] or [latest]
        return anchors, latest

    def _fit_context(
        self, entities: int, anchors: list[pd.Timestamp]
    ) -> list[pd.Timestamp]:
        """Keep the newest anchors that fit the endpoint's ceiling on training rows.

        The context table carries one row per ``(entity, anchor)`` pair, and the
        endpoint refuses more than 10k of them — ``N_train 12960 exceeds limit
        10000`` — regardless of how many entities were asked for. Eight anchors
        over 1620 entities is exactly such a request, and it used to be built
        happily because ``max_instances`` only caps entities, not their product
        with the anchor count.

        Anchors are dropped oldest-first rather than entities: every entity stays
        scoreable and the newest history is the most informative. Entities alone
        are kept below the ceiling by ``max_instances``.
        """
        if not anchors or entities <= 0:
            return anchors
        fits = max(1, _MAX_CONTEXT_INSTANCES // entities)
        if len(anchors) <= fits:
            return anchors
        logger.info(
            "kumo: keeping the %d newest of %d context anchors so %d entities fit the "
            "endpoint's %d training-row ceiling",
            fits,
            len(anchors),
            entities,
            _MAX_CONTEXT_INSTANCES,
        )
        return anchors[-fits:]

    # -- labelling ----------------------------------------------------------

    def _label_frame(
        self,
        spec: TaskSpec,
        entity: TableMeta,
        keys: list[Any],
        anchors: list[pd.Timestamp],
    ) -> pd.DataFrame:
        """``(anchor, key, label)`` triples for every context instance.

        A windowed aggregate is evaluated once per anchor across all entities,
        which keeps the cost at one group-by per anchor rather than one per
        instance.
        """
        rows = pd.DataFrame(
            {
                _ANCHOR: [a for a in anchors for _ in keys],
                _ENTITY_KEY: list(keys) * len(anchors),
            }
        )
        if not spec.target.is_aggregation:
            direct_table, direct_column = spec.target.direct or (entity.name, "")
            meta = self._meta(direct_table) or entity
            if direct_column not in meta.frame.columns or len(entity.primary_keys) != 1:
                return rows.assign(label=None)
            values = meta.frame.set_index(entity.primary_keys[0])[direct_column]
            labels = rows[_ENTITY_KEY].map(values)
            return rows.assign(label=labels.to_numpy())

        target_meta = self._meta(spec.target_table)
        if target_meta is None:
            raise PqlCompileError(
                f"The aggregation target table '{spec.target_table}' is not one of the loaded "
                "tables."
            )
        times = self._table_time(target_meta)
        if times is None:
            raise PqlCompileError(
                f"The aggregation target table '{spec.target_table}' has no timestamp of its own "
                "and none it can inherit from a parent table, so the window cannot be evaluated. "
                "Aggregate a table that records when each row happened (an invoice, order, or "
                "transaction), or ask a SQL question instead."
            )
        events = target_meta.frame
        path = self._key_path(spec.target_table, entity.name)
        if path is None:
            raise PqlCompileError(
                f"'{spec.target_table}' has no path to the prediction entity '{entity.name}' in the "
                "graph, so its rows cannot be attributed to an entity. Pick an event table that "
                "links to the entity, or a different entity."
            )
        entity_keys = self._attach_entity_key(target_meta, path, entity)
        if entity_keys is None:
            raise PqlCompileError(
                f"Cannot attribute '{spec.target_table}' rows to '{entity.name}': the shared key "
                "is not a single column. Ask a SQL question for this one instead."
            )

        working = pd.DataFrame(
            {
                _ENTITY_KEY: entity_keys.to_numpy(),
                "__kumo_time": times.to_numpy(),
            }
        )
        working = working[working[_ENTITY_KEY].notna() & working["__kumo_time"].notna()]
        if spec.target.where:
            working = self._apply_event_filter(
                working, events, spec.target.where, target_meta
            )

        labels: dict[pd.Timestamp, pd.Series] = {}
        for anchor in anchors:
            low = _shift(anchor, spec.target.window.start, spec.target.window.unit)
            high = _shift(anchor, spec.target.window.end, spec.target.window.unit)
            in_window = working[
                (working["__kumo_time"] > low) & (working["__kumo_time"] <= high)
            ]
            labels[anchor] = self._aggregate(
                in_window,
                spec.target.func,
                spec.target.column,
                events,
                target_meta,
                spec.target.where,
                low,
                high,
            )

        out = rows.copy()
        values = []
        for anchor, _key in zip(out[_ANCHOR], out[_ENTITY_KEY]):
            series = labels[anchor]
            values.append(series.get(_key))
        if spec.kind == BINARY:
            # An entity with no rows in the window is missing from the group-by,
            # not absent from the question. COUNT and SUM are zero over an empty
            # window, so `= 0` has to come out true — treating the gap as "no
            # value" made every quiet customer look like an active one, which is
            # the exact opposite of what a churn question asks.
            out["label"] = [
                self._compare(
                    (0.0 if spec.target.func in _ZERO_ON_EMPTY else None)
                    if _missing(value)
                    else value,
                    spec.target.op,
                    spec.target.literal,
                )
                for value in values
            ]
        elif spec.kind == REGRESSION:
            out["label"] = [
                # COUNT and SUM are zero over an empty window; AVG/MIN/MAX have no
                # value there, and such an instance carries no label to learn from.
                (0.0 if spec.target.func in _ZERO_ON_EMPTY else None)
                if _missing(value)
                else float(value)
                for value in values
            ]
        else:
            out["label"] = values
        return out

    def _apply_event_filter(
        self,
        working: pd.DataFrame,
        events: pd.DataFrame,
        where: str,
        meta: TableMeta,
    ) -> pd.DataFrame:
        """Apply the aggregation's ``WHERE`` to the event rows, in pandas.

        PQL only allows same-table predicates here (the static lint enforces
        it), so a column comparison is all that can arrive. A shape pandas
        cannot evaluate is refused rather than ignored — dropping the filter
        would answer a different question.
        """
        mask = self._evaluate_mask(events, where, meta)
        if mask is None:
            raise PqlCompileError(
                f"The aggregation filter `{where}` could not be evaluated offline. Use a simple "
                "column comparison the data can check — e.g. `orders.status = 'late'` or "
                "`payments.days_late > 0`."
            )
        return working[mask.to_numpy()[working.index.to_numpy()]]

    def _evaluate_mask(
        self, frame: pd.DataFrame, where: str, meta: TableMeta
    ) -> pd.Series | None:
        """A boolean mask for a conjunction of ``col <op> literal`` conditions."""
        import re

        ident = r"(?:`[^`\r\n]+`|[A-Za-z_]\w*)"
        pattern = re.compile(
            rf"(?P<col>{ident}(?:\s*\.\s*{ident})?)\s*(?P<op>>=|<=|!=|<>|=|>|<)\s*"
            r"(?P<val>'[^']*'|\"[^\"]*\"|[-\d.]+|true|false)",
            re.IGNORECASE,
        )
        mask: pd.Series | None = None
        consumed = 0
        for match in pattern.finditer(where):
            column = match.group("col").split(".")[-1].strip().strip("`")
            if column not in frame.columns:
                return None
            op = match.group("op")
            raw = match.group("val").strip()
            series = frame[column]
            if raw.startswith(("'", '"')):
                literal: Any = raw[1:-1]
                if op not in ("=", "==", "!=", "<>"):
                    return None
                condition = series.astype("string") == literal
                if op in ("!=", "<>"):
                    condition = ~condition.fillna(False)
            elif raw.lower() in ("true", "false"):
                condition = series.astype("boolean") == (raw.lower() == "true")
            else:
                try:
                    number = float(raw)
                except ValueError:
                    return None
                if pd.api.types.is_datetime64_any_dtype(series):
                    return None
                condition = {
                    ">": series > number,
                    ">=": series >= number,
                    "<": series < number,
                    "<=": series <= number,
                    "=": series == number,
                    "==": series == number,
                    "!=": series != number,
                    "<>": series != number,
                }[op]
            condition = condition.fillna(False)
            mask = condition if mask is None else (mask & condition)
            consumed += 1
        if mask is None or consumed == 0:
            return None
        return mask

    def _aggregate(
        self,
        in_window: pd.DataFrame,
        func: str,
        column: str | None,
        events: pd.DataFrame,
        meta: TableMeta,
        where: str | None,
        low: pd.Timestamp,
        high: pd.Timestamp,
    ) -> pd.Series:
        """One aggregate per entity key over the rows inside the window."""
        if func == "COUNT" or column is None:
            return in_window.groupby(_ENTITY_KEY).size()
        if column not in events.columns:
            raise PqlCompileError(
                f"'{meta.name}.{column}' is not a column of the aggregated table. Name one of its "
                "numeric columns."
            )
        # The values ride along with the working frame so a row filter cannot
        # desynchronise them from the keys.
        values = events[column].to_numpy()[in_window.index.to_numpy()]
        grouped = pd.Series(values, index=in_window.index).groupby(
            in_window[_ENTITY_KEY].to_numpy()
        )
        if func == "SUM":
            return grouped.sum()
        if func == "AVG":
            return grouped.mean()
        if func == "MIN":
            return grouped.min()
        if func == "MAX":
            return grouped.max()
        raise PqlCompileError(f"Unsupported aggregation '{func}'.")

    @staticmethod
    def _compare(value: Any, op: str | None, literal: str | None) -> bool | None:
        """Apply the PQL comparison that turns a quantity into a yes/no label."""
        if op is None or literal is None:
            return None
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return False
        raw = literal.strip()
        if raw.startswith(("'", '"')) and raw.endswith(("'", '"')):
            text = raw[1:-1]
            if op in ("=", "=="):
                return str(value) == text
            if op in ("!=", "<>"):
                return str(value) != text
            return False
        if raw.lower() in ("true", "false"):
            return bool(value) is (raw.lower() == "true")
        try:
            number = float(raw)
            left = float(value)
        except (TypeError, ValueError):
            return False
        return {
            ">": left > number,
            ">=": left >= number,
            "<": left < number,
            "<=": left <= number,
            "=": left == number,
            "==": left == number,
            "!=": left != number,
            "<>": left != number,
        }.get(op, False)

    # -- payload ------------------------------------------------------------

    def _payload(
        self,
        spec: TaskSpec,
        *,
        indices: list[Any] | None,
        anchor_time: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Build the request body and the plan needed to read the answer back."""
        entity = self._meta(spec.entity_table)
        if entity is None:
            raise PqlCompileError(
                f"The prediction entity '{spec.entity_table}' is not one of the loaded tables "
                f"({', '.join(sorted(self._tables)) or 'none'}). Choose an entity from the graph."
            )
        if len(entity.primary_keys) != 1:
            raise PqlCompileError(
                f"'{entity.name}' has a composite key, which the hosted path cannot use as a "
                "prediction entity. Pick a table with a single primary key."
            )
        pk = entity.primary_keys[0]
        keys = self._entity_keys(entity, pk, indices)
        if not keys:
            raise PqlCompileError(
                "No entities were available to score — the sample loaded for this question is "
                "empty. Ask a SQL question instead."
            )

        anchors, predict_anchor = self._anchors(spec, entity, anchor_time)
        anchors = self._fit_context(len(keys), anchors)
        context = self._label_frame(spec, entity, keys, anchors)
        # An instance whose window produced no value cannot teach anything, and
        # the endpoint refuses a null label in the context table.
        context = context[context["label"].notna()].reset_index(drop=True)
        if not len(context):
            raise PqlCompileError(
                "No context instance produced a value for this target — the history the sample "
                "loaded has nothing to learn the window from. Widen the window or ask a SQL "
                "question instead."
            )

        context_instances = pd.DataFrame(
            {
                _INSTANCE_ID: range(len(context)),
                _ENTITY_KEY: context[_ENTITY_KEY].to_numpy(),
                "__anchor": context[_ANCHOR].to_numpy(),
            }
        )
        predict_instances = pd.DataFrame(
            {
                _INSTANCE_ID: range(len(keys)),
                _ENTITY_KEY: keys,
                "__anchor": [predict_anchor] * len(keys),
            }
        )
        context_related = self._related_for(entity, context_instances, spec)
        predict_related = self._related_for(entity, predict_instances, spec)

        self._drop_unreadable_columns(entity, pk, context_related, predict_related)

        task = self._task(spec, entity, context)
        columns = [_INSTANCE_ID, _ANCHOR, pk, "label"]
        predict_columns = [_INSTANCE_ID, _ANCHOR, pk]

        while True:
            schema = self._schema(spec, entity, pk, context, context_related)
            payload = {
                "model": "kumo-relational",
                "task": task,
                "schema": schema,
                "context": {
                    "instance_table": _table(
                        columns,
                        [
                            [iid, _as_timestamp(anchor), _scalar(key), _scalar(label)]
                            for iid, anchor, key, label in zip(
                                context_instances[_INSTANCE_ID],
                                context[_ANCHOR],
                                context[_ENTITY_KEY],
                                context["label"],
                            )
                        ],
                    ),
                    "related_tables": {
                        name: _table(list(frame.columns), frame.values.tolist())
                        for name, frame in context_related.items()
                    },
                },
                "predict": {
                    "instance_table": _table(
                        predict_columns,
                        [
                            [iid, _as_timestamp(predict_anchor), _scalar(key)]
                            for iid, key in zip(range(len(keys)), keys)
                        ],
                    ),
                    "related_tables": {
                        name: _table(list(frame.columns), frame.values.tolist())
                        for name, frame in predict_related.items()
                    },
                },
                "output": self._output(spec),
            }
            size = len(json.dumps(payload, allow_nan=False))
            if size <= self._config.max_payload_bytes:
                break
            thinned = _thin(
                context_related,
                predict_related,
                keep=entity.name.casefold(),
                frac=_fit_fraction(size, self._config.max_payload_bytes),
            )
            if thinned is None:
                raise PqlCompileError(
                    f"The request for this target is {size // 1024 // 1024} MB, over the "
                    f"{self._config.max_payload_bytes // 1024 // 1024} MB the endpoint accepts, "
                    "and the history is already as thin as it can usefully get. Narrow the "
                    "question — fewer entities, a shorter window — or ask a SQL question."
                )
            (context_related, predict_related), thinned_rows = thinned
            logger.warning(
                "kumo: payload %d MB over the %d MB limit; sampling %d related row(s) down",
                size // 1024 // 1024,
                self._config.max_payload_bytes // 1024 // 1024,
                thinned_rows,
            )
        related_rows = sum(len(f) for f in context_related.values())
        plan = {
            "keys": keys,
            "context_instances": len(context),
            "predict_instances": len(keys),
            "related_rows": related_rows,
            "anchor": _as_timestamp(predict_anchor),
        }
        return payload, plan

    def _entity_keys(
        self, entity: TableMeta, pk: str, indices: list[Any] | None
    ) -> list[Any]:
        """The entities to score: the caller's scope, else the loaded sample."""
        column = entity.frame[pk]
        if indices:
            wanted = {_hashable(i) for i in indices}
            filtered = [
                key
                for key in column.drop_duplicates().tolist()
                if _hashable(key) in wanted
            ]
            if filtered:
                keys = filtered
            else:
                keys = column.drop_duplicates().tolist()
        else:
            keys = column.drop_duplicates().tolist()
        if len(keys) > self._config.max_instances:
            # Deterministic thinning keeps repeated runs comparable.
            keys = keys[: self._config.max_instances]
        if len(keys) > _MAX_PREDICT_INSTANCES:
            # The endpoint scores at most 1000 entities per request and refuses
            # the whole body otherwise. Capping here is what keeps a question over
            # a bigger table answerable at all: the first 1000 entities are scored
            # and the rest are reported as out of scope rather than failing the
            # question silently.
            logger.info(
                "kumo: scoring the first %d of %d entities — the endpoint accepts at most %d "
                "per request (raise nothing; ask for a narrower scope instead)",
                _MAX_PREDICT_INSTANCES,
                len(keys),
                _MAX_PREDICT_INSTANCES,
            )
            keys = keys[:_MAX_PREDICT_INSTANCES]
        return keys

    def _related_for(
        self,
        entity: TableMeta,
        instances: pd.DataFrame,
        spec: TaskSpec,
    ) -> dict[str, pd.DataFrame]:
        """Per-instance frames for the entity table and every table linked to it.

        Each instance is one ``(entity, anchor)`` pair, so a table's rows repeat
        across the anchors they were already known at — that repetition is what
        lets the model see history rather than a single snapshot.

        Only the tables the question reads are shipped. Every table that merely
        *can* reach the entity used to be included, and on a ten-table graph that
        put 398 MB on the wire before any sampling, for context the question
        never asked about.
        """
        frames: dict[str, pd.DataFrame] = {
            entity.name.casefold(): self._expand_entity(entity, instances, spec)
        }
        needed = self._needed_tables(entity, spec)
        skipped: list[str] = []
        for name, meta in self._tables.items():
            if name == entity.name.casefold():
                continue
            if name not in needed:
                skipped.append(name)
                continue
            path = self._key_path(meta.name, entity.name)
            if path is None:
                continue
            entity_keys = self._attach_entity_key(meta, path, entity)
            if entity_keys is None:
                continue
            block = self._expand_table(meta, entity_keys, instances, spec)
            if block is not None and len(block):
                frames[name] = block
        if skipped:
            logger.info(
                "kumo: left %d table(s) out of the payload as unrelated to the question: %s",
                len(skipped),
                ", ".join(sorted(skipped)[:8]),
            )
        return frames

    def _needed_tables(self, entity: TableMeta, spec: TaskSpec) -> set[str]:
        """The tables this question reads, plus the bridges that reach them."""
        needed = {entity.name.casefold()}
        if spec.target.is_aggregation:
            target = spec.target_table
        else:
            target = (spec.target.direct or (None, None))[0]
        if not target:
            return needed
        # A PQL may name the table as ``sales.orders``; the graph keys are bare.
        needed.add(_table_name(str(target)))
        for _column, to_table, _to_column in self._key_path(target, entity.name) or ():
            needed.add(_table_name(str(to_table)))
        return needed

    def _expand_entity(
        self, entity: TableMeta, instances: pd.DataFrame, spec: TaskSpec
    ) -> pd.DataFrame:
        """The entity's own attributes, one row per instance."""
        pk = entity.primary_keys[0]
        if pk in _RESERVED:
            raise PqlCompileError(
                f"'{entity.name}.{pk}' is the entity key but its name is reserved for the "
                "instance bookkeeping the hosted endpoint requires. Pick an entity whose key "
                "column has a normal name."
            )
        # The timestamp column stays in: it is how the model measures recency.
        frame = _safe_columns(entity.frame)
        columns = list(frame.columns)
        if not spec.target.is_aggregation and spec.target.direct:
            table, column = spec.target.direct
            if table.casefold() == entity.name.casefold() and column in columns:
                # The label must not ride along as a feature of itself.
                columns = [c for c in columns if c != column]
        merged = instances.merge(
            frame[columns].drop_duplicates(subset=[pk]),
            left_on=_ENTITY_KEY,
            right_on=pk,
            how="left",
            suffixes=("", "__entity"),
        )
        return self._clean(merged, drop=[_ENTITY_KEY, "__anchor"])

    def _expand_table(
        self,
        meta: TableMeta,
        entity_keys: pd.Series,
        instances: pd.DataFrame,
        spec: TaskSpec,
    ) -> pd.DataFrame | None:
        """A related table's rows attributed to instances, cut at the anchor."""
        frame = _safe_columns(meta.frame.reset_index(drop=True))
        # The event timestamp stays in: it is what tells the model how long ago
        # each row happened, and the anchor cut below keeps it non-future.
        columns = list(frame.columns)
        if not columns:
            return None
        block = frame[columns].copy()
        block[_ENTITY_KEY] = entity_keys.to_numpy()
        block = block[block[_ENTITY_KEY].notna()]
        if not len(block):
            return None
        row_times = self._table_time(meta)
        if row_times is not None:
            block["__time"] = row_times.to_numpy()[block.index.to_numpy()]
        else:
            block["__time"] = pd.NaT

        joined = block.merge(instances, on=_ENTITY_KEY, how="inner")
        if row_times is not None:
            in_range = joined["__time"] <= joined["__anchor"]
            if self._config.lookback_days:
                # A prediction is about what happens next, and rows from three
                # years back mostly cost upload time. Anchors keep their own
                # cutoff; this only trims how far behind it they may reach.
                floor = joined["__anchor"] - pd.Timedelta(
                    days=self._config.lookback_days
                )
                in_range = in_range & (joined["__time"] >= floor)
            joined = joined[joined["__time"].isna() | in_range]
        joined = joined.drop(columns=["__time", "__anchor"])
        if len(joined) > self._config.max_related_rows:
            # ``tail`` would keep the last instances only, silently emptying every
            # other entity. An even stride samples the whole table instead, and
            # says so: a capped request is a weaker request and the caller has to
            # be able to see that in the log.
            step = -(-len(joined) // self._config.max_related_rows)
            joined = joined.iloc[::step]
            logger.warning(
                "kumo: capping %s related rows at %d (kept every %dth row) — "
                "raise KUMO_MAX_RELATED_ROWS for a fuller history",
                meta.name,
                self._config.max_related_rows,
                step,
            )
        return self._clean(joined, drop=[_ENTITY_KEY])

    @staticmethod
    def _clean(frame: pd.DataFrame, *, drop: list[str]) -> pd.DataFrame:
        """Convert every cell to a plain Python scalar, keeping those types.

        Going through ``object`` dtype matters: a column of ``0, 1, 2`` left in a
        numeric block gains a float dtype the moment any row is missing, and the
        payload would then declare ``instance_id`` as a float while its schema
        promises ``int64`` — which the endpoint rejects.
        """
        kept = frame.drop(columns=[c for c in drop if c in frame.columns])
        columns = list(kept.columns)
        rows = [
            [_scalar(value) for value in row]
            for row in kept.itertuples(index=False, name=None)
        ]
        return pd.DataFrame(rows, columns=columns, dtype=object)

    def _unreadable_columns(
        self, related: dict[str, pd.DataFrame]
    ) -> set[tuple[str, str]]:
        """Columns whose declaration the hosted endpoint cannot read.

        The local engine calls a wide free-text column ``text``; the hosted SDM
        reads no such semantic type, and categorical stops at
        :data:`_MAX_CATEGORICAL`. A string column over that ceiling therefore
        has no declaration the endpoint accepts — it is refused with
        ``422 UNSUPPORTED_DTYPE`` before any row is read.
        """
        found: set[tuple[str, str]] = set()
        for name, frame in related.items():
            meta = self._meta(name)
            for column in frame.columns:
                if column == _INSTANCE_ID:
                    continue
                declared = self._declared_stype(meta.name if meta else name, column)
                base = None
                if meta is not None and column in meta.frame.columns:
                    base = meta.frame[column]
                if (
                    _column_spec(base if base is not None else frame[column], declared)
                    is None
                ):
                    found.add((_table_name(name), column))
        return found

    def _linked_columns(self, entity: TableMeta, pk: str) -> set[tuple[str, str]]:
        """Columns a relationship or the instance key joins on: never dropped."""
        linked = {(_table_name(entity.name), pk)}
        for src, fkey, dst in self._links:
            linked.add((_table_name(src), fkey))
            dst_meta = self._meta(dst)
            dst_key = (
                dst_meta.primary_keys[0]
                if dst_meta is not None and len(dst_meta.primary_keys) == 1
                else fkey
            )
            linked.add((_table_name(dst), dst_key))
        return linked

    def _drop_unreadable_columns(
        self,
        entity: TableMeta,
        pk: str,
        context_related: dict[str, pd.DataFrame],
        predict_related: dict[str, pd.DataFrame],
    ) -> None:
        """Leave unsendable free-text columns out of both frames.

        Runs before the schema is built, so the declaration and the rows always
        agree. A width rejection from the endpoint is repairable after the fact
        (see :meth:`_predict_response`), but a semantic type the SDM does not
        read cannot be repaired by resending the same column: it has to be gone
        from the first request. Join columns are kept regardless — dropping one
        would break the graph rather than just a feature.
        """
        known = dict(context_related)
        for name, frame in predict_related.items():
            known.setdefault(name, frame)
        skipped = self._unreadable_columns(known) - self._linked_columns(entity, pk)
        if not skipped:
            return
        logger.info(
            "kumo: leaving %s out of the hosted payload — the endpoint's SDM reads "
            "no `text` semantic type, and categorical stops at %d values",
            ", ".join(sorted(f"{table}.{column}" for table, column in skipped)),
            _MAX_CATEGORICAL,
        )
        for frames in (context_related, predict_related):
            for name in list(frames):
                gone = [
                    column
                    for owner, column in skipped
                    if owner == _table_name(name) and column in frames[name].columns
                ]
                if gone:
                    frames[name] = frames[name].drop(columns=gone)

    def _schema(
        self,
        spec: TaskSpec,
        entity: TableMeta,
        pk: str,
        context: pd.DataFrame,
        related: dict[str, pd.DataFrame],
    ) -> dict[str, Any]:
        """The declared shape of every frame in the request."""
        instance_columns = {
            _INSTANCE_ID: {"dtype": "int64", "stype": "ID", "nullable": False},
            _ANCHOR: {
                "dtype": "timestamp[us]",
                "stype": "timestamp",
                "nullable": False,
            },
            # The instance table's key names the entity, so it is an identity
            # column regardless of the local graph's stype: declaring a wide
            # string key as `text` would be refused before any row is read.
            pk: _column_spec(entity.frame[pk], "ID", where=f"{entity.name}.{pk}"),
            "label": self._label_spec(spec, context),
        }
        related_schema: dict[str, Any] = {}
        linked = self._linked_columns(entity, pk)
        for name, frame in related.items():
            columns: dict[str, Any] = {
                _INSTANCE_ID: {"dtype": "int64", "stype": "ID", "nullable": False},
            }
            meta = self._meta(name)
            table_key = _table_name(name)
            for column in frame.columns:
                if column == _INSTANCE_ID:
                    continue
                declared = self._declared_stype(meta.name if meta else name, column)
                if (
                    declared is not None
                    and declared.casefold() == "text"
                    and (table_key, column) in linked
                ):
                    # A relationship joins on this column, so it identifies a
                    # row rather than describing one: it is an ID whatever the
                    # local graph called it, and IDs have no categorical
                    # ceiling (and no `text` refusal waiting for them).
                    declared = "ID"
                # Take the dtype from the warehouse frame, not from the payload
                # frame: the latter is object-typed by design (see _clean).
                base = None
                if meta is not None and column in meta.frame.columns:
                    base = meta.frame[column]
                column_spec = _column_spec(
                    base if base is not None else frame[column],
                    declared,
                    where=f"{meta.name if meta else name}.{column}",
                )
                if column_spec is not None:
                    columns[column] = column_spec
            related_schema[table_key] = {
                "columns": columns,
                # A column left out by _unreadable_columns cannot stay a
                # declared key either; the endpoint checks the two together.
                "primary_key": [_INSTANCE_ID]
                + [
                    column
                    for column in (meta.primary_keys if meta else ())
                    if column in frame.columns
                ],
            }
        relationships = [
            {
                "source_table": "instance_table",
                "source_columns": [pk],
                "target_table": _table_name(entity.name.casefold()),
                "target_columns": [pk],
            }
        ]
        for src, fkey, dst in self._links:
            src_name = _table_name(src)
            dst_name = _table_name(dst)
            if src_name not in related_schema or dst_name not in related_schema:
                continue
            dst_meta = self._meta(dst)
            dst_key = (
                dst_meta.primary_keys[0]
                if dst_meta and len(dst_meta.primary_keys) == 1
                else fkey
            )
            relationships.append(
                {
                    "source_table": src_name,
                    "source_columns": [fkey],
                    "target_table": dst_name,
                    "target_columns": [dst_key],
                }
            )
        return {
            "instance_table": {
                "columns": instance_columns,
                "primary_key": _INSTANCE_ID,
            },
            "related_tables": related_schema,
            "relationships": relationships,
        }

    def _label_spec(self, spec: TaskSpec, context: pd.DataFrame) -> dict[str, Any]:
        # Declared per kind rather than inferred from the column: a regression
        # window that happens to be empty everywhere would otherwise present as
        # an object column and the endpoint would reject the task/target mismatch.
        if spec.kind == BINARY:
            return {"dtype": "bool", "stype": "categorical", "nullable": True}
        if spec.kind == REGRESSION:
            return {"dtype": "float64", "stype": "numerical", "nullable": True}
        return {"dtype": "string", "stype": "categorical", "nullable": True}

    def _task(
        self, spec: TaskSpec, entity: TableMeta, context: pd.DataFrame
    ) -> dict[str, Any]:
        target: dict[str, Any] = {"column_name": "label"}
        if spec.kind == BINARY:
            target.update(
                dtype="bool", classes=["false", "true"], positive_class="true"
            )
            kind = "binary_classification"
        elif spec.kind == REGRESSION:
            target.update(dtype="float64")
            kind = "regression"
        else:
            classes = sorted(
                {str(v) for v in context["label"].dropna().unique().tolist()}
            )
            target.update(dtype="string", classes=classes)
            kind = "multiclass_classification"
        return {
            "kind": kind,
            "target": target,
            "entity_table_names": [_table_name(entity.name.casefold())],
            "anchor_time_column": _ANCHOR,
        }

    @staticmethod
    def _output(spec: TaskSpec) -> dict[str, Any]:
        # The endpoint rejects `probabilities` for a regression task, so the
        # field list is per-kind rather than fixed.
        if spec.kind == REGRESSION:
            return {"fields": ["prediction"]}
        return {"fields": ["prediction", "probabilities"]}

    # -- transport ----------------------------------------------------------

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, allow_nan=False).encode()
        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        request = urllib.request.Request(self._config.url, data=body, headers=headers)

        # A large body occasionally dies mid-handshake (SSL EOF) or hits a
        # gateway 5xx; both are worth one more try before failing the question.
        last: str | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(
                    request, timeout=self._config.timeout
                ) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as error:
                detail = ""
                try:
                    detail = error.read().decode()[:800]
                except Exception:  # noqa: BLE001 - the body is best-effort context
                    pass
                if error.code < 500 or attempt == 2:
                    raise RuntimeError(
                        f"hosted prediction failed ({error.code}): {detail}"
                    ) from error
                last = f"{error.code}: {detail}"
            except OSError as error:
                last = f"{type(error).__name__}: {error}"
                if attempt == 2:
                    break
            time.sleep(1.0 + attempt)
        raise RuntimeError(
            f"hosted prediction unreachable at {self._config.url} after 3 attempts ({last})"
        )

    # -- reading the answer -------------------------------------------------

    def _to_frame(
        self, spec: TaskSpec, response: dict[str, Any], plan: dict[str, Any]
    ) -> pd.DataFrame:
        """Turn the endpoint's per-instance list into the engine's frame shape."""
        keys = plan["keys"]
        anchor = plan.get("anchor")
        rows: list[dict[str, Any]] = []
        for prediction in response.get("predictions") or []:
            index = prediction.get("row_index")
            if index is None:
                index = prediction.get("id")
            try:
                position = int(index)
            except (TypeError, ValueError):
                continue
            if position < 0 or position >= len(keys):
                continue
            record: dict[str, Any] = {
                "ENTITY": keys[position],
                "anchor_timestamp": anchor,
            }
            probabilities = prediction.get("probabilities") or {}
            if spec.kind == BINARY:
                # The ranking and preview code looks for `true_prob` first, so the
                # positive class must land under that exact name.
                for name, value in probabilities.items():
                    record[f"{str(name).capitalize()}_PROB"] = value
                if (
                    "True_PROB" not in record
                    and prediction.get("prediction") is not None
                ):
                    record["True_PROB"] = (
                        1.0
                        if prediction["prediction"] in (True, "true", "True", 1)
                        else 0.0
                    )
            elif spec.kind == REGRESSION:
                record[f"{spec.label_column.upper()}_PRED"] = prediction.get(
                    "prediction"
                )
            else:
                for name, value in probabilities.items():
                    record[f"{name}_PROB"] = value
                record[f"{spec.label_column.upper()}_PRED"] = prediction.get(
                    "prediction"
                )
            rows.append(record)

        if not rows:
            if spec.kind == REGRESSION:
                return pd.DataFrame(
                    columns=["ENTITY", "TARGET_PRED", "anchor_timestamp"]
                )
            return pd.DataFrame(columns=["ENTITY", "True_PROB", "anchor_timestamp"])
        return pd.DataFrame(rows)


def _table_name(name: str) -> str:
    """The name a frame is declared under: bare, casefolded."""
    return name.split(".")[-1].casefold()


def _shape_report(payload: dict[str, Any]) -> dict[str, dict[str, tuple[str, int]]]:
    """Per related table, each column's declared stype and how many values it carries.

    Counted over the rows actually being sent — that is the number the endpoint
    measures, not the distinct count of the warehouse table it came from.
    """
    report: dict[str, dict[str, tuple[str, int]]] = {}
    schema = (payload.get("schema") or {}).get("related_tables") or {}
    for table, declaration in schema.items():
        columns = declaration.get("columns") or {}
        values: dict[str, set[Any]] = {column: set() for column in columns}
        for section in ("context", "predict"):
            data = ((payload.get(section) or {}).get("related_tables") or {}).get(table)
            if not data:
                continue
            position = {name: i for i, name in enumerate(data.get("columns") or [])}
            for column in values:
                at = position.get(column)
                if at is None:
                    continue
                seen = values[column]
                for row in data.get("rows") or []:
                    if at < len(row):
                        seen.add(row[at])
        report[table] = {
            column: (str(spec.get("stype")), len(seen))
            for column, spec in columns.items()
            for seen in (values[column],)
        }
    return report


def _widest_columns(payload: dict[str, Any], top: int = 5) -> str:
    """The columns carrying the most distinct values, for the repair log line.

    The endpoint names the offending cardinality but not the column, so this is
    what makes a repeat of it diagnosable from the application log alone.
    """
    report = _shape_report(payload)
    ranked = sorted(
        (
            (distinct, f"{table}.{column}({stype})")
            for table, columns in report.items()
            for column, (stype, distinct) in columns.items()
        ),
        reverse=True,
    )
    return ", ".join(f"{name}={distinct}" for distinct, name in ranked[:top]) or "none"


def _joined_columns(payload: dict[str, Any]) -> set[tuple[str, str]]:
    """Columns a relationship joins on: dropping one breaks the graph, not just a feature."""
    used: set[tuple[str, str]] = set()
    for relation in (payload.get("schema") or {}).get("relationships") or []:
        for side in ("source", "target"):
            table = str(relation.get(f"{side}_table") or "")
            for column in relation.get(f"{side}_columns") or []:
                used.add((table, str(column)))
    return used


def _drop_columns(payload: dict[str, Any], targets: set[tuple[str, str]]) -> None:
    """Remove ``(table, column)`` pairs from the schema and from every sent frame."""
    for table, declaration in (
        (payload.get("schema") or {}).get("related_tables") or {}
    ).items():
        columns = declaration.get("columns") or {}
        gone = [
            column for owner, column in targets if owner == table and column in columns
        ]
        for column in gone:
            columns.pop(column, None)
        if gone:
            declaration["primary_key"] = [
                column
                for column in declaration.get("primary_key") or []
                if column not in gone
            ]
    for section in ("context", "predict"):
        tables = (payload.get(section) or {}).get("related_tables") or {}
        for table, data in tables.items():
            gone = {column for owner, column in targets if owner == table}
            present = gone & set(data.get("columns") or [])
            if not present:
                continue
            keep = [
                i for i, column in enumerate(data["columns"]) if column not in present
            ]
            data["columns"] = [data["columns"][i] for i in keep]
            data["rows"] = [[row[i] for i in keep] for row in data.get("rows") or []]


def _safe_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Rename real columns that collide with the instance bookkeeping names.

    ``instance_id``/``anchor_time``/``label`` belong to the payload protocol, so
    a warehouse column of the same name has to step aside rather than shadow it.
    """
    clashes = {c: f"{c}__src" for c in frame.columns if c in _RESERVED}
    return frame.rename(columns=clashes) if clashes else frame


def _table(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"format": "arrays", "columns": columns, "rows": rows}


# Below this a table carries too little to give up more of it.
_THIN_FLOOR = 500
# Fixed, so the same question thins the same way and stays reproducible.
_THIN_SEED = 0


def _fit_fraction(size: int, limit: int) -> float:
    """How much of the related rows to keep so ``size`` lands under ``limit``."""
    return max(0.1, min(0.9, limit / size * 0.9))


def _thin(
    context_related: dict[str, pd.DataFrame],
    predict_related: dict[str, pd.DataFrame],
    *,
    keep: str,
    frac: float,
) -> tuple[tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]], int] | None:
    """Sample the related tables down to ``frac``, uniformly and repeatably.

    The entity table stays whole: it is one row per instance, so thinning it
    drops entities instead of history. Sampling rather than slicing matters —
    rows arrive grouped by event and then by instance, so every n-th row would
    keep only certain instances and silently starve the rest.
    """
    thinned = 0
    sides: list[dict[str, pd.DataFrame]] = []
    for frames in (context_related, predict_related):
        new: dict[str, pd.DataFrame] = {}
        for name, frame in frames.items():
            if name == keep or len(frame) <= _THIN_FLOOR:
                new[name] = frame
                continue
            kept = frame.sample(frac=frac, random_state=_THIN_SEED)
            thinned += len(frame) - len(kept)
            new[name] = kept.reset_index(drop=True)
        sides.append(new)
    if not thinned:
        return None
    return (sides[0], sides[1]), thinned


def _hashable(value: Any) -> Any:
    return tuple(value) if isinstance(value, (list, tuple)) else value


def _missing(value: Any) -> bool:
    """True for ``None``/``NaN``: no aggregate was produced for that instance."""
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False
