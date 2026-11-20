# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Compile a PQL query into the materialised task a hosted endpoint accepts.

A Universal TFM NIM takes a PQL query and resolves it server-side. The hosted
API on build.nvidia.com does not: it takes the *result* of resolving it — a task
kind, a target column, the entity table to score, an anchor-time column, and the
context/predict frames already sliced per instance. The SDK performs that
translation in ``PredictiveQuery.materialize()``, which is not redistributable;
this module is the local stand-in for it.

Only the shapes the prompt asks for are compiled. Anything richer (an ``AND``
of two targets, an ``ASSUMING`` counterfactual, a ``FORECAST`` trajectory) is
refused with a message the repair loop can act on, rather than silently compiled
into a different question — a silently wrong prediction is worse than a refusal.

The compiled form is deliberately declarative: :mod:`auto_ontology.retrieval.kumo.hosted`
turns it into frames, and nothing here touches data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Lexical pieces. PQL identifiers are either bare or backtick-quoted, so every
# name in a pattern goes through the same alternation.
# ---------------------------------------------------------------------------

_IDENT = r"(?:`[^`\r\n]+`|[A-Za-z_]\w*)"

_PREDICT = re.compile(r"(?im)^[ \t]*PREDICT\b")
_FOR = re.compile(r"\bFOR\b(?!ECAST)", re.IGNORECASE)
_FORECAST = re.compile(r"\bFORECAST\s+(\d+)\s+TIMEFRAMES", re.IGNORECASE)
_RANK_TOP = re.compile(r"\bRANK\s+TOP\s+(\d+)", re.IGNORECASE)
_ASSUMING = re.compile(r"\bASSUMING\b", re.IGNORECASE)

_FOR_ENTITY = re.compile(
    rf"\bFOR\s+(?P<each>EACH\s+)?(?P<table>{_IDENT}(?:\s*\.\s*{_IDENT})*)\.(?P<pk>{_IDENT})(?!\s*\.)",
    re.IGNORECASE,
)
_ENTITY_WHERE = re.compile(r"\bWHERE\b", re.IGNORECASE)

_AGG_OPEN = re.compile(r"\b(COUNT|SUM|AVG|MIN|MAX|LIST_DISTINCT)\s*\(", re.IGNORECASE)
# The trailing ``, <start>, <end>, <unit>`` window args of an aggregation.
_WINDOW_ARGS = re.compile(
    r",\s*(?P<start>-?\d+)\s*,\s*(?P<end>-?\d+)\s*,\s*(?P<unit>[A-Za-z]+)\s*$"
)
# `<column>`, `<table>.<column>`, or a schema-qualified `<schema>.<table>.<column>`:
# the LLM sometimes prefixes the schema even though the graph spells bare names.
_QUALIFIED = re.compile(rf"^(?P<head>(?:{_IDENT}\s*\.\s*)+)(?P<column>{_IDENT}|\*)$")
_COMPARISON = re.compile(
    r"\s*(?P<op>>=|<=|!=|<>|==|=|>|<)\s*(?P<literal>.+)$", re.DOTALL
)
_CONJUNCTION = re.compile(r"\b(AND|OR)\b", re.IGNORECASE)

# Column stypes that make a bare column target a class rather than a quantity.
_CATEGORICAL_STYPES = frozenset({"categorical", "multicategorical", "ID", "text"})

_TIME_UNITS = frozenset(
    {
        "second",
        "seconds",
        "minute",
        "minutes",
        "hour",
        "hours",
        "day",
        "days",
        "week",
        "weeks",
        "month",
        "months",
    }
)

# Task kinds the hosted endpoint accepts, as it spells them.
BINARY = "binary_classification"
REGRESSION = "regression"
MULTICLASS = "multiclass_classification"
LINK = "link_prediction"


class PqlCompileError(ValueError):
    """A PQL query the hosted path cannot compile, with a repairable message."""


@dataclass(frozen=True)
class Window:
    """The ``(anchor+start, anchor+end]`` slice an aggregation is taken over."""

    start: int
    end: int
    unit: str

    def describe(self) -> str:
        return f"({self.start}, {self.end}] {self.unit}"


