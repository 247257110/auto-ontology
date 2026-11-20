# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Check an exported Ossie document before anyone reads it as the truth.

Rendering the YAML is the easy half of "browse the Ossie file". The half that
earns its place is telling a reviewer *what is wrong or missing in it*, because
everything it reports is invisible by reading:

* a relationship naming a field that does not exist on its dataset looks exactly
  like one that does — the document still reads cleanly;
* two metrics with one name cannot both be imported, and the failure arrives
  later as a 422 on a step nobody associated with browsing;
* a Term representing several tables is silently narrowed to one on export
  (``_project_terms_onto_one_table``), so the exported model describes less than
  the catalog does;
* whole object types have no Ossie representation at all — SQL Attributes,
  semantic foreign keys, zones — so a reader of the YAML would conclude the
  ontology is smaller than it is.

The findings are deliberately few and each is a statement about *this* document,
not advice. On a consistent export this returns an empty list, which is what
makes it usable as a regression gate as well as a UI panel.

Everything here operates on the exported text (what a reviewer actually sees),
plus optionally the native document it came from — the only place the
information that Ossie drops still exists.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

import yaml

logger = logging.getLogger(__name__)

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

#: Stable identifiers the UI can group by without matching on message text.
CODE_UNPARSEABLE = "unparseable"
CODE_DANGLING_DATASET = "relationship_dangling_dataset"
CODE_DANGLING_FIELD = "relationship_dangling_field"
CODE_DUPLICATE_METRIC = "duplicate_metric_name"
CODE_EMPTY_DATASET = "dataset_without_fields"
CODE_PROJECTED_TERM = "term_projected_onto_one_table"
CODE_NOT_EXPRESSIBLE = "not_expressible_in_ossie"


@dataclass(frozen=True)
class Finding:
    """One thing a reviewer should know before trusting the document."""

    severity: str
    code: str
    subject: str
    message: str
    #: Approximate 1-based line of the subject in the exported text, for a
    #: "jump to it" affordance. ``None`` when the subject could not be located,
    #: which is a limitation of text scanning rather than a statement that the
    #: finding is less certain.
    line: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "subject": self.subject,
            "message": self.message,
            "line": self.line,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Report:
    """Everything the check found, plus the shape of what it looked at."""

    findings: tuple[Finding, ...] = ()
    datasets: int = 0
    relationships: int = 0
    metrics: int = 0
    #: Total fields across every dataset — what a "structure" sidebar counts.
    fields: int = 0

    @property
    def ok(self) -> bool:
        """No errors. Warnings and notes do not block a review."""
        return not any(item.severity == SEVERITY_ERROR for item in self.findings)

    def counts(self) -> dict[str, int]:
        counted = Counter(item.severity for item in self.findings)
        return {
            severity: counted.get(severity, 0)
            for severity in (
                SEVERITY_ERROR,
                SEVERITY_WARNING,
                SEVERITY_INFO,
            )
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "counts": self.counts(),
            "shape": {
                "datasets": self.datasets,
                "fields": self.fields,
                "relationships": self.relationships,
                "metrics": self.metrics,
            },
            "findings": [item.as_dict() for item in self.findings],
        }


def _line_of(text: str, needle: str) -> int | None:
    """First 1-based line declaring *needle* as a YAML ``name``, else ``None``.

    Text scanning rather than a source-aware parse: PyYAML discards positions.
    Both the mapping form (``name: x``) and the sequence-item form
    (``- name: x``) count — datasets, relationships and metrics are all list
    entries. A miss is reported as ``None`` instead of a guessed line.
    """
    if not needle:
        return None
    for form in (f"name: {needle}", f"name: '{needle}'", f'name: "{needle}"'):
        for number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("- "):
                stripped = stripped[2:].lstrip()
            if stripped == form:
                return number
    return None


def _dataset_fields(document: dict[str, Any]) -> dict[str, set[str]]:
    return {
        str(dataset.get("name")): {
            str(field.get("name"))
            for field in dataset.get("fields") or []
            if isinstance(field, dict) and field.get("name")
        }
        for dataset in document.get("datasets") or []
        if isinstance(dataset, dict) and dataset.get("name")
    }


def _check_relationships(
    document: dict[str, Any],
    fields_by_dataset: dict[str, set[str]],
    text: str,
) -> Iterable[Finding]:
    for relationship in document.get("relationships") or []:
        if not isinstance(relationship, dict):
            continue
        name = str(relationship.get("name") or "")
        line = _line_of(text, name)
        for side in ("from", "to"):
            dataset = relationship.get(side)
            if dataset not in fields_by_dataset:
                yield Finding(
                    severity=SEVERITY_ERROR,
                    code=CODE_DANGLING_DATASET,
                    subject=name,
                    message=(
                        f"关系 {name!r} 的 {side} 端引用了数据集 {dataset!r}，"
                        "但文档里没有这个数据集。"
                    ),
                    line=line,
                    detail={"side": side, "dataset": dataset},
                )
                continue
            missing = [
                column
                for column in relationship.get(f"{side}_columns") or []
                if str(column) not in fields_by_dataset[dataset]
            ]
            if missing:
                yield Finding(
                    severity=SEVERITY_ERROR,
                    code=CODE_DANGLING_FIELD,
                    subject=name,
                    message=(
                        f"关系 {name!r} 的 {side} 端引用了数据集 {dataset!r} 上不存在的字段："
                        f"{', '.join(map(str, missing))}。"
                    ),
                    line=line,
                    detail={
                        "side": side,
                        "dataset": dataset,
                        "missing": missing,
                    },
                )


