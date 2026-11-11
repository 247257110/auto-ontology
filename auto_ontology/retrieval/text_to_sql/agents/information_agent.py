# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tool-using agent that answers catalog and semantic-metadata questions."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any, Dict

from langchain_core.messages import AIMessage, SystemMessage
from pydantic import Field, model_validator

from auto_ontology.retrieval.text_to_sql.agents.information_tools import (
    InformationToolName,
    run_information_tool,
)
from auto_ontology.retrieval.text_to_sql.base import BaseAgent
from auto_ontology.retrieval.text_to_sql.state import (
    AgentState,
    get_question_for_processing,
)
from auto_ontology.utils.llm_invoke import (
    StrictLLMOutputModel,
    invoke_with_structured_output,
)

MAX_TOOL_CALLS = 6
MAX_AGENT_ITERATIONS = 8
MAX_OBSERVATION_CHARS = 12_000
MAX_CANDIDATES_PER_KIND = 12


class InformationActionType(StrEnum):
    """Possible next steps selected by the metadata agent."""

    TOOL = "tool"
    ANSWER = "answer"


class InformationToolArguments(StrictLLMOutputModel):
    """The bounded set of identifiers accepted by metadata tools."""

    database_id: str | None = None
    dataset_id: str | None = None
    dataset_name: str | None = None
    schema_id: str | None = None
    table_id: str | None = None
    table_name: str | None = None
    column_id: str | None = None
    column_name: str | None = None
    database_name: str | None = None
    term_id: str | None = None
    term_name: str | None = None
    column_attribute_id: str | None = None
    column_attribute_name: str | None = None
    sql_attribute_id: str | None = None
    sql_attribute_name: str | None = None
    search: str | None = None
    query: str | None = None
    labels: list[str] | None = None
    skip: int | None = Field(default=None, ge=0)
    limit: int | None = Field(default=None, ge=1, le=100)


class InformationAgentAction(StrictLLMOutputModel):
    """One structured action in the information-agent loop."""

    action: InformationActionType
    tool: InformationToolName | None = None
    arguments: InformationToolArguments | None = None
    answer: str | None = Field(
        default=None,
        description=(
            "The final answer, as concise Markdown written in Simplified Chinese "
            "(简体中文). Keep dataset, schema, table, column and Term names "
            "exactly as they appear in the metadata — do not translate them."
        ),
    )
    source_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_action_fields(self) -> InformationAgentAction:
        if self.action == InformationActionType.TOOL:
            if self.tool is None or self.arguments is None:
                raise ValueError("tool actions require tool and arguments")
            if self.answer is not None:
                raise ValueError("tool actions must not include an answer")
        else:
            if not self.answer or not self.answer.strip():
                raise ValueError("answer actions require a non-empty answer")
            if self.tool is not None or self.arguments is not None:
                raise ValueError("answer actions must not include tool fields")
        return self


def _candidate_view(candidate: Any) -> dict[str, Any]:
    """Keep useful grounding fields while bounding vector-hit payloads."""
    if not isinstance(candidate, dict):
        return {}
    metadata = candidate.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    keys = (
        "id",
        "name",
        "text",
        "description",
        "database_name",
        "schema_name",
        "table_id",
        "table_name",
        "source_column",
        "term_name",
        "source",
    )
    result: dict[str, Any] = {}
    for key in keys:
        value = candidate.get(key)
        if value in (None, ""):
            value = metadata.get(key)
        if value not in (None, ""):
            result[key] = value
    return result


def _initial_context(state: AgentState) -> dict[str, Any]:
    path_state = state.get("path_state", {})

    def candidates(key: str) -> list[dict[str, Any]]:
        raw = path_state.get(key)
        if not isinstance(raw, list):
            return []
        return [
            view
            for item in raw[:MAX_CANDIDATES_PER_KIND]
            if (view := _candidate_view(item))
        ]

    return {
        "question": get_question_for_processing(state),
        "entities": path_state.get("entities") or [],
        "subject": path_state.get("subject"),
        "dataset": path_state.get("target_db"),
        "extracted_evidence": path_state.get("extracted_evidence") or "",
        "column_attribute_candidates": candidates("retrieved_column_attributes"),
        "sql_attribute_candidates": candidates("retrieved_sql_attributes"),
        "custom_analysis_candidates": candidates("retrieved_custom_analyses"),
        "subject_candidate": _candidate_view(path_state.get("retrieved_subject_term")),
    }


