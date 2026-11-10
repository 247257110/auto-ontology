# SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""User-facing labels for LangGraph node names.

Lives next to ``text_to_sql_graph.py`` (the source of truth for node names)
rather than in ``auto_ontology.server.chat`` so the agent pipeline itself — e.g.
``main.py``'s ``stream_agent_response`` when it builds the final
``thoughts`` summary — can use the same labels without depending on the
server layer. ``auto_ontology.server.chat.helpers`` re-exports this for the router.

1-to-1 with the agent classes instantiated inside ``create_graph``. Unknown
nodes fall through to the raw node_name in callers so we never display a
blank thinking step.

The labels are Chinese because this deployment's chat UI is Chinese-only (it
has no i18n layer; ``frontend/app/layout.tsx`` hardcodes ``lang="en"`` but every
other surface is Chinese). Keep them short — they render on one line above the
node's ``thought`` in the thinking bubble.
"""

from __future__ import annotations

NODE_LABELS: dict[str, str] = {
    "question_intent": "判断问题类型",
    "question_extraction": "理解问题",
    "prepare_prediction_graph": "准备预测",
    "kumo_predict": "准备预测",
    "retrieve_candidates": "检索候选项",
    "information_agent": "从元数据作答",
    "prepare_candidates": "准备候选项",
    "refine_evidence": "细化证据",
    "precheck_combined": "校验关联与取值",
    "construct_sql_from_candidates": "生成 SQL",
    "reconstruct_sql": "重写 SQL",
    "validate_sql_values": "校验筛选值",
    "validate_sql_query": "校验 SQL 与意图",
    "execute_sql_query": "执行 SQL",
    "check_empty_like_result": "检查结果",
    "check_value_repair": "检查取值",
    "format_and_respond": "组织答案",
    "unconstructable_sql_response": "无法生成 SQL",
}

__all__ = ["NODE_LABELS"]