@dataclass(frozen=True)
class Target:
    """What is being predicted, in the form the materialiser can evaluate.

    ``func``/``table``/``column``/``window`` describe a windowed aggregation;
    ``direct`` names a plain entity column instead. Exactly one of the two is
    set. ``op``/``literal`` are present when a bare value was compared to a
    threshold, which is what turns a quantity into a yes/no question.
    """

    func: str | None = None
    table: str | None = None
    column: str | None = None
    where: str | None = None
    window: Window | None = None
    direct: tuple[str, str] | None = None
    op: str | None = None
    literal: str | None = None

    @property
    def is_aggregation(self) -> bool:
        return self.func is not None


@dataclass(frozen=True)
class TaskSpec:
    """A compiled PQL query: enough to materialise, not yet materialised."""

    pql: str
    kind: str
    target: Target
    entity_table: str
    entity_pk: str
    single_entity: bool = False
    entity_filter: str | None = None
    rank_top: int | None = None
    forecast: int | None = None

    @property
    def target_table(self) -> str:
        """The table the target lives on (the entity table for a bare column)."""
        if self.target.is_aggregation:
            return str(self.target.table)
        return str((self.target.direct or (self.entity_table, ""))[0])

    @property
    def label_column(self) -> str:
        """Name the synthesised target column gets in the payload."""
        return "target"


def unquote(name: str) -> str:
    """Strip the backticks PQL puts around a name that cannot be written bare."""
    name = name.strip()
    if len(name) >= 2 and name[0] == "`" and name[-1] == "`":
        return name[1:-1]
    return name


def split_qualified(text: str) -> tuple[str, str] | None:
    """Split ``column`` / ``table.column`` / ``schema.table.column`` into table + column.

    A leading schema is dropped: the graph spells bare table names, so the last
    qualifier before the column is the one it will recognise. Returns ``None``
    when the text is not a qualified name at all.
    """
    match = _QUALIFIED.match(text.strip())
    if match is None:
        return None
    head = match.group("head").strip().rstrip(".")
    parts = [unquote(p.strip()) for p in re.split(r"\s*\.\s*", head) if p.strip()]
    if not parts:
        return None
    column = match.group("column")
    return parts[-1], column if column == "*" else unquote(column)


def _last_qualifier(text: str) -> str:
    """The bare table name of a possibly schema-qualified name."""
    head = text.strip().rstrip(".")
    parts = [unquote(p.strip()) for p in re.split(r"\s*\.\s*", head) if p.strip()]
    return parts[-1] if parts else ""


def _balanced_body(text: str, open_index: int) -> tuple[str, int]:
    """Return ``(body, index_after_close)`` for the parenthesis opening at ``open_index``."""
    depth = 0
    for i in range(open_index, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : i], i + 1
    raise PqlCompileError(
        "Unbalanced parentheses in the PQL target — the aggregation call is not closed. "
        "Write the target as AGG(<table>.<column>, <start>, <end>, <unit>)."
    )


def _split_aggregate_body(body: str) -> tuple[str, str | None, Window]:
    """Split an aggregation body into ``(operand, where_clause, window)``.

    ``operand`` is ``table.column`` or ``table.*``; the window args are the
    trailing three comma-separated fields.
    """
    match = _WINDOW_ARGS.search(body)
    if match is None:
        raise PqlCompileError(
            "An aggregation target must end with its window arguments "
            "`, <start>, <end>, <unit>` (e.g. `COUNT(orders.*, 0, 90, days)`). "
            "Add them: they say which slice of the future the target covers."
        )
    unit = match.group("unit").lower()
    if unit not in _TIME_UNITS:
        raise PqlCompileError(
            f"Unknown aggregation time unit '{match.group('unit')}'. Use one of: "
            "seconds, minutes, hours, days, weeks, months."
        )
    window = Window(
        start=int(match.group("start")), end=int(match.group("end")), unit=unit
    )

    head = body[: match.start()].strip()
    where = None
    where_match = _ENTITY_WHERE.search(head)
    if where_match is not None:
        where = head[where_match.end() :].strip() or None
        head = head[: where_match.start()].strip()
    return head, where, window


def _parse_target(
    raw: str, col_stypes: dict[str, dict[str, str]] | None
) -> tuple[Target, str]:
    """Parse the target text into ``(Target, kind)``."""
    text = raw.strip()
    if not text:
        raise PqlCompileError(
            "The PQL has no target — PREDICT must be followed by what to predict."
        )

    agg = _AGG_OPEN.search(text)
    if agg is not None:
        return _parse_aggregate_target(text, agg, col_stypes)
    return _parse_direct_target(text, col_stypes)


