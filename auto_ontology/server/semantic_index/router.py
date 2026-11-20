# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""API route for rebuilding the semantic vector index.

This is the backend of the Settings "rebuild index" action: it re-embeds the
semantic layer (Term / ColumnAttribute / SqlAttribute / CustomAnalysis /
PqlAnalysis) so description, synonym and certification edits made after the
original compile actually land in the vectors retrieval searches.

Like the ``semantic-compilation`` routes, this is an administration operation
that runs on the backend (port 3001), which must stay on a private network.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from auto_ontology.server.semantic_index.service import reindex_semantic_nodes

router = APIRouter()


class SemanticReindexRequest(BaseModel):
    databases: list[str] | None = None
    labels: list[str] | None = None
    node_ids: list[str] | None = None
    dry_run: bool = False


@router.post("/semantic-index/reindex")
def reindex(body: SemanticReindexRequest) -> dict:
    """Preview (``dry_run=true``) or execute a semantic-layer vector rebuild.

    ``labels`` defaults to every semantic label (see ``SEMANTIC_LABELS``); pass
    an explicit list to rebuild a subset. ``databases`` scopes the rebuild to
    those databases; omit it to rebuild all. ``node_ids`` narrows to specific
    node ids.

    A real run refuses loudly when the embedding endpoint is unavailable or
    nothing matched, rather than reporting success.
    """
    return reindex_semantic_nodes(
        databases=body.databases,
        labels=body.labels,
        node_ids=body.node_ids,
        dry_run=body.dry_run,
    )
