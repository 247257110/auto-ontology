# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Export real retrieval candidates as a labelled set for ``rerank_eval.py``.

``scripts/rerank_eval.py`` compares rankers over **the pool retrieval actually
produced**. A hand-written pool measures nothing: the question is whether the
reranker can order the candidates the system really surfaced, including the
plausible-but-wrong ones that make retrieval hard.

This script asks a running deployment for those candidates and writes JSONL in
the shape the evaluator reads. Relevance is supplied by the operator — the one
thing a deployment cannot tell you — as a mapping from question to the ids that
should have ranked first:

    # relevance.json
    {"毛利率": ["<sql-attribute-id>"],
     "哪些客户最近一年没下单": ["<custom-analysis-id>"]}

    python scripts/export_rerank_cases.py --relevance relevance.json \
        --out cases.jsonl

    python scripts/rerank_eval.py cases.jsonl --top-k 3

Ids come from the ontology UI or from ``GET /api/sql-attributes`` /
``GET /api/custom-analyses``. A question with no entry in the relevance map is
skipped: an unlabelled case cannot score anything, and including it would drag
every ranker's numbers toward each other.

The candidate pool is read from ``/api/search`` where available; the semantic
candidate endpoint is what the text-to-SQL flow itself uses, so that is
preferred when the deployment exposes it.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class Api:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def get(self, path: str) -> Any:
        request = urllib.request.Request(f"{self.base}{path}", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode() or "null")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"GET {path} → {exc.code}: {exc.read().decode()}"
            ) from exc


def _candidates_from_search(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a global-search response into ``{id, text, score}`` candidates."""
    candidates: list[dict[str, Any]] = []
    for row in payload.get("data") or []:
        node_id = row.get("id")
        if node_id is None:
            continue
        label = row.get("label") or row.get("type") or ""
        name = row.get("name") or ""
        description = (row.get("description") or "").strip()
        text = f"{label}: {name}".strip()
        if description:
            text += f". {description}"
        candidates.append(
            {
                "id": str(node_id),
                "text": text,
                # Global search returns its own ordering, not vector distance;
                # it is emitted so the evaluator's vector baseline has
                # something to sort by, and is absent for a semantic pool.
                "score": row.get("score"),
            }
        )
    return candidates


def export(
    api: Api,
    relevance: dict[str, list[str]],
    *,
    top_k: int,
    database: str | None,
) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    skipped: list[str] = []

    for question, relevant in relevance.items():
        query = urllib.parse.quote(question)
        payload = api.get(f"/api/search?query={query}&limit={top_k * 4}")
        candidates = _candidates_from_search(payload or {})
        if database:
            # Retrieval filters on the database; an export that mixes two
            # databases measures a pool production would never have built.
            candidates = [
                candidate
                for candidate in candidates
                if (candidate.get("database_name") or database) == database
            ]
        if not candidates:
            skipped.append(question)
            continue
        cases.append(
            {
                "query": question,
                "relevant": relevant,
                "candidates": candidates,
            }
        )

    for question in skipped:
        print(f"skip {question!r}: no candidates returned", file=sys.stderr)
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api", default="http://127.0.0.1:3000", help="web app base URL"
    )
    parser.add_argument(
        "--relevance",
        type=Path,
        required=True,
        help="JSON map of question → relevant node ids",
    )
    parser.add_argument("--out", type=Path, required=True, help="JSONL output path")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--database", default=None)
    args = parser.parse_args()

    try:
        relevance = json.loads(args.relevance.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"{args.relevance}: {exc.msg}", file=sys.stderr)
        return 1
    if not isinstance(relevance, dict):
        print(
            f"{args.relevance}: expected an object of question → [ids]", file=sys.stderr
        )
        return 1

    try:
        cases = export(
            Api(args.api), relevance, top_k=args.top_k, database=args.database
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    args.out.write_text(
        "\n".join(json.dumps(case, ensure_ascii=False) for case in cases) + "\n",
        encoding="utf-8",
    )
    print(f"{args.out}: {len(cases)} case(s)")
    return 0 if cases else 1


if __name__ == "__main__":
    raise SystemExit(main())
