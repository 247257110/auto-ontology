# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

import pytest

from auto_ontology.retrieval.text_to_sql import pipeline
from auto_ontology.retrieval.text_to_sql.agents.sql_execution import QueryResponse
from auto_ontology.retrieval.text_to_sql.react import agent as agent_module
from auto_ontology.retrieval.text_to_sql.react.loop import EpisodeResult, Submission
from auto_ontology.retrieval.text_to_sql.react.semantic import SemanticContext


def _state() -> dict:
    return {
        "initial_question": "How much revenue came from active customers?",
        "evidence": "",
        "llm": SimpleNamespace(),
        "connectors": [SimpleNamespace(database_name="shop", dialect="sqlite")],
        "semantic_retriever": object(),
        "path_state": {"target_db": "shop"},
    }


def _episode(submission: Submission | None, reason: str) -> EpisodeResult:
    trajectory = [
        {
            "role": "ai",
            "tool_calls": [{"name": "retrieve", "args": {"query": "revenue"}}],
        }
    ]
    return EpisodeResult(submission, reason, 1, trajectory)


def test_an_answer_is_handed_to_formatting(monkeypatch: pytest.MonkeyPatch) -> None:
    db_response = QueryResponse(result=['[{"total": 160.0}]'], sliced=False)
    submission = Submission("SELECT 1", "Revenue of active customers.", "", db_response)
    monkeypatch.setattr(
        agent_module, "run_episode", lambda *a, **k: _episode(submission, "submitted")
    )

    result = agent_module.ReactSQLAgent().execute(_state())

    path_state = result["path_state"]
    assert result["decision"] == "valid_sql"
    assert path_state["sql_generation_result"].sql_code == "SELECT 1"
    assert (
        path_state["sql_generation_result"].response == "Revenue of active customers."
    )
    assert path_state["sql_response_from_db"] == ['[{"total": 160.0}]']
    assert path_state["thoughts_log"] == [
        {"node": "react_sql_agent", "text": "Searching for: revenue"}
    ]
    assert pipeline.route_react(result) == "format_and_respond"


def test_an_information_answer_is_the_final_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = "Table orders stores each order."
    contexts = []

    def episode(_llm: object, ctx: object, *a: object, **k: object) -> EpisodeResult:
        contexts.append(ctx)
        return _episode(Submission("", answer, "", None), "submitted")

    monkeypatch.setattr(agent_module, "run_episode", episode)

    result = agent_module.ReactSQLAgent().execute(_state())

    assert contexts[0].information_answers is True
    assert result["decision"] == "information_answer"
    assert result["messages"][-1].content == answer
    assert result["path_state"]["final_response"]["response"] == answer
    assert result["path_state"]["final_response"]["sql_code"] == ""
    assert "sql_code" not in result["path_state"]
    assert pipeline.route_react(result) is None


def test_calculation_only_turns_information_answers_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contexts = []

    def episode(_llm: object, ctx: object, *a: object, **k: object) -> EpisodeResult:
        contexts.append(ctx)
        return _episode(None, "budget_exhausted")

    monkeypatch.setattr(agent_module, "run_episode", episode)

    agent_module.ReactSQLAgent().execute({**_state(), "calculation_only": True})

    assert contexts[0].information_answers is False


def test_examples_anchors_and_short_answers_reach_the_episode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def episode(*_a: object, **kwargs: object) -> EpisodeResult:
        calls.append(kwargs)
        return _episode(None, "budget_exhausted")

    monkeypatch.setattr(agent_module, "run_episode", episode)
    examples = [{"question": "How many?", "sql": "SELECT COUNT(*) FROM t"}]
    anchors = [{"phrase": "active", "tbl": "customers", "col": "status"}]

    agent_module.ReactSQLAgent().execute(
        {
            **_state(),
            "sql_examples": examples,
            "value_anchors": anchors,
            "shorten_answer": True,
        }
    )

    assert calls[0]["sql_examples"] == examples
    assert calls[0]["value_anchors"] == anchors
    assert calls[0]["shorten_answer"] is True


