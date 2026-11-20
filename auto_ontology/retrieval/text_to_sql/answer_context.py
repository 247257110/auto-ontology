# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Attach what the pipeline *decided* to the answer it produced.

By the time a query is answered, the run has already resolved three things that
change what the number means, and none of them used to reach the user:

* which government definition a metric was computed from, and whether that
  definition was certified (``metric_grounding``);
* which concrete date range a relative phrase became, anchored on the data's
  own calendar rather than today (``time_anchor``);
* whether an empty result is a fact about the data (``EmptyVerdict``).

A reviewer cannot check an answer against the source without that, and a
plausible-but-different caliber reads exactly like a correct answer — the WWI
gross-margin example differs by 6.5 points between its two tax conventions and
both are "毛利率". So the disclosure goes in the reply, not only in the prompt.

It also propagates for free: the persisted assistant message is this string, and
the follow-up rewriter is fed the previous turns' responses (see
``server/chat/conversation_dal.py`` and ``follow_up._format_history``), so the
next turn's rewrite sees the caliber the last answer was computed under.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)

#: Metrics disclosed per answer. A note listing more than a handful stops being
#: read, and questions rarely carry more than three.
_MAX_DISCLOSED_METRICS = 3

_QUALIFIER_LABELS = {
    "tax_included": "含税",
    "tax_excluded": "不含税",
}

_NOTE_SEPARATOR = "---"
_NOTE_HEADING = "口径说明"


def _qualifier_text(metric: dict[str, Any]) -> str:
    labels = [
        _QUALIFIER_LABELS.get(str(item), str(item))
        for item in (metric.get("qualifiers") or [])
    ]
    return f"（口径：{'、'.join(labels)}）" if labels else ""


def _metric_line(metric: dict[str, Any]) -> str:
    """One line per metric, claiming only what grounding established."""
    name = str(metric.get("name") or "").strip()
    if not name:
        return ""
    status = metric.get("status")
    selected = metric.get("selected") or {}

    if status == "governed" and selected:
        definition = str(selected.get("name") or "").strip()
        if not definition:
            return ""
        trust = "已认证定义" if selected.get("certified") else "治理定义"
        return f"指标「{name}」：按{trust}「{definition}」计算{_qualifier_text(metric)}"

    if status == "candidate":
        return (
            f"指标「{name}」：未匹配到治理定义，按字段直接计算{_qualifier_text(metric)}"
        )

    if status == "unresolved":
        return f"指标「{name}」：没有治理定义，按问题描述生成，建议核对计算方式"

    return ""


def _human_span(start: Any, end: Any) -> str:
    """An inclusive, human-readable span for a half-open resolved range.

    ``ResolvedTimeRange.end`` is *exclusive* — that is what keeps a month
    boundary from double-counting its last day, and it is the wrong thing to
    show a person: "2015-06-01 ~ 2016-06-01" reads as covering June 1st, which
    the predicate does not. The disclosure therefore reports the last day it
    actually includes.

    ``DataWindow.end`` is different and is *not* passed through here: it is the
    last day that has data, inclusive already.
    """
    if not end:
        return f"（截至 {start}）" if start else ""
    last = end
    if isinstance(end, date):
        last = end - timedelta(days=1)
    if not start:
        return f"（截至 {last}）"
    return f"{start} ~ {last}"


def _time_lines(path_state: dict[str, Any]) -> list[str]:
    """Coverage plus every resolved relative range, as the run resolved them."""
    anchor = path_state.get("time_anchor") or {}
    if not isinstance(anchor, dict):
        return []

    lines: list[str] = []
    window = anchor.get("window") or {}
    start = window.get("start")
    end = window.get("end")
    if end:
        coverage = f"{start} ~ {end}" if start else end
        source = window.get("source")
        suffix = f"（取自 {source}）" if source else ""
        lines.append(f"数据覆盖：{coverage}{suffix}")

    for item in anchor.get("resolved") or []:
        if not isinstance(item, dict):
            continue
        expression = str(item.get("expression") or "").strip()
        item_start = _as_date(item.get("start"))
        item_end = _as_date(item.get("end"))
        if not expression or (item_start is None and item_end is None):
            continue
        span = _human_span(item_start, item_end)
        if not span:
            continue
        lines.append(f"时间「{expression}」：{span}（按数据末端锚定，非今日）")
    return lines


def _as_date(value: Any) -> date | None:
    """Parse the ISO date strings ``as_dict`` writes; tolerate ``None``."""
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def build_caliber_note(path_state: dict[str, Any] | None) -> str:
    """The ``口径说明`` block for *path_state*, or ``""`` when there is none.

    Only the parts the run actually established are included: a metric that
    grounded to nothing contributes a warning line, an unmeasured window
    contributes nothing at all. An empty block means the answer carries no
    structured claims, and saying nothing is better than saying "n/a".
    """
    if not path_state:
        return ""

    lines: list[str] = []
    for metric in (path_state.get("grounded_metrics") or [])[:_MAX_DISCLOSED_METRICS]:
        if isinstance(metric, dict):
            line = _metric_line(metric)
            if line:
                lines.append(line)
    lines.extend(_time_lines(path_state))

    if not lines:
        return ""
    return "\n".join(
        [_NOTE_SEPARATOR, f"{_NOTE_HEADING}：", *[f"- {line}" for line in lines]]
    )


def _empty_result_note(empty_result: Any) -> str:
    if not isinstance(empty_result, dict):
        return ""
    if empty_result.get("verdict") != "data_fact":
        return ""
    return str(empty_result.get("explanation") or "").strip()


def attach_answer_notes(
    formatted_response: str,
    path_state: dict[str, Any] | None,
) -> str:
    """Prefix the verified-empty explanation and suffix the caliber block.

    Two different jobs, one entry point, so the response can never end up with
    one of them and not the other:

    * the empty-result note *explains the answer*, so it goes first — a reader
      who stops after one line should still learn the table is empty on
      purpose;
    * the caliber note *qualifies the answer*, so it goes last, after the
      value it applies to.

    Idempotent: an answer that already carries either block is returned as-is,
    which matters because a regenerated SQL can reach this twice.
    """
    text = formatted_response or ""
    if not path_state:
        return text

    explanation = _empty_result_note(path_state.get("empty_result"))
    if explanation and explanation not in text:
        text = f"{explanation}\n\n{text}" if text else explanation

    caliber = build_caliber_note(path_state)
    if caliber and _NOTE_HEADING not in text:
        text = f"{text}\n\n{caliber}" if text else caliber

    return text


__all__ = ["attach_answer_notes", "build_caliber_note"]