def _parse_aggregate_target(
    text: str,
    agg: re.Match[str],
    col_stypes: dict[str, dict[str, str]] | None,
) -> tuple[Target, str]:
    func = agg.group(1).upper()
    body, after = _balanced_body(text, agg.end() - 1)

    prefix = text[: agg.start()].strip()
    suffix = text[after:].strip()
    if prefix:
        raise PqlCompileError(
            f"Unexpected text before the aggregation target: '{prefix}'. "
            "The target must start with the aggregation call, optionally followed by a single "
            "comparison (e.g. `COUNT(orders.*, 0, 90, days) > 0`)."
        )

    if _CONJUNCTION.search(suffix) or _AGG_OPEN.search(suffix):
        raise PqlCompileError(
            "A target combining several conditions (AND/OR, or a second aggregation) cannot be "
            "evaluated by the hosted endpoint. Ask the single most important condition instead — "
            "e.g. `COUNT(orders.*, 0, 90, days) = 0` rather than `COUNT(...) = 0 AND SUM(...) > 5`."
        )

    op = literal = None
    if suffix:
        cmp = _COMPARISON.match(suffix)
        if cmp is None:
            raise PqlCompileError(
                f"Cannot parse '{suffix}' after the aggregation. Use one comparison operator "
                "(`> 0`, `= 0`, `>= 100`) or nothing at all."
            )
        op, literal = cmp.group("op"), cmp.group("literal").strip()

    operand, where, window = _split_aggregate_body(body)
    split = split_qualified(operand)
    if split is None:
        raise PqlCompileError(
            f"The aggregation operand '{operand}' must be qualified as `<table>.<column>` or "
            "`<table>.*` (use `*` to count rows)."
        )
    table, column = split
    if column == "*":
        column = None
        if func not in ("COUNT",):
            raise PqlCompileError(
                f"`{func}({table}.*, ...)` is not valid — `*` counts rows and belongs to COUNT only. "
                "Name the numeric column to aggregate."
            )

    target = Target(
        func="COUNT" if func == "LIST_DISTINCT" else func,
        table=table,
        column=column,
        where=where,
        window=window,
        op=op,
        literal=literal,
    )
    if op is not None:
        return target, BINARY
    if func == "LIST_DISTINCT":
        return target, LINK
    return target, REGRESSION


def _parse_direct_target(
    text: str, col_stypes: dict[str, dict[str, str]] | None
) -> tuple[Target, str]:
    op = literal = None
    head = text
    conj = _CONJUNCTION.search(text)
    if conj is not None:
        raise PqlCompileError(
            "A target combining several conditions (AND/OR) cannot be evaluated by the hosted "
            "endpoint. Ask the single most important condition instead."
        )
    cmp = _COMPARISON.search(text)
    if cmp is not None:
        head = text[: cmp.start()].strip()
        op, literal = cmp.group("op"), cmp.group("literal").strip()

    split = split_qualified(head)
    if split is None:
        raise PqlCompileError(
            f"The target '{head}' must name a column as `<table>.<column>` — either a plain "
            "entity column to classify, or an aggregation like `COUNT(<event>.*, 0, 90, days)`."
        )
    table, column = split
    if column == "*":
        raise PqlCompileError(
            "`<table>.*` is only valid inside COUNT(...). Name the column to predict, or write "
            "`COUNT(<table>.*, 0, 90, days)`."
        )

    target = Target(direct=(table, column), op=op, literal=literal)
    if op is not None:
        return target, BINARY

    stype = ((col_stypes or {}).get(table.lower()) or {}).get(column.lower())
    if stype is not None and stype.lower() in _CATEGORICAL_STYPES:
        return target, MULTICLASS
    if stype is None:
        # Without a declared stype, a bare column target is the multi-class case
        # the grammar describes (a status/category column read straight off the
        # entity); a numeric quantity is normally asked for as an aggregation.
        return target, MULTICLASS
    return target, REGRESSION


