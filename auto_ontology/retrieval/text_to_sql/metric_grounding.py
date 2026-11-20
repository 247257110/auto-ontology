# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Ground user-language metrics against already retrieved semantic objects.

Two signals decide which retrieved definition a question means:

* **Qualifiers.** Business metrics differ by a stated qualifier far more often
  than by their head noun — *revenue including tax* and *revenue excluding tax*
  are both "营收", and a lexical score cannot tell them apart. Those qualifiers
  are extracted from both sides and compared explicitly, so a question that
  says "含税营收" locks onto the tax-inclusive definition instead of reporting
  the pair as ambiguous.
* **Certification.** A vetted (``certified``) SQL Attribute is the definition a
  human signed off on. It wins a tie against an uncertified lookalike instead
  of the pair being escalated to the user.

What survives both is either a **binding** (a single certified definition the
SQL generator must reuse verbatim) or a genuine **ambiguity** worth asking the
user about. Everything else stays guidance, which is what it was before.
"""

from __future__ import annotations

import os
import re
from typing import Any

from auto_ontology.semantic.chinese import normalize_chinese_text

# A candidate has to reach this lexical score before qualifiers or
# certification are worth considering at all.
_MATCH_THRESHOLD = 0.55

# Two governed definitions closer than this are indistinguishable by score and
# need either a tie-break or a clarification.
_AMBIGUITY_GAP = 0.15

# Scoring adjustments, applied on top of the lexical score.
_QUALIFIER_MATCH_BONUS = 0.5
_QUALIFIER_CONFLICT_PENALTY = 0.35
_QUALIFIER_UNSPECIFIED_PENALTY = 0.1
_CERTIFIED_BONUS = 0.3

# What a binding needs: a certified SQL Attribute that is the only plausible
# answer — the runner-up must be this far behind, or the run is still a choice.
_BINDING_MIN_LEAD = 0.4
_BINDING_MIN_CONFIDENCE = 0.9

#: Qualifier families that make two otherwise identical metrics different.
#: Within a family the members are mutually exclusive; across families they
#: compose (a metric can be both tax-exclusive and per-capita).
_QUALIFIER_FAMILIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "tax_included",
        "含税",
        (
            "含税",
            "税后",
            "含税金",
            "taxinclusive",
            "incltax",
            "includingtax",
            "grossoftax",
            "withtax",
        ),
    ),
    (
        "tax_excluded",
        "不含税",
        (
            "不含税",
            "未税",
            "除税",
            "税外",
            "税前",
            "netoftax",
            "taxexclusive",
            "excltax",
            "excludingtax",
            "withouttax",
            "nettax",
        ),
    ),
)

# Longest token first, so "不含税" is consumed before "含税" can match inside it.
_QUALIFIER_TOKENS: tuple[tuple[str, str], ...] = tuple(
    sorted(
        (
            (token, family)
            for family, _label, tokens in _QUALIFIER_FAMILIES
            for token in tokens
        ),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
)

_TOKEN_FAMILY = dict(_QUALIFIER_TOKENS)

# One alternation of every token, longest first. A regex rather than repeated
# ``str.replace`` is what makes this correct: it scans left to right, so in
# "不含税金额" the match at position 0 is "不含税" and the scan resumes after
# it — the "含税金" hiding inside would otherwise be found and reported as a
# *tax-inclusive* qualifier, silently inverting the metric's meaning.
_QUALIFIER_PATTERN = re.compile(
    "|".join(re.escape(token) for token, _family in _QUALIFIER_TOKENS)
)

# Separators dropped before matching, so "net of tax" and "net-of-tax" both
# reach the concatenated token form.
_COMPACTION_RE = re.compile(r"[\s_\-. ]+")

_FAMILY_LABELS = {family: label for family, label, _tokens in _QUALIFIER_FAMILIES}

# Blocking a question whose metric has no retrieved definition is the historical
# behaviour; the flag exists because "nothing was retrieved" is weak evidence
# that no definition exists, and long-tail metrics would otherwise always be
# turned away. Read at call time, not import time, so a deployment can flip it
# without a restart path of its own.
_BLOCK_UNRESOLVED_ENV = "METRIC_GROUNDING_BLOCK_UNRESOLVED"


def extract_qualifiers(text: str) -> frozenset[str]:
    """Qualifier families named in *text*, e.g. ``{"tax_excluded"}``.

    Matched with one longest-first alternation scanned left to right, so
    "不含税" is recognised as *excluding* tax rather than also registering the
    "含税" it contains.
    """
    normalized = _COMPACTION_RE.sub("", normalize_chinese_text(text or "")).casefold()
    if not normalized:
        return frozenset()
    return frozenset(
        _TOKEN_FAMILY[match.group(0)]
        for match in _QUALIFIER_PATTERN.finditer(normalized)
    )


def _qualifier_adjustment(asked: frozenset[str], offered: frozenset[str]) -> float:
    """Score adjustment for how well *offered* answers *asked*."""
    if not asked:
        # No qualifier in the question: any candidate is equally qualified.
        return 0.0
    if offered == asked:
        return _QUALIFIER_MATCH_BONUS
    if not offered:
        # The candidate says less than the question asked for.
        return -_QUALIFIER_UNSPECIFIED_PENALTY
    # The candidate qualifies the metric differently — it is a different metric.
    return -_QUALIFIER_CONFLICT_PENALTY


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


def _candidate_name(candidate: dict[str, Any]) -> str:
    return normalize_chinese_text(
        " ".join(
            str(candidate.get(key) or "")
            for key in ("name", "term_name", "source_column")
        )
    )


def _score_metric(metric: str, candidate: dict[str, Any]) -> float:
    """Lexical score in ``[0, 1]``, before qualifiers and certification."""
    query = normalize_chinese_text(metric).casefold()
    if not query:
        return 0.0
    name = _candidate_name(candidate).casefold()
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


def _annotate(
    candidate: dict[str, Any],
    kind: str,
    score: float,
    *,
    lexical: float,
    certified: bool,
    qualifiers: frozenset[str],
    name_qualifiers: frozenset[str],
) -> dict[str, Any]:
    return {
        "kind": kind,
        "id": candidate.get("id"),
        "name": candidate.get("name") or candidate.get("source_column") or "",
        "score": round(score, 4),
        # Pre-adjustment score. Two candidates with the same head noun have the
        # same ``lexical``; that is what makes them a genuine choice, and the
        # adjustments are what settle it.
        "lexical": round(lexical, 4),
        "description": candidate.get("description") or "",
        "expression": candidate.get("expression") or "",
        "sql": candidate.get("sql") or "",
        "certified": certified,
        # Every family the candidate mentions anywhere — used for ranking.
        "qualifiers": sorted(qualifiers),
        # Only the families in its *name*, which is what the definition claims
        # to be. A definition named "毛利率" may mention 不含税营收 in its
        # formula without being *the tax-exclusive variant* of anything.
        "name_qualifiers": sorted(name_qualifiers),
    }


def _is_certified(candidate: dict[str, Any]) -> bool:
    return bool(candidate.get("certified"))


_GOVERNED_KINDS = frozenset({"sql_attribute", "custom_analysis"})


def _decide_ambiguity(
    governed: list[dict[str, Any]],
    asked: frozenset[str],
) -> tuple[bool, str | None]:
    """Whether the top two governed definitions are a real choice for the user.

    Returns ``(ambiguous, resolved_by)``. ``resolved_by`` is ``None`` when the
    question is genuinely ambiguous (or when only one definition matched), or
    names what settled a same-head-noun pair: ``"certification"``,
    ``"qualifier"``, or ``"score"`` for an adjustment that separated them
    without either named rule applying.

    Two definitions are only *compared* when they are lexically inseparable —
    different head nouns are different metrics, not a choice. That check uses
    the pre-adjustment score deliberately: a qualifier or certification bonus
    must not be able to make an unrelated definition look like a rival.
    """
    if len(governed) < 2:
        return False, None
    top, second = governed[0], governed[1]

    comparable = abs(top["lexical"] - second["lexical"]) < _AMBIGUITY_GAP
    if comparable:
        # Same head noun, so exactly one of these has to settle it.
        if top["certified"] and not second["certified"]:
            return False, "certification"
        if (
            asked
            and set(top["qualifiers"]) == set(asked)
            and set(second["qualifiers"]) != set(asked)
        ):
            return False, "qualifier"
        if top["score"] - second["score"] >= _AMBIGUITY_GAP:
            return False, "score"
        return True, None

    # Not a lexical tie: the wording alone already prefers one definition. The
    # question's own qualifier is still worth recording, since that is what the
    # user will recognise as the reason when the answer comes back.
    if asked and set(top["qualifiers"]) == set(asked):
        return False, "qualifier"
    return False, None


def _qualifier_contrast(first: dict[str, Any], second: dict[str, Any]) -> str:
    """A human-readable note on what distinguishes two tied definitions."""
    first_q = set(first.get("qualifiers") or [])
    second_q = set(second.get("qualifiers") or [])
    if not first_q and not second_q:
        return ""
    differing = first_q.symmetric_difference(second_q)
    if not differing:
        return ""
    labels = [f"“{_FAMILY_LABELS.get(family, family)}”" for family in sorted(differing)]
    return "它们的主要区别是口径：" + " 与 ".join(labels) + "。"


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

    Each entry gains, beyond the historical fields, ``qualifiers`` (the
    families the question stated), ``binding`` (``"required"`` only when a
    single certified SQL Attribute is the unambiguous answer), and
    ``resolved_by`` (which tie-break settled a near-tie, if any).
    """
    if not semantic_ir:
        return []
    candidate_groups = (
        ("sql_attribute", sql_attributes or [], 3),
        ("custom_analysis", custom_analyses or [], 2),
        ("column_attribute", column_attributes or [], 1),
    )
    grounded: list[dict[str, Any]] = []
    for metric_name, role, spec in _phrases_to_ground(semantic_ir):
        asked_qualifiers = extract_qualifiers(metric_name)

        scored: list[tuple[int, dict[str, Any]]] = []
        for kind, candidates, priority in candidate_groups:
            for candidate in candidates:
                lexical = _score_metric(metric_name, candidate)
                if lexical < _MATCH_THRESHOLD:
                    continue
                certified = _is_certified(candidate)
                offered = extract_qualifiers(_candidate_text(candidate))
                final = (
                    lexical
                    + _qualifier_adjustment(asked_qualifiers, offered)
                    + (_CERTIFIED_BONUS if certified else 0.0)
                )
                scored.append(
                    (
                        priority,
                        _annotate(
                            candidate,
                            kind,
                            max(0.0, final),
                            lexical=lexical,
                            certified=certified,
                            qualifiers=offered,
                            name_qualifiers=extract_qualifiers(
                                _candidate_name(candidate)
                            ),
                        ),
                    )
                )

        scored.sort(
            key=lambda item: (
                -(item[1]["score"] + item[0] * 0.1),
                -item[1]["score"],
                str(item[1]["name"]),
            )
        )
        annotations = [item[1] for item in scored[:5]]
        governed = [item[1] for item in scored if item[1]["kind"] in _GOVERNED_KINDS]
        selected = (
            governed[0] if governed else (annotations[0] if annotations else None)
        )

        ambiguous, resolved_by = _decide_ambiguity(governed, asked_qualifiers)
        runner_up = governed[1] if len(governed) > 1 else None

        if not annotations:
            status, confidence, reason = (
                "unresolved",
                0.0,
                "未找到匹配的本体指标或字段",
            )
        elif ambiguous:
            status, confidence = "ambiguous", 0.45
            reason = "多个治理指标分数接近，需要用户选择"
            contrast = (
                _qualifier_contrast(governed[0], governed[1]) if runner_up else ""
            )
            if contrast:
                reason = f"{reason}；{contrast}"
        elif governed:
            status = "governed"
            confidence = min(0.99, round(0.55 + selected["score"] * 0.4, 4))
            if resolved_by:
                confidence = max(confidence, 0.9)
            reason = "已匹配到治理指标定义"
            if resolved_by == "certification":
                reason = "多个候选接近，已按认证口径选定"
            elif resolved_by == "qualifier":
                reason = "多个候选接近，已按问题中的口径限定选定"
        else:
            status = "candidate"
            confidence = min(0.6, round(0.4 + selected["score"] * 0.3, 4))
            reason = "仅匹配到普通字段候选，缺少治理公式"

        # Binding: only a signed-off SQL Attribute may be made mandatory. A
        # Custom Analysis teaches a pattern, not a formula, so pinning it would
        # over-constrain the generated query.
        #
        # The definition's *name* has to match the question's own qualifier. A
        # definition named "营收（不含税）" is one particular caliber, so pinning
        # it for a bare "营收" would silently choose for the user — that case
        # stays guidance instead.
        binding = "none"
        if (
            status == "governed"
            and selected is not None
            and selected.get("kind") == "sql_attribute"
            and selected.get("certified")
            and not ambiguous
            and confidence >= _BINDING_MIN_CONFIDENCE
            and set(selected.get("name_qualifiers") or []) == set(asked_qualifiers)
            and (
                runner_up is None
                or selected["score"] - runner_up["score"] >= _BINDING_MIN_LEAD
            )
        ):
            binding = "required"
        elif status == "governed":
            binding = "guidance"

        grounded.append(
            {
                "name": metric_name,
                "role": role,
                "aggregation": spec.get("aggregation", "unknown"),
                "unit": spec.get("unit"),
                "comparison": spec.get("comparison", "none"),
                "status": status,
                "confidence": confidence,
                "reason": reason,
                "qualifiers": sorted(asked_qualifiers),
                "certified": bool(selected.get("certified")) if selected else False,
                "resolved_by": resolved_by,
                "binding": binding,
                "selected": selected,
                "runner_up": runner_up,
                "matches": annotations,
            }
        )
    return grounded


