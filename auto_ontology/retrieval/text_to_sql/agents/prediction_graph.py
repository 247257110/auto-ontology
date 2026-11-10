# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
KumoRFM graph-preparation node.

The prediction path is split into two nodes so the (potentially slow) infra
step streams its own progress:

  1. ``prepare_prediction_graph`` (this agent) — load the relevant tables, build
     the KumoRFM ``LocalGraph`` + model, and stash the resulting
     :class:`~auto_ontology.retrieval.kumo.predictor.PredictionContext` in ``path_state``.
  2. ``kumo_predict`` — generate/repair the PQL, predict, and format.

On failure (no relevant tables, KumoRFM unavailable on this platform, etc.) it
writes a graceful ``final_response`` and signals ``predict_failed`` so the graph
routes straight to END without attempting a prediction.
"""

import logging
from typing import Any, Dict

from langchain_core.messages import AIMessage
from auto_ontology.catalog.constants import Labels

from auto_ontology.dal.datasources import (
    fetch_table_by_name,
    fetch_table_in_database,
)
from auto_ontology.retrieval.kumo import PredictionContext, build_prediction_context
from auto_ontology.retrieval.kumo.pql_gen import _TABLE_COL
from auto_ontology.retrieval.kumo.rag import fetch_pql_examples
from auto_ontology.retrieval.text_to_sql.base import BaseAgent
from auto_ontology.retrieval.text_to_sql.state import (
    AgentState,
    get_standalone_question,
)

logger = logging.getLogger(__name__)


def _error_response(message: str) -> Dict[str, Any]:
    return {
        "response": message,
        "sql_code": "",
        "sql_columns": [],
        "custom_analyses_used": [],
        "sql_response_from_db": None,
    }


def _pql_tables(pql: str) -> set[str]:
    """Table names referenced in a PQL (the left side of every ``TABLE.COLUMN``)."""
    return {m.group(1) for m in _TABLE_COL.finditer(pql or "")}


def _scope_database(relevant_tables: list[dict[str, Any]]) -> str:
    """The database the relevant tables belong to, or ``''`` when unknown."""
    for table in relevant_tables:
        name = str(table.get("database_name") or "").strip()
        if name:
            return name
    return ""


def _enrich_relevant_tables(
    relevant_tables: list[dict[str, Any]],
    examples: list[dict[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Add any table referenced by a retrieved PQL example but missing from
    ``relevant_tables``, resolved from the catalog by name.

    Done BEFORE the graph is built so the added tables are loaded into the graph
    (and auto-linked by metadata inference) — the LLM is guided by these examples,
    so every table they reference must exist in the graph or the PQL won't parse.

    Two scoping rules, both learned from a deployment with two connected
    databases:

    * **Look the table up inside the scoped database only.** A bare name is not
      unique — ``invoices`` exists in both ``wideworldimporters.sales`` and the
      retail database — and an unscoped lookup returns whichever was ingested
      first along with *its* schema name, which the caller then runs on the
      other database's connection.
    * **Drop examples that cite a table the scoped database does not have.** A
      corpus verified against one database will otherwise describe tables that
      do not exist here, and the model follows the example it was shown.

    Returns the enriched tables and the examples that survived.
    """
    existing = {str(t.get("name") or "").upper() for t in relevant_tables}
    database_name = _scope_database(relevant_tables)

    enriched = list(relevant_tables)
    added: list[str] = []
    resolvable = set(existing)
    for name in sorted({n for ex in examples for n in _pql_tables(ex.get("query") or "")}):
        if name.upper() in existing:
            continue
        row = (
            fetch_table_in_database(name, database_name)
            if database_name
            else fetch_table_by_name(name)
        )
        if not row or not row.get("name"):
            # Not in this database: the example that named it cannot be followed.
            continue
        enriched.append(
            {
                "id": row.get("id"),
                "name": row.get("name"),
                "schema_name": row.get("schema_name") or "",
                "database_name": row.get("database_name") or database_name,
                "description": row.get("description") or "",
                "pk": row.get("pk"),
                "label": Labels.TABLE,
            }
        )
        existing.add(name.upper())
        resolvable.add(row["name"].upper())
        added.append(row["name"])
    if added:
        logger.info(
            "kumo: enriched graph with %d table(s) from PQL examples: %s",
            len(added),
            added,
        )

    usable: list[dict[str, str]] = []
    for example in examples:
        cited = _pql_tables(example.get("query") or "")
        missing = {name for name in cited if name.upper() not in resolvable}
        if missing:
            logger.info(
                "kumo: dropping PQL example citing table(s) absent from %s: %s",
                database_name or "(unscoped)",
                ", ".join(sorted(missing)),
            )
            continue
        usable.append(example)
    if len(usable) != len(examples):
        logger.info(
            "kumo: kept %d of %d PQL example(s) after scoping to %s",
            len(usable),
            len(examples),
            database_name or "(unscoped)",
        )
    return enriched, usable


class PredictionGraphAgent(BaseAgent):
    """Build the KumoRFM graph/model for the relevant tables (prediction phase 1)."""

    def __init__(self):
        super().__init__("prediction_graph")

    def execute(self, state: AgentState) -> Dict[str, Any]:
        path_state = state.get("path_state", {})
        connectors = state.get("connectors", []) or []
        # Scope the KumoRFM graph to the tables candidate-preparation found relevant,
        # and use the catalog-derived join paths as the graph's table relationships.
        relevant_tables = path_state.get("relevant_tables") or []
        join_paths = path_state.get("attribute_join_paths") or []
        # Few-shot PQL examples retrieved from the verified PqlAnalysis corpus.
        examples = fetch_pql_examples(
            state.get("semantic_retriever"), get_standalone_question(state)
        )
        # Enrich the table set with any table the examples reference before the
        # graph is built, so the LLM can never cite a table absent from the graph.
        # Examples citing tables this database does not have are dropped here for
        # the same reason.
        relevant_tables, examples = _enrich_relevant_tables(relevant_tables, examples)

        try:
            context = build_prediction_context(
                connectors,
                relevant_tables,
                join_paths=join_paths,
                examples=examples,
            )
        except Exception as exc:
            self.logger.exception("KumoRFM graph preparation failed")
            context = _error_response(
                "这个问题的预测图没能准备好。"
                f"（{type(exc).__name__}: {exc}）"
            )

        if isinstance(context, PredictionContext):
            return {
                "decision": "predict_ready",
                "path_state": {**path_state, "prediction_context": context},
            }

        # build_prediction_context returned a graceful error response dict —
        # terminate the prediction path with it.
        markdown = context.get("response", "")
        return {
            "decision": "predict_failed",
            "messages": state["messages"] + [AIMessage(content=markdown)],
            "path_state": {
                **path_state,
                "formatted_response": markdown,
                "final_response": context,
            },
        }
