# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Measure whether reranking helps candidate retrieval, per query language.

``RETRIEVAL_RERANK`` is off by default and ``RETRIEVAL_RERANK_LANGUAGES``
defaults to ``latin``, because the shipped reranker is a vision-language model
whose Chinese ranking has never been measured while the existing fusion *was*
built for Chinese character n-grams. This script is how that gets decided
instead of guessed.

It scores three rankers over one labelled set and reports them side by side:

* ``vector`` — the raw embedding order, the baseline everything else built on;
* ``hybrid`` — vector + lexical + certification, i.e. production today;
* ``rerank`` — hybrid with the cross-encoder bonus folded in, what
  ``RETRIEVAL_RERANK=1`` would produce.

Metrics are computed **per script** (``latin`` / ``cjk``) because that is the
question being asked: the global number can improve while Chinese regresses.

Input is JSONL, one case per line::

    {"query": "毛利率",
     "relevant": ["sa-margin"],
     "candidates": [{"id": "sa-margin", "text": "sql_attribute: 毛利率, ..."},
                    {"id": "other", "text": "..."}]}

``candidates`` must be what *retrieval* returned for the query, in any order —
the point is to compare orderings over the same pool. Export them from a live
deployment with ``scripts/semantic_search_probe.py`` or by hand for a regression
set; a fabricated pool measures nothing.

::

    python scripts/rerank_eval.py cases.jsonl
    python scripts/rerank_eval.py cases.jsonl --show-misses

Exit code is 1 when reranking is worse than hybrid on an enabled script — a
number a CI job can gate on.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auto_ontology.retrieval.data_access.reranking import (  # noqa: E402
    _RERANK_WEIGHT,
    _normalized,
    allowed_languages,
)
from auto_ontology.retrieval.data_access.semantic_search import (  # noqa: E402
    _hybrid_rank_rows,
)
from auto_ontology.semantic.chinese import contains_cjk  # noqa: E402
from auto_ontology.utils.rerank import get_rerank_kwargs, rerank_hits  # noqa: E402


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{line_number}: {exc.msg}") from exc
        if not case.get("query") or not case.get("candidates"):
            raise SystemExit(f"{path}:{line_number}: needs 'query' and 'candidates'")
        cases.append(case)
    return cases


def _relevant(case: dict[str, Any]) -> set[str]:
    return {str(item) for item in case.get("relevant") or []}


def _reciprocal_rank(ids: list[str], relevant: set[str]) -> float:
    for rank, item in enumerate(ids, start=1):
        if item in relevant:
            return 1.0 / rank
    return 0.0


def _hit_at(ids: list[str], relevant: set[str], k: int) -> float:
    return 1.0 if any(item in relevant for item in ids[:k]) else 0.0


def _ndcg(ids: list[str], relevant: set[str], k: int) -> float:
    gain = 0.0
    for rank, item in enumerate(ids[:k], start=1):
        if item in relevant:
            gain += 1.0 / math.log2(rank + 1)
    ideal = sum(
        1.0 / math.log2(rank + 1) for rank in range(1, min(len(relevant), k) + 1)
    )
    return gain / ideal if ideal else 0.0


def _vector_order(candidates: list[dict[str, Any]]) -> list[str]:
    """The baseline: embedding distance alone, best first."""

    def distance(candidate: dict[str, Any]) -> float:
        value = candidate.get("score")
        return float(value) if value is not None else float("inf")

    return [str(c["id"]) for c in sorted(candidates, key=distance)]