def _phrases_to_ground(
    semantic_ir: dict[str, Any],
) -> list[tuple[str, str, dict[str, Any]]]:
    """``[(phrase, role, spec)]`` for everything the question asks to ground.

    Metrics first, then dimensions. Ordinal order follows the question, and a
    phrase appearing as both is grounded once — as the metric, because the
    measurement is what the answer is about.

    Dimensions belong here because that is where a cross-table concept lives:
    "按地域" is three joined tables, "按品类" is a many-to-many bridge, and
    "按销售员" is a foreign key into a shared person table. A concept Term
    carries that path in a certified SQL Attribute, and grounding is what makes
    it *bindable* rather than merely present in the prompt.
    """
    phrases: list[tuple[str, str, dict[str, Any]]] = []
    seen: set[str] = set()

    for spec in semantic_ir.get("metric_specs") or []:
        if not isinstance(spec, dict):
            continue
        name = str(spec.get("name") or "").strip()
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            phrases.append((name, "metric", spec))

    for dimension in semantic_ir.get("dimensions") or []:
        name = str(dimension or "").strip()
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            phrases.append((name, "dimension", {}))

    return phrases


def block_unresolved_metrics() -> bool:
    """Whether a metric with no retrieved definition should stop the query.

    Defaults to the historical behaviour (block). Set
    ``METRIC_GROUNDING_BLOCK_UNRESOLVED=false`` to let the generator fall back
    to physical columns instead of asking the user to define the metric.
    """
    raw = os.environ.get(_BLOCK_UNRESOLVED_ENV, "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def build_metric_clarification_question(
    grounded_metrics: list[dict[str, Any]] | None,
    *,
    block_unresolved: bool | None = None,
) -> str | None:
    """Build one focused Chinese question for an unsafe metric grounding.

    Only metrics that a binding could not settle are asked about: a
    ``binding == "required"`` metric is answered by the ontology, and asking
    the user to choose anyway would be the opposite of using the governed
    definition.
    """
    ask_unresolved = (
        block_unresolved if block_unresolved is not None else block_unresolved_metrics()
    )
    for metric in grounded_metrics or []:
        name = str(metric.get("name") or "").strip()
        status = metric.get("status")
        if not name:
            continue
        # Only a *measurement* justifies stopping the query. A dimension that
        # matched no governed definition is ordinary — it is still a column the
        # generator can group by — so dimensions never block.
        if metric.get("role") == "dimension":
            continue
        if metric.get("binding") == "required":
            continue
        if status == "ambiguous":
            matches = metric.get("matches") or []
            if not matches:
                continue
            choices = "、".join(
                str(item.get("name") or "未命名指标") for item in matches[:3]
            )
            question = (
                f"指标“{name}”对应多个口径（{choices}）。请确认要使用哪一个指标，"
                "或补充它的业务定义/计算公式。"
            )
            contrast = (
                _qualifier_contrast(matches[0], matches[1]) if len(matches) > 1 else ""
            )
            if contrast:
                question += contrast
            return question
        if status == "unresolved" and ask_unresolved:
            return (
                f"暂未找到指标“{name}”的明确治理定义。请补充该指标的业务含义、"
                "对应字段，或提供计算公式后再查询。"
            )
    return None


__all__ = [
    "block_unresolved_metrics",
    "build_metric_clarification_question",
    "extract_qualifiers",
    "ground_metric_semantics",
]
