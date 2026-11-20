# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Chinese semantic understanding for the ReAct text-to-SQL agent.

The ReAct agent lets the model choose its own retrieval, which is what makes it
flexible — but a Chinese calculation question carries business semantics that a
table/column search cannot recover on its own: "营收" vs "含税营收", "最近 30
天" vs "本月", or a metric whose governed formula lives in a certified SQL
Attribute. This module builds the language-preserving semantic IR before the
episode and grounds its metrics against the ontology, so the model starts with
the definition instead of guessing one from column names.

Nothing here writes SQL or picks tables; a question the ontology cannot settle
comes back as ``clarification`` and the caller asks the user rather than
inventing a formula.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from auto_ontology.catalog.constants import Labels
from auto_ontology.dal.sql_attributes import fetch_sql_attributes_with_sql
from auto_ontology.retrieval.data_access.semantic_search import search_semantic_index
from auto_ontology.retrieval.text_to_sql.metric_grounding import (
    build_metric_clarification_question,
    ground_metric_semantics,
)
from auto_ontology.retrieval.text_to_sql.semantic_ir import (
    SemanticIR,
    build_semantic_ir,
)
from auto_ontology.semantic.chinese import contains_cjk
from auto_ontology.semantic.constants import (
    LABEL_COLUMN_ATTRIBUTE,
    LABEL_SQL_ATTRIBUTE,
)

logger = logging.getLogger(__name__)

#: Hits kept per semantic label before grounding. Attributes get the most:
#: a metric definition has to compete with every column that mentions it.
_PER_LABEL_K: dict[str, int] = {
    LABEL_SQL_ATTRIBUTE: 8,
    Labels.CUSTOM_ANALYSIS: 5,
    LABEL_COLUMN_ATTRIBUTE: 12,
}


@dataclass(frozen=True)
class SemanticContext:
    """What the Chinese question means, and what the ontology says it is.

    ``ir`` is the language-preserving intermediate representation, ``grounded_metrics``
    the matches against retrieved SQL Attributes / Custom Analyses / Column
    Attributes, and ``clarification`` the one question to ask when a measure
    cannot be settled safely.
    """

    ir: dict[str, Any] | None = None
    grounded_metrics: list[dict[str, Any]] = field(default_factory=list)
    clarification: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.ir and not self.grounded_metrics


def build_semantic_context(
    question: str,
    retriever: Any,
    *,
    database_name: str | None = None,
) -> SemanticContext:
    """Build the IR and ground its metrics, or return an empty context.

    Only Chinese questions get an IR: the parser is a Chinese-specific
    fallback, and running it on English would turn ordinary words into metric
    phrases. Retrieval and grounding are best-effort — a failure leaves the
    ReAct agent exactly as it was, rather than blocking the question.
    """
    if not question or retriever is None or not contains_cjk(question):
        return SemanticContext()

    try:
        ir = build_semantic_ir(question, question_type="calculation")
    except Exception:  # noqa: BLE001 - the IR is an enhancement, not a gate
        logger.warning("semantic IR construction failed", exc_info=True)
        return SemanticContext()

    payload = ir.model_dump(mode="json")
    if not ir.metrics and not ir.dimensions:
        return SemanticContext(ir=payload)

    try:
        candidates = _retrieve_candidates(ir, retriever, database_name)
        grounded = ground_metric_semantics(payload, **candidates)
    except Exception:  # noqa: BLE001
        logger.warning("metric grounding failed", exc_info=True)
        return SemanticContext(ir=payload)

    return SemanticContext(
        ir=payload,
        grounded_metrics=grounded,
        clarification=build_metric_clarification_question(grounded),
    )