def _strip_clauses(query: str) -> tuple[str, int | None, str | None]:
    """Cut the trailing clauses off and return ``(predict_body, forecast_n, assuming)``.

    ``ASSUMING`` is a counterfactual the hosted endpoint has no way to express,
    so it is returned to the caller to refuse explicitly rather than dropped.
    """
    text = query.strip()
    start = _PREDICT.search(text)
    if start is None:
        raise PqlCompileError(
            "No PREDICT found. Emit a single plain `PREDICT <target> FOR EACH <entity>.<pk>` — "
            "do not wrap it in EVALUATE, SELECT, or a fenced block inside another one."
        )
    text = text[start.end() :].strip()

    forecast = None
    fc = _FORECAST.search(text)
    if fc is not None:
        forecast = int(fc.group(1))
        # FORECAST sits between the target and the FOR clause, so cut it out
        # rather than truncating — the entity clause must survive.
        text = (text[: fc.start()] + " " + text[fc.end() :]).strip()

    assuming = None
    asm = _ASSUMING.search(text)
    if asm is not None:
        assuming = text[asm.end() :].strip()
        text = text[: asm.start()].strip()

    # A statement-terminating semicolon (or a trailing comment) is noise.
    text = text.rstrip(";").strip()
    return text, forecast, assuming


def compile_pql(
    query: str,
    *,
    col_stypes: dict[str, dict[str, str]] | None = None,
) -> TaskSpec:
    """Compile ``query`` into a :class:`TaskSpec`, or raise :class:`PqlCompileError`.

    The error text is the repair signal: it states what the hosted path cannot
    express so the next generation can land on a shape it can.
    """
    body, forecast, assuming = _strip_clauses(query)

    if assuming:
        raise PqlCompileError(
            "The hosted endpoint cannot evaluate an ASSUMING counterfactual. Drop the ASSUMING "
            "clause and predict the observed outcome instead."
        )

    rank_top = None
    rank = _RANK_TOP.search(body)
    if rank is not None:
        rank_top = int(rank.group(1))
        body = (body[: rank.start()] + body[rank.end() :]).strip()

    entity_match = _FOR_ENTITY.search(body)
    if entity_match is None:
        raise PqlCompileError(
            "No `FOR EACH <entity>.<primary_key>` (or `FOR <entity>.<primary_key>=<value>`) found. "
            "State which entity population to score — PQL always ends with a FOR clause."
        )
    target_text = body[: entity_match.start()].strip()
    tail = body[entity_match.end() :].strip()

    entity_table = _last_qualifier(entity_match.group("table"))
    entity_pk = unquote(entity_match.group("pk"))

    entity_where = None
    single = not bool(entity_match.group("each"))
    if tail:
        where_match = _ENTITY_WHERE.search(tail)
        if where_match is not None:
            entity_where = tail[where_match.end() :].strip() or None
        elif single:
            # `FOR customers.customerid = 42` — the value is part of the tail.
            single_value = tail.lstrip("= ").strip()
            if single_value:
                entity_where = f"{entity_pk} = {single_value}"

    target, kind = _parse_target(target_text, col_stypes)
    if rank_top is not None and kind not in (LINK, MULTICLASS):
        raise PqlCompileError(
            "RANK TOP only applies to a link prediction (LIST_DISTINCT) or a multi-class column "
            "target. A binary/regression target needs no RANK — its scores already rank entities."
        )

    return TaskSpec(
        pql=query.strip(),
        kind=kind,
        target=target,
        entity_table=entity_table,
        entity_pk=entity_pk,
        single_entity=single,
        entity_filter=entity_where,
        rank_top=rank_top,
        forecast=forecast,
    )


def describe(spec: TaskSpec) -> str:
    """A one-line human summary, for logs and telemetry."""
    target = spec.target
    if target.is_aggregation:
        operand = f"{target.table}.{target.column or '*'}"
        shape = f"{target.func}({operand}{', ' + target.where if target.where else ''})"
        if target.window is not None:
            shape += f" over {target.window.describe()}"
    else:
        shape = ".".join(target.direct or ())
    if target.op is not None:
        shape += f" {target.op} {target.literal}"
    return (
        f"{spec.kind}: {shape} for each {spec.entity_table}.{spec.entity_pk}"
        f"{' (single)' if spec.single_entity else ''}"
    )


def available_for(spec: TaskSpec) -> dict[str, Any]:
    """The subset of a spec that must be told to the materialiser explicitly."""
    return {
        "kind": spec.kind,
        "entity_table": spec.entity_table,
        "entity_pk": spec.entity_pk,
        "single_entity": spec.single_entity,
        "rank_top": spec.rank_top,
    }
