# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The answer carries what the run resolved: caliber, dates, verified emptiness.

A number whose caliber is unstated is not checkable. Both tax conventions of
Wide World Importers' gross margin are legitimately called "毛利率" and differ by
6.5 points, so the reply has to say which one it used — and the same string is
what the follow-up rewriter reads on the next turn, which is how the caliber
survives into a follow-up without any extra plumbing.
"""

from __future__ import annotations

from auto_ontology.retrieval.text_to_sql.answer_context import (
    attach_answer_notes,
    build_caliber_note,
)

ANSWER = "毛利率为 49.77%。"


def _governed(name: str, definition: str, *, certified: bool, qualifiers=()) -> dict:
    return {
        "name": name,
        "status": "governed",
        "qualifiers": list(qualifiers),
        "selected": {"name": definition, "certified": certified},
    }


def _anchor(**overrides) -> dict:
    base = {
        "window": {
            "start": "2013-01-01",
            "end": "2016-05-31",
            "source": "wideworldimporters.sales.invoices.invoicedate",
        },
        "resolved": [
            {
                "expression": "最近12个月",
                "canonical": "rolling_12mo",
                "start": "2015-06-01",
                "end": "2016-06-01",
            }
        ],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_a_certified_definition_is_named_as_certified() -> None:
    note = build_caliber_note(
        {"grounded_metrics": [_governed("毛利率", "毛利率（不含税）", certified=True)]}
    )

    assert "毛利率" in note
    assert "已认证定义" in note
    assert "毛利率（不含税）" in note


def test_an_uncertified_definition_is_not_called_certified() -> None:
    """The distinction is the whole point of the flag; do not blur it."""
    note = build_caliber_note(
        {"grounded_metrics": [_governed("毛利率", "毛利率草稿", certified=False)]}
    )

    assert "已认证" not in note
    assert "治理定义" in note


def test_qualifiers_are_translated_into_business_words() -> None:
    note = build_caliber_note(
        {
            "grounded_metrics": [
                _governed(
                    "营收",
                    "营收（不含税）",
                    certified=True,
                    qualifiers=["tax_excluded"],
                )
            ]
        }
    )

    assert "口径：不含税" in note
    assert "tax_excluded" not in note  # internal vocabulary must not leak


def test_a_metric_with_no_governed_definition_says_so() -> None:
    note = build_caliber_note(
        {"grounded_metrics": [{"name": "神秘指标", "status": "candidate"}]}
    )

    assert "未匹配到治理定义" in note


def test_an_unresolved_metric_warns_about_the_formula() -> None:
    note = build_caliber_note(
        {"grounded_metrics": [{"name": "神秘指标", "status": "unresolved"}]}
    )

    assert "建议核对计算方式" in note


def test_at_most_a_handful_of_metrics_are_disclosed() -> None:
    metrics = [_governed(f"指标{i}", f"定义{i}", certified=True) for i in range(6)]

    note = build_caliber_note({"grounded_metrics": metrics})

    assert "指标0" in note
    assert "指标3" not in note  # capped, so the note stays readable


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def test_the_resolved_range_states_it_is_anchored_on_the_data() -> None:
    note = build_caliber_note({"time_anchor": _anchor()})

    # The stored interval is half-open (``end`` is exclusive); the disclosure
    # reports the last day it includes, so it cannot be misread as covering it.
    assert "2015-06-01 ~ 2016-05-31" in note
    assert "按数据末端锚定" in note
    assert "非今日" in note


def test_the_disclosed_span_is_inclusive_while_the_predicate_is_half_open() -> None:
    from auto_ontology.retrieval.text_to_sql.time_anchor import (
        DataWindow,
        format_time_anchor_section,
        resolve_time_ranges,
    )
    from datetime import date

    window = DataWindow(date(2013, 1, 1), date(2016, 5, 31), "t.d")
    (item,) = resolve_time_ranges(
        [{"expression": "最近12个月", "canonical": "rolling_12mo"}], window
    )

    # Prompt: exclusive upper bound, which is what the SQL must use.
    assert "< '2016-06-01'" in format_time_anchor_section([item], window)
    # Answer: the last included day, which is what a reader needs.
    note = build_caliber_note(
        {
            "time_anchor": {
                "window": window.as_dict(),
                "resolved": [item.as_dict()],
            }
        }
    )
    assert "2015-06-01 ~ 2016-05-31" in note
    assert "2016-06-01" not in note.split("时间「")[1]


def test_the_data_coverage_and_its_source_are_reported() -> None:
    note = build_caliber_note({"time_anchor": _anchor()})

    assert "2013-01-01 ~ 2016-05-31" in note
    assert "sales.invoices.invoicedate" in note


def test_a_half_known_window_still_reports_what_it_has() -> None:
    note = build_caliber_note(
        {
            "time_anchor": _anchor(
                window={"start": None, "end": "2016-05-31", "source": ""}
            )
        }
    )

    assert "2016-05-31" in note
    assert "（取自" not in note


def test_an_unresolved_time_phrase_is_omitted() -> None:
    note = build_caliber_note(
        {
            "time_anchor": _anchor(
                resolved=[{"expression": "上个财年", "start": None, "end": None}]
            )
        }
    )

    assert "上个财年" not in note


# ---------------------------------------------------------------------------
# The block as a whole
# ---------------------------------------------------------------------------


def test_no_structured_findings_means_no_note() -> None:
    """Saying nothing beats a block full of "n/a"."""
    assert build_caliber_note({}) == ""
    assert build_caliber_note(None) == ""
    assert build_caliber_note({"grounded_metrics": [], "time_anchor": {}}) == ""


def test_the_block_is_marked_and_bulleted() -> None:
    note = build_caliber_note(
        {
            "grounded_metrics": [_governed("毛利率", "毛利率", certified=True)],
            "time_anchor": _anchor(),
        }
    )

    assert note.startswith("---")
    assert "口径说明" in note
    assert "\n- " in note


def test_metrics_and_time_both_appear_when_both_are_known() -> None:
    note = build_caliber_note(
        {
            "grounded_metrics": [_governed("毛利率", "毛利率", certified=True)],
            "time_anchor": _anchor(),
        }
    )

    assert "指标「毛利率」" in note
    assert "时间「最近12个月」" in note


# ---------------------------------------------------------------------------
# Attaching to the answer
# ---------------------------------------------------------------------------


def test_the_note_is_appended_after_the_answer() -> None:
    result = attach_answer_notes(
        ANSWER, {"grounded_metrics": [_governed("毛利率", "m", certified=True)]}
    )

    assert result.startswith(ANSWER)
    assert "口径说明" in result


def test_the_empty_result_explanation_comes_first() -> None:
    """A reader who stops after one line must still learn why it is empty."""
    result = attach_answer_notes(
        ANSWER,
        {
            "empty_result": {
                "verdict": "data_fact",
                "explanation": "查询已成功执行，结果为空是数据事实。",
            },
            "grounded_metrics": [_governed("毛利率", "m", certified=True)],
        },
    )

    assert result.index("数据事实") < result.index(ANSWER)
    assert result.index(ANSWER) < result.index("口径说明")


def test_an_undiagnosed_empty_result_adds_no_explanation() -> None:
    result = attach_answer_notes(
        ANSWER, {"empty_result": {"verdict": "undiagnosed", "explanation": ""}}
    )

    assert result == ANSWER


def test_attaching_twice_changes_nothing() -> None:
    """A regenerated SQL can reach the response node more than once."""
    once = attach_answer_notes(
        ANSWER,
        {
            "empty_result": {"verdict": "data_fact", "explanation": "为空是数据事实。"},
            "grounded_metrics": [_governed("毛利率", "m", certified=True)],
        },
    )

    assert attach_answer_notes(once, {"grounded_metrics": []}) == once


def test_an_empty_answer_still_carries_its_note() -> None:
    result = attach_answer_notes(
        "", {"grounded_metrics": [_governed("毛利率", "m", certified=True)]}
    )

    assert result.startswith("---")
    assert not result.startswith("\n")


def test_the_response_agent_attaches_the_caliber_note() -> None:
    from types import SimpleNamespace

    from auto_ontology.retrieval.text_to_sql.agents.response import ResponseAgent

    state = {
        "messages": [],
        "path_state": {
            "sql_generation_result": SimpleNamespace(
                sql_code="SELECT 1", response="毛利率为 49.77%。"
            ),
            "sql_response_from_db": None,
            "grounded_metrics": [
                {
                    "name": "毛利率",
                    "status": "governed",
                    "selected": {
                        "name": "毛利率（不含税）",
                        "certified": True,
                    },
                }
            ],
        },
    }

    result = ResponseAgent().execute(state)

    formatted = result["path_state"]["formatted_response"]
    assert "毛利率为 49.77%" in formatted
    assert "口径说明" in formatted
    assert "毛利率（不含税）" in formatted
