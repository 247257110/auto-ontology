# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""A graph the prompt and the materialiser can read, built from plain frames.

The self-hosted path builds its graph with ``kumo_relational_client``'s
``Graph.from_data``, which infers semantic types and links and is the engine's own
view of the schema. The hosted path needs none of that inference — it only needs
the same *shape* the rest of this package reads:

* ``graph.tables`` — name → table, each with ``columns`` (``name``/``stype``/
  ``dtype``), a primary key, and a timestamp column;
* ``graph.edges`` — ``(src, fkey, dst)`` foreign keys;
* ``graph[name]``, ``graph.link(...)``, iteration.

Those are the attributes ``build_graph_context`` (prompt DDL), ``_entity_ids``
(scoping) and ``built_graph_fingerprint`` (cache identity) touch, so a plain
duck-typed object keeps every downstream reader working without the engine
package installed.

Semantic types are inferred from the frame's dtypes plus the catalog's declared
keys. That is deliberately conservative: a column is an ``ID`` only when it is
integer-typed and looks like a key, ``categorical`` only when its distinct count
is small, and ``text`` otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

import pandas as pd

# A text column with more distinct values than this is a free-text field, not a
# category: the PQL prompt shows categories with their values, which a high-
# cardinality column would drown out.
_CATEGORY_LIMIT = 50
_NULLABLE_ID_LIMIT = 10_000_000


@dataclass
class GraphColumn:
    """One column, spelled the way the prompt and the lint expect."""

    name: str
    stype: str
    dtype: str = ""


@dataclass
class GraphTable:
    """One table: its columns, identity, and the timestamp that orders its rows."""

    name: str
    columns: list[GraphColumn]
    primary_key: Any = None
    time_column: GraphColumn | None = None
    end_time_column: GraphColumn | None = None

    @property
    def primary_key_columns(self) -> tuple[str, ...]:
        """The declared key as plain column names, single or composite.

        ``_declare_primary_keys`` assigns either a bare name or a tuple; both
        have to read back the same way, because every edge is oriented towards a
        key.
        """
        raw = self.primary_key
        if raw is None:
            return ()
        if isinstance(raw, tuple):
            return tuple(str(part) for part in raw)
        if isinstance(raw, str):
            return (raw,)
        name = getattr(raw, "name", None)
        return (str(name),) if name else ()


@dataclass(frozen=True)
class GraphEdge:
    """A foreign key: ``src.fkey`` points at ``dst``'s primary key."""

    src_table: str
    fkey: str
    dst_table: str


@dataclass
class FramedGraph:
    """The subset of a KumoRFM graph this package reads, over pandas frames."""

    tables: dict[str, GraphTable] = field(default_factory=dict)
    edges: list[GraphEdge] = field(default_factory=list)

    def __getitem__(self, name: str) -> GraphTable:
        return self.tables[name]

    def __contains__(self, name: object) -> bool:
        return name in self.tables

    def __iter__(self) -> Iterator[str]:
        return iter(self.tables)

    def __len__(self) -> int:
        return len(self.tables)

    def keys(self):
        return self.tables.keys()

    def link(self, src: str, fkey: Any, dst: str) -> None:
        """Record ``src.fkey → dst``, refusing a link the graph cannot state."""
        if src not in self.tables or dst not in self.tables:
            raise KeyError(f"{src} or {dst} is not a table of this graph")
        if isinstance(fkey, tuple):
            for part in fkey:
                if part not in self._column_names(src):
                    raise KeyError(f"{src} has no column {part}")
        elif fkey not in self._column_names(src):
            raise KeyError(f"{src} has no column {fkey}")
        edge = GraphEdge(src, fkey, dst)
        if edge not in self.edges:
            self.edges.append(edge)

    def _column_names(self, table: str) -> set[str]:
        return {column.name for column in self.tables[table].columns}


def build_framed_graph(
    frames: dict[str, pd.DataFrame],
    catalog_keys: dict[str, list[str]] | None = None,
) -> FramedGraph:
    """Describe ``frames`` as a graph: typed columns, a key, and a time column.

    ``catalog_keys`` are the primary keys the catalog recorded; a table with none
    falls back to a single integer column named after the table. No link is
    created here — the catalog join paths supply those, and guessing them would
    give the model a relationship the warehouse never declared.
    """
    catalog_keys = catalog_keys or {}
    graph = FramedGraph()
    for name, frame in frames.items():
        declared = [
            str(column).strip()
            for column in (catalog_keys.get(name) or [])
            if str(column).strip() in frame.columns
        ]
        key_names = declared or _guess_key(name, frame)
        columns = [
            GraphColumn(
                name=str(column),
                stype=_infer_stype(str(column), frame[column], set(key_names)),
                dtype=str(frame[column].dtype),
            )
            for column in frame.columns
        ]
        time_column = _first_timestamp(frame, columns)
        graph.tables[name] = GraphTable(
            name=name,
            columns=columns,
            primary_key=(
                key_names[0] if len(key_names) == 1 else tuple(key_names)
            )
            if key_names
            else None,
            time_column=time_column,
        )
    return graph