def _hybrid(candidates: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    rows = [dict(candidate) for candidate in candidates]
    return _hybrid_rank_rows(query, rows, len(rows))


def _rerank_bonus(query: str, rows: list[dict[str, Any]]) -> dict[str, float]:
    """The same bonus ``reranking.rerank_component`` would produce."""
    if len(rows) < 2:
        return {}
    try:
        ranked = rerank_hits(query, rows, top_n=len(rows), **get_rerank_kwargs())
    except Exception as exc:  # noqa: BLE001 — surface it, do not fail the run
        raise SystemExit(f"rerank call failed: {exc}") from exc
    raw = [float(hit.get("_rerank_score", float("-inf"))) for hit in ranked]
    bonuses: dict[str, float] = {}
    for hit, share in zip(ranked, _normalized(raw)):
        if hit.get("id") is None or not share:
            continue
        bonuses[str(hit["id"])] = round(share * _RERANK_WEIGHT, 6)
    return bonuses


def _script(query: str) -> str:
    return "cjk" if contains_cjk(query) else "latin"


def evaluate(cases: Iterable[dict[str, Any]], *, top_k: int, show_misses: bool) -> int:
    scores: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    misses: list[str] = []

    for case in cases:
        query = str(case["query"])
        relevant = _relevant(case)
        if not relevant:
            continue
        script = _script(query)
        candidates = case["candidates"]

        hybrid_rows = _hybrid(candidates, query)
        bonuses = _rerank_bonus(query, hybrid_rows)
        rerank_rows = _hybrid_rank_rows(
            query, [dict(r) for r in hybrid_rows], len(hybrid_rows), bonuses
        )

        orderings = {
            "vector": _vector_order(candidates),
            "hybrid": [str(row["id"]) for row in hybrid_rows],
            "rerank": [str(row["id"]) for row in rerank_rows],
        }
        for name, ids in orderings.items():
            scores[script][name].append(_reciprocal_rank(ids, relevant))
            scores[script][name].append(_hit_at(ids, relevant, top_k))
            scores[script][name].append(_ndcg(ids, relevant, top_k))

        if show_misses and not _hit_at(orderings["rerank"], relevant, top_k):
            misses.append(
                f"[{script}] {query!r}: rerank top-{top_k} = "
                f"{orderings['rerank'][:top_k]}, relevant = {sorted(relevant)}"
            )

    if not scores:
        print("no scored cases (every case needs a non-empty 'relevant')")
        return 0

    enabled = allowed_languages()
    regressions = 0
    print(f"metric groups: MRR / hit@{top_k} / nDCG@{top_k}\n")
    for script in sorted(scores):
        print(f"== {script} ({len(scores[script]['vector']) // 3} case(s)) ==")
        mrr_hit_ndcg: dict[str, tuple[float, float, float]] = {}
        for name in ("vector", "hybrid", "rerank"):
            values = scores[script][name]
            if not values:
                continue
            mrr = sum(values[0::3]) / len(values[0::3])
            hit = sum(values[1::3]) / len(values[1::3])
            ndcg = sum(values[2::3]) / len(values[2::3])
            mrr_hit_ndcg[name] = (mrr, hit, ndcg)
            print(f"  {name:7} MRR={mrr:.4f}  hit={hit:.4f}  nDCG={ndcg:.4f}")

        if "hybrid" in mrr_hit_ndcg and "rerank" in mrr_hit_ndcg:
            base, reranked = mrr_hit_ndcg["hybrid"], mrr_hit_ndcg["rerank"]
            delta = reranked[0] - base[0]
            verdict = (
                "better"
                if delta > 1e-9
                else "neutral"
                if abs(delta) <= 1e-9
                else "WORSE"
            )
            print(f"  rerank vs hybrid: MRR {delta:+.4f} ({verdict})")
            if delta < -1e-9 and script in enabled:
                regressions += 1
        print()

    if misses:
        print("misses (rerank did not surface a relevant id in top-k):")
        for line in misses:
            print(f"  {line}")
        print()

    if regressions:
        print(
            f"{regressions} enabled script(s) regressed. Either drop them from "
            "RETRIEVAL_RERANK_LANGUAGES or keep RETRIEVAL_RERANK off.",
            file=sys.stderr,
        )
        return 1
    print(f"enabled scripts: {sorted(enabled)} — no enabled regression.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", type=Path, help="JSONL evaluation set")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--show-misses",
        action="store_true",
        help="list queries where rerank did not surface a relevant id",
    )
    args = parser.parse_args()
    return evaluate(
        load_cases(args.cases), top_k=args.top_k, show_misses=args.show_misses
    )


if __name__ == "__main__":
    raise SystemExit(main())