def test_no_answer_is_reported_as_unconstructable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        agent_module, "run_episode", lambda *a, **k: _episode(None, "budget_exhausted")
    )

    result = agent_module.ReactSQLAgent().execute(_state())

    assert result["decision"] == "unconstructable"
    assert result["path_state"]["react_end_reason"] == "budget_exhausted"
    assert result["path_state"]["unconstructable_explanation"]
    assert pipeline.route_react(result) == "unconstructable_sql_response"


def test_the_pipeline_has_only_the_agent_and_its_two_outcomes() -> None:
    assert set(pipeline.create_steps()) == {
        "react_sql_agent",
        "format_and_respond",
        "unconstructable_sql_response",
    }


def test_a_metric_clarification_stops_before_the_episode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clarification = "指标“营收”对应多个口径，请确认使用哪一个。"
    monkeypatch.setattr(
        agent_module,
        "build_semantic_context",
        lambda *a, **k: SemanticContext(
            ir={"language": "zh"}, clarification=clarification
        ),
    )

    def fail(*_a: object, **_k: object) -> EpisodeResult:
        raise AssertionError("the episode must not run while a metric is ambiguous")

    monkeypatch.setattr(agent_module, "run_episode", fail)

    result = agent_module.ReactSQLAgent().execute(
        {**_state(), "prediction_override": False}
    )

    assert result["decision"] == "unconstructable"
    assert result["path_state"]["unconstructable_explanation"] == clarification
    assert result["path_state"]["metric_clarification_question"] == clarification
    assert pipeline.route_react(result) == "unconstructable_sql_response"


def test_a_semantic_context_reaches_the_episode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = SemanticContext(ir={"language": "zh", "metrics": ["营收"]})
    monkeypatch.setattr(agent_module, "build_semantic_context", lambda *a, **k: context)
    calls: list[dict] = []

    def episode(*_a: object, **kwargs: object) -> EpisodeResult:
        calls.append(kwargs)
        return _episode(None, "budget_exhausted")

    monkeypatch.setattr(agent_module, "run_episode", episode)

    result = agent_module.ReactSQLAgent().execute(
        {**_state(), "prediction_override": False}
    )

    assert calls[0]["semantic_context"] is context
    assert result["path_state"]["semantic_ir"] == context.ir


def test_a_chinese_question_gets_a_chinese_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        agent_module, "build_semantic_context", lambda *a, **k: SemanticContext()
    )
    monkeypatch.setattr(
        agent_module, "run_episode", lambda *a, **k: _episode(None, "budget_exhausted")
    )

    result = agent_module.ReactSQLAgent().execute(
        {
            **_state(),
            "initial_question": "查询最近30天营收",
            "prediction_override": False,
        }
    )

    assert "无法" in result["path_state"]["unconstructable_explanation"]


def test_a_metric_question_turns_prose_answers_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contexts = []
    monkeypatch.setattr(
        agent_module,
        "build_semantic_context",
        lambda *a, **k: SemanticContext(ir={"metrics": ["营收"]}),
    )

    def episode(_llm: object, ctx: object, *_a: object, **_k: object) -> EpisodeResult:
        contexts.append(ctx)
        return _episode(None, "budget_exhausted")

    monkeypatch.setattr(agent_module, "run_episode", episode)

    agent_module.ReactSQLAgent().execute(
        {
            **_state(),
            "initial_question": "查询最近12个月营收",
            "prediction_override": False,
        }
    )

    assert contexts[0].information_answers is False


def test_a_definition_question_keeps_prose_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contexts = []
    monkeypatch.setattr(
        agent_module,
        "build_semantic_context",
        lambda *a, **k: SemanticContext(ir={"metrics": ["营收"]}),
    )

    def episode(_llm: object, ctx: object, *_a: object, **_k: object) -> EpisodeResult:
        contexts.append(ctx)
        return _episode(None, "budget_exhausted")

    monkeypatch.setattr(agent_module, "run_episode", episode)

    agent_module.ReactSQLAgent().execute(
        {
            **_state(),
            "initial_question": "营收怎么算？",
            "prediction_override": False,
        }
    )

    assert contexts[0].information_answers is True