def _retrieve_candidates(
    ir: SemanticIR,
    retriever: Any,
    database_name: str | None,
) -> dict[str, list[dict[str, Any]]]:
    """Search each semantic label once per phrase the question names."""
    queries = ir.retrieval_entities() or [ir.question]
    hits: dict[str, list[dict[str, Any]]] = {}
    attempted = succeeded = 0
    for label, limit in _PER_LABEL_K.items():
        collected: list[dict[str, Any]] = []
        for query in queries:
            attempted += 1
            try:
                collected.extend(
                    search_semantic_index(
                        retriever,
                        query,
                        label_filter=[label],
                        per_label_k=limit,
                        database_name=database_name,
                    )
                )
                succeeded += 1
            except Exception:  # noqa: BLE001 - one label failing is not fatal
                logger.warning(
                    "semantic search failed for label %s and query %r",
                    label,
                    query,
                    exc_info=True,
                )
        hits[label] = _dedupe(collected)

    if attempted and not succeeded:
        # Every search failed. That is an unavailable index, not "the ontology
        # has no definition": leaving the IR ungrounded and unblocked is what
        # lets the ReAct agent fall back to its normal catalog search.
        raise RuntimeError("semantic search is unavailable")

    sql_attributes = _with_sql_details(hits.get(LABEL_SQL_ATTRIBUTE) or [])
    return {
        "sql_attributes": sql_attributes,
        "custom_analyses": hits.get(Labels.CUSTOM_ANALYSIS) or [],
        "column_attributes": hits.get(LABEL_COLUMN_ATTRIBUTE) or [],
    }