def _guess_key(table: str, frame: pd.DataFrame) -> list[str]:
    """The single integer column that names a row, when the catalog said nothing."""
    lowered = {str(c).lower(): str(c) for c in frame.columns}
    stem = table.lower().rstrip("s")
    for candidate in (f"{stem}id", "id", f"{stem}_id", "key"):
        column = lowered.get(candidate)
        if column is None or not pd.api.types.is_integer_dtype(frame[column]):
            continue
        if frame[column].notna().all() and frame[column].is_unique:
            return [column]
    return []


def _infer_stype(name: str, series: pd.Series, keys: set[str]) -> str:
    """The semantic type the prompt shows and the static lint reasons about."""
    if name in keys:
        return "ID"
    if pd.api.types.is_bool_dtype(series.dtype):
        return "categorical"
    if pd.api.types.is_datetime64_any_dtype(series.dtype):
        return "timestamp"
    if pd.api.types.is_numeric_dtype(series.dtype):
        # A non-key integer named like an identifier is still a reference.
        if name.lower().endswith("id") and pd.api.types.is_integer_dtype(series.dtype):
            return "ID"
        return "numerical"
    distinct = series.nunique(dropna=True)
    return "categorical" if distinct <= _CATEGORY_LIMIT else "text"


def _first_timestamp(
    frame: pd.DataFrame, columns: list[GraphColumn]
) -> GraphColumn | None:
    """The column that orders a table's rows, if it has one."""
    for column in columns:
        if column.stype == "timestamp":
            return column
    for column in columns:
        if column.name.lower().endswith(("date", "time", "at")) and pd.api.types.is_object_dtype(
            frame[column.name].dtype
        ):
            converted = pd.to_datetime(frame[column.name], errors="coerce")
            if converted.notna().mean() > 0.9:
                return column
    return None


# How many child values to check against a parent key before trusting a link.
_OVERLAP_SAMPLE = 500
# Share of the sampled child values that must exist in the parent key.
_OVERLAP_FLOOR = 0.8


def infer_links_by_name(
    graph: FramedGraph, frames: dict[str, pd.DataFrame]
) -> int:
    """Link tables that carry the same key column, when the values agree.

    The catalog sometimes brings no join paths — the prediction is routed before
    any join traversal ran. Without links, no event table can be attributed to an
    entity and every prediction is refused, so a conservative fallback is needed.

    This is stricter than the engine's own inference: a link needs a column with
    the parent key's name *and* sampled child values that actually appear in that
    key. A same-named column in an unrelated table fails the value check, and a
    table without a single-column key is never a destination.

    Returns the number of links added.
    """
    # One edge per foreign key: a warehouse that keeps both a live and an
    # archived copy of a dimension offers two destinations for the same column,
    # and stating both would split the model's attention across a duplicate.
    best: dict[tuple[str, str], tuple[float, str, str]] = {}
    for src_name, src in graph.tables.items():
        for dst_name, dst in graph.tables.items():
            if src_name == dst_name:
                continue
            keys = dst.primary_key_columns
            if len(keys) != 1:
                continue
            key = keys[0]
            column = next(
                (c.name for c in src.columns if c.name.lower() == key.lower()), None
            )
            if column is None:
                continue
            # A foreign key usually carries the parent key's own name, so a match
            # alone says nothing. What rules a column out is being the source's
            # *own* single-column key: two independently numbered tables that both
            # call it ``id`` would otherwise look related.
            src_keys = src.primary_key_columns
            if len(src_keys) == 1 and src_keys[0].lower() == column.lower():
                continue
            left = frames.get(src_name, pd.DataFrame()).get(column)
            right = frames.get(dst_name, pd.DataFrame()).get(key)
            if left is None or right is None:
                continue
            share = _overlap_share(left, right)
            if share < _OVERLAP_FLOOR:
                continue
            slot = (src_name, column.lower())
            if slot not in best or share > best[slot][0]:
                best[slot] = (share, dst_name, column)

    added = 0
    for (src_name, _column), (_share, dst_name, column) in best.items():
        before = len(graph.edges)
        try:
            graph.link(src_name, column, dst_name)
        except KeyError:
            continue
        if len(graph.edges) > before:
            added += 1
    return added


def _values_agree(left: pd.Series, right: pd.Series) -> bool:
    """Whether the child values are plausibly drawn from the parent key."""
    return _overlap_share(left, right) >= _OVERLAP_FLOOR


def _overlap_share(left: pd.Series, right: pd.Series) -> float:
    """Share of the sampled child values that exist in the parent key."""
    present = left.dropna()
    keys = right.dropna()
    if not len(present) or not len(keys):
        return 0.0
    sample = present.drop_duplicates()
    if len(sample) > _OVERLAP_SAMPLE:
        sample = sample.head(_OVERLAP_SAMPLE)
    return float(sample.isin(set(keys.head(100_000))).mean())