def _render_prompt(
    context: dict[str, Any],
    observations: list[dict[str, Any]],
    *,
    tool_calls_remaining: int,
) -> str:
    observation_text = json.dumps(observations, default=str, ensure_ascii=False)
    if len(observation_text) > MAX_OBSERVATION_CHARS:
        observation_text = observation_text[-MAX_OBSERVATION_CHARS:]
        observation_text = "[earlier observations omitted] " + observation_text

    # The instructions are written in Chinese on purpose: the answer language
    # follows the language of the prompt far more reliably than it follows an
    # instruction *about* language. Asking for Chinese inside an English prompt
    # was tried and the model still answered in English.
    return f"""你负责回答关于数据目录元数据的问题。不要查询源数据，也不要写 SQL。
只使用给定的候选项和只读工具的返回结果，绝不编造表、列、公式、描述或关系。
所有面向用户的文字（最终的 answer）都用简体中文写；数据集、schema、表、列和 Term
的名字保持元数据里的原样，不翻译、不改大小写。

可用工具：
- list_databases：目录下的数据库（懒加载数据树的根）。
- list_schemas：某个 database_id 下的 schema。
- list_tables：某个 schema_id 下的表。
- list_columns：某个 table_id 的列分页，支持 skip/limit。
- get_dataset：数据集标识，以及有数量上限的 schema 和表。
- get_table：表详情、列、语义属性、关联表。
- get_column：列详情、计算/语义属性、所属表、关联表。
- get_table_semantic_fks：某张表的语义外键连接。
- get_term：单个术语（Term），及其代表的表、ColumnAttribute 和 SqlAttribute。
- list_terms：术语表分页浏览，可按搜索文本过滤。
- get_column_attribute：单个语义 ColumnAttribute，及其 Term、主列/引用列、描述、样例和关系。
- get_sql_attribute：单个派生 SqlAttribute，及其 Term、表达式和完整 SQL。
- search_semantic_layer：当给定候选项找不到正确元数据时，向量检索 Term、
  ColumnAttribute、SqlAttribute 和 CustomAnalysis。

优先使用候选项或工具返回里已有的 ID。按名称查找时用数据集名限定范围。已知 ID 或
确切名称时，优先用精确的元数据工具而不是向量检索。如果用户问某个度量如何计算或
如何定义，报告它的描述/公式或 SQL 语义属性、所属表/列，以及关联表。如果用户问
某张表有哪些列，列出这些列及其关联表。元数据缺失或有歧义时要明确说出来。

需要更多元数据时返回 action="tool"；问题可以回答时返回 action="answer"。
最终答案用简洁的中文 Markdown，并在 source_ids 里列出真正支撑该答案的目录 ID。
不要暴露这套工作流程。

剩余工具调用次数：{tool_calls_remaining}

初始上下文：
{json.dumps(context, default=str, ensure_ascii=False)}

工具返回：
{observation_text}
"""


def _collect_ids(value: Any) -> set[str]:
    ids: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if (key == "id" or key.endswith("_id")) and item:
                ids.add(str(item))
            ids.update(_collect_ids(item))
    elif isinstance(value, list):
        for item in value:
            ids.update(_collect_ids(item))
    return ids


def _collect_column_ids(value: Any) -> set[str]:
    column_ids: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"column", "source_column", "foreign_key_column"} and isinstance(
                item, dict
            ):
                if item.get("id"):
                    column_ids.add(str(item["id"]))
            if key in {"columns", "connected_columns", "referencing_columns"}:
                column_ids.update(_collect_ids(item))
            column_ids.update(_collect_column_ids(item))
    elif isinstance(value, list):
        for item in value:
            column_ids.update(_collect_column_ids(item))
    return column_ids


def _fallback_answer(context: dict[str, Any], reason: str) -> str:
    candidates = context.get("column_attribute_candidates") or []
    names = [
        str(candidate.get("name") or candidate.get("text") or "").strip()
        for candidate in candidates[:5]
    ]
    names = [name for name in names if name]
    candidate_note = "\n\n找到的相关候选项：" + "、".join(names) + "。" if names else ""
    return f"这次元数据查询没能安全完成。{reason} 没有查询任何源数据。{candidate_note}"


