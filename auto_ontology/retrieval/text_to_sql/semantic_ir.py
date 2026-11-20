# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""A small, language-preserving semantic intermediate representation.

The IR is deliberately independent from catalog ids and SQL identifiers.  It
captures what the user asked for before schema grounding: subject, measures,
dimensions, filters, time expressions, grain, ordering, and ambiguity signals.
It is a stable hand-off between intent classification, entity retrieval, and
SQL generation, with a deterministic fallback for Chinese questions.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from auto_ontology.semantic.chinese import contains_cjk, normalize_chinese_text

SemanticRole = Literal["subject", "metric", "dimension", "filter_value"]
FilterOperator = Literal[
    "=",
    "!=",
    ">",
    ">=",
    "<",
    "<=",
    "between",
    "contains",
]
MetricAggregation = Literal[
    "sum",
    "count",
    "avg",
    "min",
    "max",
    "ratio",
    "unknown",
]
MetricComparison = Literal["yoy", "mom", "qoq", "period_over_period", "none"]
TimeKind = Literal["calendar", "rolling", "relative", "unknown"]


class SemanticEntity(BaseModel):
    """A user phrase that should be grounded against the ontology."""

    text: str
    role: SemanticRole


class SemanticMetric(BaseModel):
    """A metric phrase plus the computation semantics expressed by the user."""

    name: str
    aggregation: MetricAggregation = "unknown"
    unit: str | None = None
    comparison: MetricComparison = "none"
    formula_hint: str | None = None


class SemanticFilter(BaseModel):
    """A filter expressed in user language, before column grounding."""

    field: str
    operator: FilterOperator
    value: str
    value_type: Literal["text", "number", "date", "unknown"] = "unknown"


class SemanticTimeRange(BaseModel):
    """A time phrase whose calendar/rolling meaning must be preserved."""

    expression: str
    kind: TimeKind
    canonical: str = "unknown"
    granularity: Literal["day", "week", "month", "quarter", "year", "unknown"] = (
        "unknown"
    )


class SemanticIR(BaseModel):
    """Language-neutral structure used between understanding and grounding."""

    language: Literal["zh", "mixed", "other"]
    question: str
    question_type: str
    calculation_subtype: str | None = None
    subject: str | None = None
    entities: list[SemanticEntity] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    metric_specs: list[SemanticMetric] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[SemanticFilter] = Field(default_factory=list)
    time_ranges: list[SemanticTimeRange] = Field(default_factory=list)
    granularity: str | None = None
    sort_by: str | None = None
    sort_order: Literal["asc", "desc"] | None = None
    limit: int | None = Field(default=None, ge=1)
    ambiguities: list[str] = Field(default_factory=list)

    def retrieval_entities(self) -> list[str]:
        """Return ordered, non-empty phrases useful for semantic retrieval."""
        values = [self.subject, *self.metrics, *self.dimensions]
        values.extend(entity.text for entity in self.entities)
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            normalized = normalize_chinese_text(value or "")
            key = normalized.casefold()
            if normalized and key not in seen:
                seen.add(key)
                result.append(normalized)
        return result


_TIME_PATTERNS: tuple[tuple[str, TimeKind, str], ...] = (
    (r"最近\s*\d+\s*天", "rolling", "day"),
    (r"最近\s*\d+\s*周", "rolling", "week"),
    (r"最近\s*\d+\s*个月", "rolling", "month"),
    (r"最近\s*\d+\s*年", "rolling", "year"),
    (r"去年|今年|前年", "calendar", "year"),
    (r"上个月|本月|下个月", "calendar", "month"),
    (r"上季度|本季度|下季度", "calendar", "quarter"),
    (r"上周|本周|下周", "calendar", "week"),
    (r"昨天|今天|明天", "calendar", "day"),
)

_METRIC_TERMS = (
    "收入",
    "营收",
    "销售额",
    "订单数",
    "订单量",
    "客户数",
    "用户数",
    "数量",
    "金额",
    "利润",
    "利润率",
    "成本",
    "客单价",
    "转化率",
    "占比",
    "平均值",
    "总额",
)
_FILTER_PATTERN = re.compile(
    r"(?P<field>[\u4e00-\u9fffA-Za-z_][\u4e00-\u9fffA-Za-z_0-9]{0,20})"
    r"\s*(?P<operator>大于|超过|高于|至少|小于|低于|最多|等于|不等于|>=|<=|>|<|=)"
    r"\s*(?P<value>[\d.,]+(?:%|万|亿)?|[^，。；;\s]{1,20})"
)


