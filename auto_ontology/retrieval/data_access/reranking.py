# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Optional cross-encoder reranking for the main candidate search.

The retrieval stage fuses vector distance with lexical evidence and a
certification bonus (see ``semantic_search._hybrid_rank_rows``). That fusion is
deliberately coarse: vector distance is not calibrated across backends, so the
order is essentially a rank. A cross-encoder reads the query and each candidate
*together* and is much better at "which of these five near-identical columns is
the one the question means".

Two things make that worth having only as an *addition*, never a replacement:

* The certification bonus and the CJK lexical fusion are deliberate signals. A
  reranker that simply reorders the list would erase both — a signed-off
  definition could be pushed below an uncertified lookalike purely on the
  encoder's opinion, undoing the point of certifying anything.
* The default reranker in ``.env.example`` is a **vision-language** model whose
  ranking quality on Chinese is unverified. Overwriting a fusion built
  specifically for Chinese character n-grams with an unmeasured score is a
  regression dressed as an improvement.

So :func:`rerank_component` returns a bounded *bonus*, folded into the same
fusion the rest of the ranker uses. And Chinese queries are excluded unless an
operator has measured them — ``RETRIEVAL_RERANK_LANGUAGES`` — which is what
``scripts/rerank_eval.py`` exists to inform.

**What the bonus can and cannot do.** It is worth ``_RERANK_WEIGHT * len(rows)``
positions, so it adjusts the *top* of the fused order — where the decision that
matters is made, since only ``per_label_k`` rows reach the prompt — and cannot
lift the last of forty candidates to first. That is the intended scope: a
tie-breaker for the near-identical candidates a query really confuses, not a
second, unaccountable ranking. ``scripts/rerank_eval.py`` reports MRR/hit@k/nDCG
per script so the size of that effect is measured rather than assumed.

Everything here fails open: a missing endpoint, a 500, or a malformed response
leaves the original ordering untouched. Reranking is an improvement to ranking,
and ranking must survive without it.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Iterable

from auto_ontology.semantic.chinese import contains_cjk
from auto_ontology.utils.rerank import get_rerank_kwargs, rerank_hits

logger = logging.getLogger(__name__)

#: Weight of the reranker inside the fusion, in the same units as
#: ``_lexical_score`` (0..2) and ``CERTIFIED_RANK_BONUS`` (0.25). Kept
#: **strictly below** the certification bonus on purpose: both are multiplied by
#: the pool size, so the ratio is pool-independent and certification always
#: retains a larger influence than "the encoder likes this one".
#:
#: It is also below the gap between a full and a partial lexical match, so it
#: settles close calls rather than displacing real textual evidence.
#:
#: Worth ``weight * len(rows)`` positions in the fused order, which is what
#: makes it useful on the 10–30 row pools retrieval actually produces.
_RERANK_WEIGHT = 0.2

#: Query scripts the reranker is trusted with, by default. Latin only: the
#: shipped reranker is a VL model and its Chinese ranking is unmeasured, while
#: the hybrid ranker's CJK n-gram fusion *was* built for it. Add ``cjk`` once
#: ``scripts/rerank_eval.py`` shows it helps.
_DEFAULT_LANGUAGES = "latin"


def is_retrieval_rerank_enabled() -> bool:
    """Whether candidate search should consult the reranker at all.

    Off by default. The eval harness is what should turn this on: reranking
    costs one HTTP round trip per searched entity, so it belongs on a
    deployment whose retrieval has been measured, not on every deployment by
    default.
    """
    raw = os.environ.get("RETRIEVAL_RERANK", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def allowed_languages() -> set[str]:
    """Scripts the reranker may be applied to (``{"latin"}``, ``{"cjk"}``, …)."""
    raw = os.environ.get("RETRIEVAL_RERANK_LANGUAGES", _DEFAULT_LANGUAGES)
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def _script_of(text: str) -> str:
    return "cjk" if contains_cjk(text) else "latin"


def is_rerank_applicable(query: str) -> bool:
    """Whether *query*'s script is one the operator has enabled."""
    return _script_of(query) in allowed_languages()


def _has_endpoint() -> bool:
    return bool(get_rerank_kwargs().get("rerank_invoke_url"))


def _normalized(scores: Iterable[float]) -> list[float]:
    """Min-max the raw scores into ``[0, 1]``; all-equal maps to ``1.0``.

    Reranker scores are logits (NIM) or relevance scores (inference-api), and
    their ranges differ by orders of magnitude between the two. Only their
    *relative* order is portable, so that is all this keeps.
    """
    values = list(scores)
    if not values:
        return []
    finite = [value for value in values if value != float("-inf")]
    if not finite:
        return [0.0 for _ in values]
    low, high = min(finite), max(finite)
    if high - low <= 1e-9:
        # Every candidate scored the same. Handing them all the full bonus
        # would let a degenerate response reorder nothing but still displace
        # the lexical signal, so treat it as "no opinion".
        return [0.0 for _ in values]
    return [
        0.0 if value == float("-inf") else (value - low) / (high - low)
        for value in values
    ]


def rerank_component(
    query: str,
    rows: list[dict[str, Any]],
    *,
    text_key: str = "text",
) -> dict[str, float]:
    """``{row_id: bonus}`` for the rows the reranker distinguishes, else ``{}``.

    An empty dict is the normal, non-exceptional answer: reranking disabled, an
    endpoint that is not configured, a script the operator has not enabled, a
    single candidate (nothing to order), or any failure talking to the service.
    The caller then keeps its existing fusion unchanged.
    """
    if not rows:
        return {}
    if not is_retrieval_rerank_enabled():
        return {}
    if len(rows) < 2:
        # One candidate cannot be reordered, and the call is not free.
        return {}
    if not is_rerank_applicable(query):
        logger.debug(
            "Rerank skipped: query script %r is not in RETRIEVAL_RERANK_LANGUAGES=%r",
            _script_of(query),
            sorted(allowed_languages()),
        )
        return {}
    if not _has_endpoint():
        logger.info("Rerank enabled but no RERANK_ENDPOINT is configured; skipping")
        return {}

    try:
        ranked = rerank_hits(query, rows, top_n=len(rows), **get_rerank_kwargs())
    except Exception:  # noqa: BLE001 — ranking must survive a dead reranker
        logger.warning("Rerank call failed; keeping the fused ordering", exc_info=True)
        return {}

    raw = [float(hit.get("_rerank_score", float("-inf"))) for hit in ranked]
    bonuses: dict[str, float] = {}
    for hit, share in zip(ranked, _normalized(raw)):
        row_id = hit.get("id")
        if row_id is None or not share:
            continue
        bonuses[str(row_id)] = round(share * _RERANK_WEIGHT, 6)
    if not bonuses:
        logger.info(
            "Rerank returned no usable scores for %d candidate(s); "
            "keeping the fused ordering",
            len(rows),
        )
    return bonuses


__all__ = [
    "allowed_languages",
    "is_rerank_applicable",
    "is_retrieval_rerank_enabled",
    "rerank_component",
]