def _final_response(answer: str, column_ids: list[str]) -> dict[str, Any]:
    return {
        "response": answer,
        "sql_code": "",
        "sql_columns": column_ids,
        "custom_analyses_used": [],
        "sql_response_from_db": None,
    }


class InformationAgent(BaseAgent):
    """Answer an information intent through a bounded structured tool loop."""

    def __init__(self) -> None:
        super().__init__("information_agent")

    def validate_input(self, state: AgentState) -> bool:
        return bool(get_question_for_processing(state))

    def execute(self, state: AgentState) -> Dict[str, Any]:
        path_state = state.get("path_state", {})
        context = _initial_context(state)
        observations: list[dict[str, Any]] = []
        call_records: list[dict[str, Any]] = []
        seen_calls: set[str] = set()
        known_source_ids = _collect_ids(context)
        known_column_ids: set[str] = set()
        answer: str | None = None
        final_source_ids: list[str] = []
        tool_call_count = 0

        for _ in range(MAX_AGENT_ITERATIONS):
            prompt = _render_prompt(
                context,
                observations,
                tool_calls_remaining=MAX_TOOL_CALLS - tool_call_count,
            )
            try:
                action = invoke_with_structured_output(
                    state["llm"],
                    [SystemMessage(content=prompt)],
                    InformationAgentAction,
                )
            except Exception:
                self.logger.exception("Information-agent LLM call failed")
                action = None

            if action is None:
                answer = _fallback_answer(context, "元数据推理步骤失败。")
                break

            if action.action == InformationActionType.ANSWER:
                answer = (action.answer or "").strip()
                final_source_ids = list(dict.fromkeys(action.source_ids))
                break

            assert action.tool is not None
            assert action.arguments is not None
            arguments = action.arguments.model_dump(exclude_none=True)
            signature = json.dumps(
                {"tool": action.tool.value, "arguments": arguments},
                sort_keys=True,
            )
            if signature in seen_calls:
                observations.append(
                    {
                        "tool": action.tool.value,
                        "arguments": arguments,
                        "result": {
                            "ok": False,
                            "error": {
                                "code": "duplicate_call",
                                "message": "This exact tool call was already made.",
                            },
                        },
                    }
                )
                continue
            if tool_call_count >= MAX_TOOL_CALLS:
                answer = _fallback_answer(context, "元数据查询到达了工具调用上限。")
                break

            seen_calls.add(signature)
            tool_call_count += 1
            try:
                result = run_information_tool(
                    action.tool,
                    arguments,
                    semantic_retriever=state.get("semantic_retriever"),
                )
            except Exception as exc:
                self.logger.exception("Information metadata tool failed")
                result = {
                    "ok": False,
                    "error": {
                        "code": "tool_failure",
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                }
            observations.append(
                {
                    "tool": action.tool.value,
                    "arguments": arguments,
                    "result": result,
                }
            )
            call_records.append(
                {
                    "tool": action.tool.value,
                    "arguments": arguments,
                    "ok": bool(result.get("ok")),
                }
            )
            known_source_ids.update(_collect_ids(result))
            known_column_ids.update(_collect_column_ids(result))

        if answer is None:
            answer = _fallback_answer(context, "元数据查询到达了推理轮次上限。")

        sources = [
            source_id for source_id in final_source_ids if source_id in known_source_ids
        ]
        linked_column_ids = [
            source_id for source_id in sources if source_id in known_column_ids
        ]
        final_response = _final_response(answer, linked_column_ids)
        new_path_state = {
            **path_state,
            "information_tool_calls": call_records,
            "information_sources": sources,
            "formatted_response": answer,
            "final_response": final_response,
        }
        return {
            "messages": list(state.get("messages") or []) + [AIMessage(content=answer)],
            "path_state": new_path_state,
        }


__all__ = [
    "InformationActionType",
    "InformationAgent",
    "InformationAgentAction",
    "InformationToolArguments",
    "MAX_AGENT_ITERATIONS",
    "MAX_TOOL_CALLS",
]