def _language(question: str) -> Literal["zh", "mixed", "other"]:
    has_cjk = contains_cjk(question)
    has_ascii = bool(re.search(r"[A-Za-z]", question))
    if has_cjk and has_ascii:
        return "mixed"
    return "zh" if has_cjk else "other"


def _metric_phrases(question: str) -> list[str]:
    matches = [term for term in _METRIC_TERMS if term in question]
    return [
        term
        for term in sorted(matches, key=len, reverse=True)
        if not any(term != other and term in other for other in matches)
    ]


def _metric_specs(question: str, metrics: list[str]) -> list[SemanticMetric]:
    specs: list[SemanticMetric] = []
    for metric in metrics:
        aggregation: MetricAggregation = "unknown"
        if metric.endswith(("率", "占比")) or re.search(r"百分比|比例", metric):
            aggregation = "ratio"
        elif re.search(r"平均|均值|人均", question):
            aggregation = "avg"
        elif metric.endswith(("数", "数量", "量")) or re.search(
            r"多少|几家|几笔|几位", question
        ):
            aggregation = "count"
        elif metric.endswith(("率", "占比")) or re.search(r"百分比|比例", question):
            aggregation = "ratio"
        elif re.search(r"总|合计", question):
            aggregation = "sum"

        unit: str | None = None
        if metric.endswith(("率", "占比")) or re.search(r"百分比|比例", question):
            unit = "percent"
        elif currency_match := re.search(r"万元|亿元|元", question):
            unit = f"currency:{currency_match.group(0)}"
        elif re.search(r"人均", question):
            unit = "per_person"
        elif metric.endswith(("数", "数量", "量")):
            unit = "count"

        comparison: MetricComparison = "none"
        if "同比" in question or "去年同期" in question:
            comparison = "yoy"
        elif "环比" in question:
            comparison = "mom"
        elif "季度对比" in question:
            comparison = "qoq"

        formula_hint = None
        explicit_formula = re.search(
            rf"{re.escape(metric)}\s*(?:是|为|=|等于)\s*([^，。；;]+)",
            question,
        )
        if explicit_formula:
            formula_hint = explicit_formula.group(1).strip()
        if aggregation == "ratio":
            formula_hint = formula_hint or (
                "ratio_or_percentage; use the governed metric definition"
            )
        elif "人均" in question and formula_hint is None:
            formula_hint = "per_person; use the governed metric definition"
        specs.append(
            SemanticMetric(
                name=metric,
                aggregation=aggregation,
                unit=unit,
                comparison=comparison,
                formula_hint=formula_hint,
            )
        )
    return specs


# A classifier between "每"/"各" and the noun it counts: "每个地区" is the
# dimension 地区, not 个地区. Left in, the counter becomes part of the phrase and
# grounding scores it against ontology objects that mention a count.
_DIMENSION_COUNTERS = "个|家|种|类|位|名|条|笔|张|份|项"


# Words that end a dimension phrase more often than they belong to one:
# "按客户类别统计订单数" is the dimension 客户类别, and "按品类看毛利率" is 品类.
# Truncated at the first one rather than stripped, since the rest is a verb
# phrase about the metric, not part of the noun.
_DIMENSION_STOPWORDS = (
    "统计数据",
    "统计",
    "计算",
    "来看",
    "看",
    "分别",
    "得到",
    "汇总",
    "排名",
    "排序",
    "的",
)


def _dimension_phrases(question: str) -> list[str]:
    metric_lookahead = "|".join(_METRIC_TERMS)
    match = re.search(
        rf"(?:按|按照|依|以|各|每)\s*(?:{_DIMENSION_COUNTERS})?\s*"
        rf"([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9_]{{1,15}}?)"
        rf"(?=(?:客户|用户|订单|{metric_lookahead}|最高|最低|$))",
        question,
    )
    if not match:
        return []
    value = match.group(1)
    for stopword in _DIMENSION_STOPWORDS:
        index = value.find(stopword)
        if index > 0:
            value = value[:index]
    value = value.strip("的").strip()
    return [value] if value else []


def _time_ranges(question: str) -> list[SemanticTimeRange]:
    result: list[SemanticTimeRange] = []
    for pattern, kind, granularity in _TIME_PATTERNS:
        for match in re.finditer(pattern, question):
            expression = normalize_chinese_text(match.group(0))
            if not any(item.expression == expression for item in result):
                result.append(
                    SemanticTimeRange(
                        expression=expression,
                        kind=kind,
                        canonical=_canonical_time(expression, kind, granularity),
                        granularity=granularity,
                    )
                )
    return result


