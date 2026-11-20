# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Semantic-layer reindex orchestration.

Rebuilds the ``vdb.semantic_layer`` rows for the five semantic object types —
Term, ColumnAttribute, SqlAttribute, CustomAnalysis and PqlAnalysis — so that
edits to descriptions, synonyms, or certification flags that were made after
the original compile actually land in the vectors used for retrieval.

The write order matters and is deliberately *embed-first*: every row is
embedded before any old row is deleted, so a failing embedding endpoint leaves
the previous vectors untouched rather than a half-rebuilt index.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from auto_ontology.catalog.constants import Labels
from auto_ontology.dal.custom_analyses import (
    _custom_analysis_docs,
    fetch_database_name_for_analysis,
)
from auto_ontology.dal.datasources import fetch_databases
from auto_ontology.dal.pql_analyses import _pql_analysis_docs
from auto_ontology.dal.sql_attributes import (
    _query_sql_attributes,
    fetch_sql_attribute_databases,
)
from auto_ontology.dal.terms import fetch_terms_and_attributes_for_database
from auto_ontology.semantic.constants import LABEL_PQL_ANALYSIS, LABEL_SQL_ATTRIBUTE
from auto_ontology.semantic.embed import _build_rows
from auto_ontology.utils.embedding import embed_rows, get_embed_params
from auto_ontology.utils.model_config import resolve
from auto_ontology.vdb import get_semantic_vdb
from auto_ontology.vdb.postgres import PostgresVDB
from auto_ontology.vdb.records import write_embedded_rows

logger = logging.getLogger(__name__)

# The semantic labels the reindex can rebuild, in a stable report order.
LABEL_TERM = "Term"
LABEL_COLUMN_ATTRIBUTE = "ColumnAttribute"
SEMANTIC_LABELS = (
    LABEL_TERM,
    LABEL_COLUMN_ATTRIBUTE,
    LABEL_SQL_ATTRIBUTE,
    Labels.CUSTOM_ANALYSIS,
    LABEL_PQL_ANALYSIS,
)


def _doc_row(doc: dict[str, Any], database_name: str | None) -> dict[str, Any]:
    """Turn a doc (``id``/``label``/``name``/``text``) into an embed-ready row.

    Mirrors the metadata shape written by
    :func:`auto_ontology.utils.embedding.embed_docs_into_vdb` so rows built here
    are indistinguishable from rows written on create/update.
    """
    node_id = doc.get("id")
    path = (
        f"auto_ontology:{node_id}" if node_id is not None else "auto_ontology:unknown"
    )
    tabular_fields = {
        "id": node_id,
        "label": doc.get("label", ""),
        "name": doc.get("name", ""),
        "source_path": path,
        "database_name": database_name,
        "source": doc.get("source", ""),
        "certified": bool(doc.get("certified", False)),
    }
    return {
        "text": (doc.get("text") or "").strip(),
        "path": path,
        "page_number": -1,
        "metadata": {**tabular_fields, "content_metadata": dict(tabular_fields)},
    }


def _rows_for_terms_and_attributes(database_name: str) -> list[dict[str, Any]]:
    """Embed-ready Term + ColumnAttribute rows for one database."""
    terms, attrs = fetch_terms_and_attributes_for_database(database_name)
    attrs_by_term: dict[str, list[dict[str, Any]]] = {}
    for attr in attrs:
        term_name = attr.get("term_name") or ""
        if term_name:
            attrs_by_term.setdefault(term_name, []).append(attr)

    rows: list[dict[str, Any]] = []
    for term in terms:
        term_name = term.get("name") or ""
        if not term_name:
            continue
        rows.extend(
            _build_rows(
                database_name,
                term,
                attrs_by_term.get(term_name, []),
            )
        )
    return rows


def _rows_for_sql_attributes(
    database_names: set[str] | None,
) -> list[dict[str, Any]]:
    """Embed-ready SqlAttribute rows, scoped by database when requested."""
    attrs = _query_sql_attributes()
    if not attrs:
        return []

    attr_ids = [row["id"] for row in attrs]
    database_by_id = fetch_sql_attribute_databases(attr_ids)

    rows: list[dict[str, Any]] = []
    for row in attrs:
        attr_databases = database_by_id.get(row["id"]) or set()
        if database_names is not None and not (attr_databases & database_names):
            continue
        # A definition spanning several databases cannot be scoped to one in
        # the VDB; ``None`` keeps it global, and the consistency check reports
        # the span separately.
        database_name = next(iter(attr_databases)) if len(attr_databases) == 1 else None
        description = row.get("description")
        text = f"sql_attribute: {row['name']}"
        if description and str(description).strip():
            text += f", description: {description}"
        if row.get("term_name") is not None:
            text += f", term: {row['term_name']}"
        text += f", sql: {row.get('sql') or ''}"
        rows.append(
            _doc_row(
                {
                    "id": row["id"],
                    "label": LABEL_SQL_ATTRIBUTE,
                    "name": row["name"],
                    "text": text,
                    "source": row.get("source") or "",
                    "certified": row.get("certified", False),
                },
                database_name,
            )
        )
    return rows