def _dedupe(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One hit per id, keeping the closest (lowest score) match."""
    best: dict[str, dict[str, Any]] = {}
    for hit in hits:
        key = str(hit.get("id") or "")
        if not key:
            continue
        score = hit.get("score")
        previous = best.get(key)
        if previous is None:
            best[key] = hit
            continue
        previous_score = previous.get("score")
        if score is None:
            continue
        if previous_score is None or float(score) < float(previous_score):
            best[key] = hit
    return list(best.values())


def _with_sql_details(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge the stored formula into each search hit.

    The VDB row carries the name and description that matched; the formula and
    SQL live on the SQL Attribute. Grounding needs both, so the hit is kept and
    the stored fields are laid over it.
    """
    ids = [str(hit.get("id")) for hit in hits if hit.get("id")]
    details = {
        str(row.get("id")): row
        for row in fetch_sql_attributes_with_sql(list(dict.fromkeys(ids)))
    }
    merged: list[dict[str, Any]] = []
    for hit in hits:
        row = dict(hit)
        stored = details.get(str(hit.get("id")))
        if stored:
            for key, value in stored.items():
                if value not in (None, ""):
                    row[key] = value
        merged.append(row)
    return merged


def format_semantic_context(context: SemanticContext | None) -> str:
    """Render the IR and its grounding as prompt sections the agent can use."""
    if context is None or context.is_empty:
        return ""
    return format_metric_semantics_section(
        {
            "semantic_ir": context.ir or {},
            "grounded_metrics": context.grounded_metrics,
        }
    ).strip()


def format_metric_semantics_section(path_state: dict[str, Any]) -> str:
    """Render Chinese metric intent without pretending it is a schema mapping."""
    ir = path_state.get("semantic_ir") or {}
    specs = ir.get("metric_specs") or []
    if not specs:
        return ""
    lines = [
        "## Metric semantic intent",
        "These are user-language metric semantics, not physical column names.",
        "Prefer a retrieved governed SQL Attribute/Custom Analysis definition when "
        "available. If no definition exists, preserve the ambiguity and do not "
        "invent a formula.",
    ]
    for spec in specs:
        line = f"- {spec.get('name', '')}: aggregation={spec.get('aggregation', 'unknown')}"
        if spec.get("unit"):
            line += f", unit={spec['unit']}"
        if spec.get("comparison") and spec["comparison"] != "none":
            line += f", comparison={spec['comparison']}"
        if spec.get("formula_hint"):
            line += f", hint={spec['formula_hint']}"
        lines.append(line)
    grounded_metrics = path_state.get("grounded_metrics") or []
    if grounded_metrics:
        lines.append("Grounding against retrieved ontology objects:")
        for metric in grounded_metrics:
            selected = metric.get("selected") or {}
            label = "维度" if metric.get("role") == "dimension" else "指标"
            line = (
                f"- {label}「{metric.get('name', '')}」: "
                f"status={metric.get('status', 'unresolved')}"
            )
            if metric.get("qualifiers"):
                line += f", qualifiers={','.join(metric['qualifiers'])}"
            if selected:
                line += (
                    f", selected={selected.get('kind')}:{selected.get('name')}"
                    f" (score={selected.get('score')}"
                    f"{'，certified' if selected.get('certified') else ''})"
                )
                definition = (
                    selected.get("expression")
                    or selected.get("description")
                    or selected.get("sql")
                )
                if definition:
                    line += f"\n  Definition: {definition}"
            lines.append(line)
    for section in (
        format_metric_bindings_section(grounded_metrics),
        format_dimension_bindings_section(grounded_metrics),
    ):
        if section:
            lines.append("")
            lines.append(section.rstrip("\n"))
    if ir.get("ambiguities"):
        lines.append("Ambiguities to resolve from ontology evidence:")
        lines.extend(f"- {item}" for item in ir["ambiguities"])
    time_ranges = ir.get("time_ranges") or []
    if time_ranges:
        lines.append("Normalized time semantics:")
        lines.extend(
            f"- {item.get('expression', '')}: {item.get('canonical', 'unknown')}"
            for item in time_ranges
        )
    return "\n".join(lines) + "\n"


def format_dimension_bindings_section(
    grounded_metrics: list[dict[str, Any]] | None,
) -> str:
    """Render the certified *dimension* definitions that must be joined through.

    A dimension concept is a join path plus a grouping key, not a formula. So
    this section pins the path and the key while leaving the metric, filters
    and date range to the question.
    """
    required = [
        metric
        for metric in grounded_metrics or []
        if metric.get("binding") == "required" and metric.get("role") == "dimension"
    ]
    if not required:
        return ""

    lines = [
        "## Certified dimension definitions — use this join path and key",
        "Each dimension below has one certified definition. Join and group the way "
        "it does. The metric, the filters and the date range still come from the "
        "question — only the path and the grouping key are fixed.",
    ]
    for metric in required:
        selected = metric.get("selected") or {}
        definition = selected.get("expression") or selected.get("sql") or ""
        lines.append(
            f"- Dimension `{metric.get('name', '')}` "
            f"(certified definition `{selected.get('name', '')}`):"
        )
        if definition:
            lines.append(f"  ```sql\n  {definition.strip()}\n  ```")
        if selected.get("description"):
            lines.append(f"  Documented meaning: {selected['description']}")
    return "\n".join(lines) + "\n"


def format_metric_bindings_section(
    grounded_metrics: list[dict[str, Any]] | None,
) -> str:
    """Render the certified definitions that must be reused verbatim.

    A ``binding == "required"`` metric is one the ontology settles: exactly one
    certified SQL Attribute matched, it is clearly ahead of every alternative,
    and the question's own qualifier (if any) agrees with it.
    """
    required = [
        metric
        for metric in grounded_metrics or []
        if metric.get("binding") == "required" and metric.get("role") != "dimension"
    ]
    if not required:
        return ""

    lines = [
        "## Certified metric definitions — reuse these verbatim",
        "Each metric below has exactly one certified definition in the ontology. "
        "Use its expression as written: keep the same columns, joins, filters and "
        "NULLIF guards. Do not substitute an alternative formula, do not "
        "re-derive the metric from raw columns, and do not widen or narrow its "
        "scope.",
    ]
    for metric in required:
        selected = metric.get("selected") or {}
        name = metric.get("name", "")
        definition = selected.get("expression") or selected.get("sql") or ""
        lines.append(
            f"- Metric `{name}` (certified SQL Attribute `{selected.get('name', '')}`):"
        )
        if definition:
            lines.append(f"  ```sql\n  {definition.strip()}\n  ```")
        if selected.get("description"):
            lines.append(f"  Documented meaning: {selected['description']}")
    return "\n".join(lines) + "\n"


__all__ = [
    "SemanticContext",
    "build_semantic_context",
    "format_dimension_bindings_section",
    "format_metric_bindings_section",
    "format_metric_semantics_section",
    "format_semantic_context",
]