def _check_metrics(document: dict[str, Any], text: str) -> Iterable[Finding]:
    counts = Counter(
        str(metric.get("name"))
        for metric in document.get("metrics") or []
        if isinstance(metric, dict) and metric.get("name")
    )
    for name, count in counts.items():
        if count > 1:
            yield Finding(
                severity=SEVERITY_ERROR,
                code=CODE_DUPLICATE_METRIC,
                subject=name,
                message=(
                    f"指标 {name!r} 出现了 {count} 次。Ossie 的指标名是全局唯一的，"
                    "重名会导致导入失败。"
                ),
                line=_line_of(text, name),
                detail={"count": count},
            )


def _check_empty_datasets(
    fields_by_dataset: dict[str, set[str]], text: str
) -> Iterable[Finding]:
    for name, fields in fields_by_dataset.items():
        if not fields:
            yield Finding(
                severity=SEVERITY_WARNING,
                code=CODE_EMPTY_DATASET,
                subject=name,
                message=f"数据集 {name!r} 没有任何字段，对应关系无法挂到它上面。",
                line=_line_of(text, name),
            )


def _check_native_losses(native: Any, text: str) -> Iterable[Finding]:
    """Report what the export had to leave behind, from the native document.

    Only the native model knows: the Ossie text is the *result* of dropping
    these, so it cannot report their absence.
    """
    semantic = getattr(native, "semantic_layer", None)
    if semantic is None:
        return

    for term in getattr(semantic, "terms", []) or []:
        represents = list(getattr(term, "represents", []) or [])
        if len(represents) <= 1:
            continue
        yield Finding(
            severity=SEVERITY_INFO,
            code=CODE_PROJECTED_TERM,
            subject=str(getattr(term, "name", "")),
            message=(
                f"词条 {getattr(term, 'name', '')!r} 代表 {len(represents)} 张表，"
                "Ossie 的数据集只能对应一张表，导出时被压平成一张，"
                "其余表的列属性不会出现在文档里。"
            ),
            line=_line_of(text, str(getattr(term, "name", ""))),
            detail={"represents": len(represents)},
        )

    sql_attributes = getattr(semantic, "sql_attributes", None)
    by_source = (
        {
            source: len(getattr(sql_attributes, source, []) or [])
            for source in ("manual", "table", "sql", "bridge_table")
        }
        if sql_attributes is not None
        else {}
    )
    total_sql_attributes = sum(by_source.values())
    semantic_fks = list(getattr(semantic, "semantic_fks", []) or [])

    if total_sql_attributes or semantic_fks:
        parts = []
        if total_sql_attributes:
            parts.append(f"{total_sql_attributes} 条 SQL Attribute（口径公式）")
        if semantic_fks:
            parts.append(f"{len(semantic_fks)} 条语义外键")
        yield Finding(
            severity=SEVERITY_INFO,
            code=CODE_NOT_EXPRESSIBLE,
            subject="Ossie",
            message=(
                f"本库另有 {'、'.join(parts)}，Ossie 文档里没有对应表示。"
                "它们是问答流程实际使用的内容，读这份文件时不要以为本体只有这么小。"
            ),
            detail={
                "sql_attributes": by_source,
                "semantic_fks": len(semantic_fks),
            },
        )


def check_ossie_text(
    ossie_yaml: str,
    *,
    native: Any | None = None,
) -> Report:
    """Check *ossie_yaml* for the defects reading the file cannot reveal.

    *native*, when given, is the :class:`AutoOntologyModelDocument` the text was
    produced from — the only source for what the Ossie conversion dropped.
    """
    if not ossie_yaml or not ossie_yaml.strip():
        return Report(
            findings=(
                Finding(
                    severity=SEVERITY_ERROR,
                    code=CODE_UNPARSEABLE,
                    subject="document",
                    message="导出的文档是空的。",
                ),
            )
        )

    try:
        document = yaml.safe_load(ossie_yaml)
    except yaml.YAMLError as exc:
        return Report(
            findings=(
                Finding(
                    severity=SEVERITY_ERROR,
                    code=CODE_UNPARSEABLE,
                    subject="document",
                    message=f"文档不是合法的 YAML：{exc}",
                ),
            )
        )
    if not isinstance(document, dict):
        return Report(
            findings=(
                Finding(
                    severity=SEVERITY_ERROR,
                    code=CODE_UNPARSEABLE,
                    subject="document",
                    message="文档的顶层不是一个映射，不可能是 Ossie 语义模型。",
                ),
            )
        )

    fields_by_dataset = _dataset_fields(document)
    findings: list[Finding] = []
    findings.extend(_check_relationships(document, fields_by_dataset, ossie_yaml))
    findings.extend(_check_metrics(document, ossie_yaml))
    findings.extend(_check_empty_datasets(fields_by_dataset, ossie_yaml))
    if native is not None:
        findings.extend(_check_native_losses(native, ossie_yaml))

    return Report(
        findings=tuple(findings),
        datasets=len(fields_by_dataset),
        fields=sum(len(fields) for fields in fields_by_dataset.values()),
        relationships=len(document.get("relationships") or []),
        metrics=len(document.get("metrics") or []),
    )


__all__ = [
    "CODE_DANGLING_DATASET",
    "CODE_DANGLING_FIELD",
    "CODE_DUPLICATE_METRIC",
    "CODE_EMPTY_DATASET",
    "CODE_NOT_EXPRESSIBLE",
    "CODE_PROJECTED_TERM",
    "CODE_UNPARSEABLE",
    "Finding",
    "Report",
    "check_ossie_text",
]