def _rows_for_custom_analyses(
    database_names: set[str] | None,
) -> list[dict[str, Any]]:
    """Embed-ready CustomAnalysis rows, scoped by database when requested."""
    docs = _custom_analysis_docs(None)
    rows: list[dict[str, Any]] = []
    for doc in docs:
        database_name = fetch_database_name_for_analysis(doc["id"])
        if database_names is not None and database_name not in database_names:
            continue
        rows.append(_doc_row(doc, database_name))
    return rows


def _rows_for_pql_analyses() -> list[dict[str, Any]]:
    """Embed-ready PqlAnalysis rows. PQL analyses are global (no database)."""
    # ``_pql_analysis_docs`` omits the label (its writer hardcodes it), so add
    # it here for the shared ``_doc_row`` shape.
    return [
        _doc_row({**doc, "label": LABEL_PQL_ANALYSIS}, None)
        for doc in _pql_analysis_docs(None)
    ]


def _build_all_rows(
    database_names: set[str] | None,
    labels: set[str],
) -> list[dict[str, Any]]:
    """All embed-ready semantic rows for the requested scope."""
    rows: list[dict[str, Any]] = []
    if LABEL_TERM in labels or LABEL_COLUMN_ATTRIBUTE in labels:
        for db in database_names or {d["name"] for d in fetch_databases()}:
            rows.extend(_rows_for_terms_and_attributes(db))
    if LABEL_SQL_ATTRIBUTE in labels:
        rows.extend(_rows_for_sql_attributes(database_names))
    if Labels.CUSTOM_ANALYSIS in labels:
        rows.extend(_rows_for_custom_analyses(database_names))
    if LABEL_PQL_ANALYSIS in labels:
        rows.extend(_rows_for_pql_analyses())

    # ``_rows_for_terms_and_attributes`` emits a Term row and its
    # ColumnAttribute rows together, so a label-scoped rebuild still has to
    # build both before filtering down to exactly what was requested.
    return [row for row in rows if (row.get("metadata") or {}).get("label") in labels]


def _count_rows(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Count embed-ready rows by label."""
    counts: dict[str, int] = {}
    for row in rows:
        label = str((row.get("metadata") or {}).get("label") or "")
        counts[label] = counts.get(label, 0) + 1
    return counts


def _vdb_row_count(vdb: PostgresVDB) -> int:
    """Total rows currently in the semantic collection, or 0 if absent."""
    if not vdb._table_exists():
        return 0
    import psycopg
    from psycopg import sql

    with psycopg.connect(vdb.connection_string) as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("SELECT COUNT(*) FROM {table}").format(
                    table=sql.Identifier(vdb.schema_name, vdb.collection_name)
                )
            )
            return int(cur.fetchone()[0])


def reindex_semantic_nodes(
    *,
    databases: list[str] | None = None,
    labels: list[str] | None = None,
    node_ids: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Preview or execute a semantic-layer rebuild.

    Returns a report shaped ``{dry_run, elapsed_ms, labels, total, ...}``. When
    ``dry_run`` is true nothing is written — the report only describes what a
    real run would rebuild. When the embedding endpoint is unavailable the run
    refuses loudly (``skipped: true`` + a reason) rather than silently
    reporting success.
    """
    started = time.monotonic()
    label_filter = set(labels) if labels else set(SEMANTIC_LABELS)
    database_filter = set(databases) if databases else None

    rows = _build_all_rows(database_filter, label_filter)
    if node_ids:
        wanted = set(node_ids)
        rows = [r for r in rows if str((r.get("metadata") or {}).get("id")) in wanted]

    report: dict[str, Any] = {
        "dry_run": dry_run,
        "labels": _count_rows(rows),
        "total": len(rows),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }

    if dry_run:
        return report

    if not resolve("EMBED", "API_KEY"):
        report["skipped"] = True
        report["reason"] = "EMBED_API_KEY / DEFAULT_MODELS_API_KEY is not set"
        return report

    if not rows:
        report["skipped"] = True
        report["reason"] = "no semantic nodes matched the requested scope"
        return report

    # Embed everything before touching the store. A failed embed leaves the
    # existing vectors in place, so a real run either replaces every scoped row
    # or none of them.
    try:
        embedded = embed_rows(rows, get_embed_params(), what="semantic-reindex")
    except RuntimeError as exc:
        report["skipped"] = True
        report["reason"] = str(exc)
        report["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return report

    vdb = get_semantic_vdb()
    before = _vdb_row_count(vdb)

    # Delete the old rows for every node id we just rebuilt, then append the
    # fresh ones. Re-embedding the same id replaces its row; ids that were
    # duplicated in the store collapse to a single row.
    node_ids_seen: set[str] = set()
    for row in embedded:
        node_id = (row.get("metadata") or {}).get("id")
        if node_id is not None and node_id not in node_ids_seen:
            node_ids_seen.add(node_id)
            vdb.delete_by_id(node_id)
    written = write_embedded_rows(vdb, embedded)

    after = _vdb_row_count(vdb)
    report.update(
        {
            "written": written,
            "deleted_node_ids": len(node_ids_seen),
            "rows_before": before,
            "rows_after": after,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
    )
    return report
