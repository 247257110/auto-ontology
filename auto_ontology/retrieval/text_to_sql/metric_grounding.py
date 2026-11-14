# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Ground user-language metrics against already retrieved semantic objects."""

from __future__ import annotations

import re
from typing import Any

from auto_ontology.semantic.chinese import normalize_chinese_text


def _candidate_text(candidate: dict[str, Any]) -> str:
    values = [
        candidate.get("name"),
        candidate.get("term_name"),
        candidate.get("description"),
        candidate.get("expression"),
        candidate.get("sql"),
        candidate.get("text"),
        candidate.get("source_column"),
    ]
    synonyms = candidate.get("synonyms") or []
    if isinstance(synonyms, str):
        values.append(synonyms)
    else:
        values.extend(synonyms)
    return normalize_chinese_text(" ".join(str(value or "") for value in values))


def _score_metric(metric: str, candidate: dict[str, Any]) -> float:
    query = normalize_chinese_text(metric).casefold()
    if not query:
        return 0.0
    name = normalize_chinese_text(
        " ".join(
            str(candidate.get(key) or "")
            for key in ("name", "term_name", "source_column")
        )
    ).casefold()
    text = _candidate_text(candidate).casefold()
    if query == name or query in name.split():
        return 1.0
    if query in text:
        return 0.85
    tokens = set(re.findall(r"[a-z0-9_]+", query))
    tokens.update(ch for ch in query if "\u4e00" <= ch <= "\u9fff")
    candidate_tokens = set(re.findall(r"[a-z0-9_]+", text))
    candidate_tokens.update(ch for ch in text if "\u4e00" <= ch <= "\u9fff")
    return len(tokens & candidate_tokens) / len(tokens) if tokens else 0.0


def _annotate(candidate: dict[str, Any], kind: str, score: float) -> dict[str, Any]:
    return {
        "kind": kind,
        "id": candidate.get("id"),
        "name": candidate.get("name") or candidate.get("source_column") or "",
        "score": round(score, 4),
        "description": candidate.get("description") or "",
        "expression": candidate.get("expression") or "",
        "sql": candidate.get("sql") or "",
    }


def ground_metric_semantics(
    semantic_ir: dict[str, Any] | None,
    *,
    sql_attributes: list[dict[str, Any]] | None = None,
    custom_analyses: list[dict[str, Any]] | None = None,
    column_attributes: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return explainable metric-to-ontology matches.

    SQL Attributes and Custom Analyses outrank plain ColumnAttributes because
    they carry governed formulas. No candidate is fabricated when the metric
    has no lexical evidence in the retrieved semantic context.
    """
    if not semantic_ir:
        return []
    candidate_groups = (
        ("sql_attribute", sql_attributes or [], 3),
        ("custom_analysis", custom_analyses or [], 2),
        ("column_attribute", column_attributes or [], 1),
    )
    grounded: list[dict[str, Any]] = []
    for spec in semantic_ir.get("metric_specs") or []:
        metric_name = str(spec.get("name") or "").strip()
        if not metric_name:
            continue
        matches: list[tuple[int, dict[str, Any]]] = []
        for kind, candidates, priority in candidate_groups:
            for candidate in candidates:
                score = _score_metric(metric_name, candidate)
                if score >= 0.55:
                    matches.append((priority, _annotate(candidate, kind, score)))
        matches.sort(
            key=lambda item: (
                -(item[1]["score"] + item[0] * 0.1),
                -item[1]["score"],
                str(item[1]["name"]),
            )
        )
        annotations = [item[1] for item in matches[:5]]
        governed = [
            item
            for item in annotations
            if item["kind"] in {"sql_attribute", "custom_analysis"}
        ]
        selected = (
            governed[0] if governed else (annotations[0] if annotations else None)
        )
        ambiguous = bool(
            len(governed) > 1 and governed[0]["score"] - governed[1]["score"] < 0.15
        )
        grounded.append(
            {
                "name": metric_name,
                "aggregation": spec.get("aggregation", "unknown"),
                "unit": spec.get("unit"),
                "comparison": spec.get("comparison", "none"),
                "status": (
                    "ambiguous"
                    if ambiguous
                    else "governed"
                    if governed
                    else "candidate"
                    if annotations
                    else "unresolved"
                ),
                "confidence": (
                    0.0
                    if not annotations
                    else 0.45
                    if ambiguous
                    else min(0.99, round(0.55 + (selected["score"] * 0.4), 4))
                    if selected
                    else 0.0
                ),
                "reason": (
                    "未找到匹配的本体指标或字段"
                    if not annotations
                    else "多个治理指标分数接近，需要用户选择"
                    if ambiguous
                    else "已匹配到治理指标定义"
                    if governed
                    else "仅匹配到普通字段候选，缺少治理公式"
                ),
                "selected": selected,
                "matches": annotations,
            }
        )
    return grounded


def build_metric_clarification_question(
    grounded_metrics: list[dict[str, Any]] | None,
) -> str | None:
    """Build one focused Chinese question for an unsafe metric grounding."""
    for metric in grounded_metrics or []:
        name = str(metric.get("name") or "").strip()
        status = metric.get("status")
        if not name or status not in {"unresolved", "ambiguous"}:
            continue
        matches = metric.get("matches") or []
        if status == "ambiguous" and matches:
            choices = "、".join(
                str(item.get("name") or "未命名指标") for item in matches[:3]
            )
            return (
                f"指标“{name}”对应多个口径（{choices}）。请确认要使用哪一个指标，"
                "或补充它的业务定义/计算公式。"
            )
        return (
            f"暂未找到指标“{name}”的明确治理定义。请补充该指标的业务含义、"
            "对应字段，或提供计算公式后再查询。"
        )
    return None


__all__ = ["build_metric_clarification_question", "ground_metric_semantics"]