def _canonical_time(expression: str, kind: TimeKind, granularity: str) -> str:
    rolling = re.fullmatch(r"最近\s*(\d+)\s*(天|周|个月|年)", expression)
    if rolling:
        unit = {"天": "d", "周": "w", "个月": "mo", "年": "y"}[rolling.group(2)]
        return f"rolling_{rolling.group(1)}{unit}"
    calendar = {
        "去年": "previous_calendar_year",
        "今年": "current_calendar_year",
        "前年": "two_years_ago",
        "上个月": "previous_calendar_month",
        "本月": "current_calendar_month",
        "下个月": "next_calendar_month",
        "上季度": "previous_calendar_quarter",
        "本季度": "current_calendar_quarter",
        "下季度": "next_calendar_quarter",
        "上周": "previous_calendar_week",
        "本周": "current_calendar_week",
        "下周": "next_calendar_week",
        "昨天": "previous_calendar_day",
        "今天": "current_calendar_day",
        "明天": "next_calendar_day",
    }
    return calendar.get(expression, f"{kind}_{granularity}")


def _filters(question: str) -> list[SemanticFilter]:
    operators: dict[str, FilterOperator] = {
        "大于": ">",
        "超过": ">",
        "高于": ">",
        "至少": ">=",
        "小于": "<",
        "低于": "<",
        "最多": "<=",
        "等于": "=",
        "不等于": "!=",
        ">=": ">=",
        "<=": "<=",
        ">": ">",
        "<": "<",
        "=": "=",
    }
    result: list[SemanticFilter] = []
    for match in _FILTER_PATTERN.finditer(question):
        raw_field = match.group("field")
        field = next(
            (term for term in _METRIC_TERMS if raw_field.endswith(term)),
            raw_field,
        )
        raw_value = match.group("value")
        value_type: Literal["text", "number", "date", "unknown"] = "text"
        if re.fullmatch(r"[\d.,]+(?:%|万|亿)?", raw_value):
            value_type = "number"
        result.append(
            SemanticFilter(
                field=field,
                operator=operators[match.group("operator")],
                value=raw_value,
                value_type=value_type,
            )
        )
    return result


def build_semantic_ir(
    question: str,
    *,
    question_type: str,
    calculation_subtype: str | None = None,
) -> SemanticIR:
    """Build a conservative IR from a rewritten user question.

    This parser intentionally records only high-confidence signals. Missing
    fields remain null/empty so downstream agents can ask for clarification or
    use catalog evidence instead of receiving invented SQL semantics.
    """
    normalized = normalize_chinese_text(question)
    metrics = _metric_phrases(normalized)
    metric_specs = _metric_specs(normalized, metrics)
    dimensions = _dimension_phrases(normalized)
    times = _time_ranges(normalized)
    filters = _filters(normalized)
    subject = None
    if metrics:
        subject = metrics[0]
    elif dimensions:
        subject = dimensions[0]

    sort_order: Literal["asc", "desc"] | None = None
    if re.search(r"最高|最大|最多|降序|从高到低", normalized):
        sort_order = "desc"
    elif re.search(r"最低|最小|最少|升序|从低到高", normalized):
        sort_order = "asc"
    limit_match = re.search(r"(?:前|top\s*)(\d+)", normalized, re.IGNORECASE)
    limit = int(limit_match.group(1)) if limit_match else None
    ambiguities: list[str] = []
    if not metrics and question_type == "calculation":
        ambiguities.append("未识别出明确指标，需结合本体定义确认计算对象")
    if len(metrics) > 1:
        ambiguities.append("问题包含多个指标，需要确认是否同时返回")
    if "收入" in normalized and "营收" in normalized:
        ambiguities.append("收入与营收可能是同义词，也可能对应不同口径")

    entities = [SemanticEntity(text=value, role="metric") for value in metrics]
    entities.extend(
        SemanticEntity(text=value, role="dimension") for value in dimensions
    )
    entities.extend(
        SemanticEntity(text=item.field, role="filter_value") for item in filters
    )
    return SemanticIR(
        language=_language(normalized),
        question=normalized,
        question_type=question_type,
        calculation_subtype=calculation_subtype,
        subject=subject,
        entities=entities,
        metrics=metrics,
        metric_specs=metric_specs,
        dimensions=dimensions,
        filters=filters,
        time_ranges=times,
        granularity=times[0].granularity if times else None,
        sort_by=metrics[0] if sort_order and metrics else None,
        sort_order=sort_order,
        limit=limit,
        ambiguities=ambiguities,
    )


__all__ = [
    "SemanticEntity",
    "SemanticFilter",
    "SemanticIR",
    "SemanticMetric",
    "SemanticTimeRange",
    "build_semantic_ir",
]
