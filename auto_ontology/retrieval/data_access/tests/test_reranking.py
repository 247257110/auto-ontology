# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Reranking is an *addition* to the retrieval fusion, and it fails open.

Two properties are load-bearing and pull against each other:

* a cross-encoder settles close calls that a rank-based fusion cannot;
* the fusion encodes two deliberate signals — lexical evidence and
  certification — that a plain reorder would silently erase.

So the reranker contributes a bounded bonus into the same sum. These tests pin
both the blend and every path where it must contribute nothing at all.
"""

from __future__ import annotations

import pytest

from auto_ontology.retrieval.data_access import reranking, semantic_search
from auto_ontology.retrieval.data_access.reranking import (
    _normalized,
    allowed_languages,
    is_rerank_applicable,
    is_retrieval_rerank_enabled,
    rerank_component,
)
from auto_ontology.retrieval.data_access.semantic_search import _hybrid_rank_rows


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch):
    for name in ("RETRIEVAL_RERANK", "RETRIEVAL_RERANK_LANGUAGES", "RERANK_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)


def _enable(monkeypatch: pytest.MonkeyPatch, languages: str = "latin,cjk") -> None:
    monkeypatch.setenv("RETRIEVAL_RERANK", "true")
    monkeypatch.setenv("RETRIEVAL_RERANK_LANGUAGES", languages)
    monkeypatch.setattr(
        reranking,
        "get_rerank_kwargs",
        lambda: {
            "rerank_invoke_url": "https://rerank.example/reranking",
            "model_name": "reranker",
            "api_key": "",
        },
    )


ROWS = [
    {"id": "a", "text": "ColumnAttribute: 营收 of Term Invoice"},
    {"id": "b", "text": "ColumnAttribute: 毛利率 of Term Invoice"},
    {"id": "c", "text": "ColumnAttribute: 客户数 of Term Customer"},
]


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------


def test_reranking_is_off_by_default() -> None:
    assert is_retrieval_rerank_enabled() is False
    assert rerank_component("gross margin", ROWS) == {}


def test_only_latin_is_enabled_by_default() -> None:
    assert allowed_languages() == {"latin"}
    assert is_rerank_applicable("gross margin") is True
    assert is_rerank_applicable("毛利率") is False


def test_language_allowlist_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RETRIEVAL_RERANK_LANGUAGES", " latin , CJK ")

    assert allowed_languages() == {"latin", "cjk"}
    assert is_rerank_applicable("毛利率") is True


# ---------------------------------------------------------------------------
# Every path that must contribute nothing
# ---------------------------------------------------------------------------


def test_no_endpoint_means_no_component(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETRIEVAL_RERANK", "1")
    monkeypatch.setattr(
        reranking,
        "get_rerank_kwargs",
        lambda: {"rerank_invoke_url": "", "model_name": "", "api_key": ""},
    )

    assert rerank_component("gross margin", ROWS) == {}


def test_a_cjk_query_is_skipped_unless_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped reranker's Chinese ranking is unmeasured; the fusion is not."""
    _enable(monkeypatch, languages="latin")
    called: list[str] = []
    monkeypatch.setattr(
        reranking, "rerank_hits", lambda *a, **k: called.append("x") or []
    )

    assert rerank_component("毛利率", ROWS) == {}
    assert called == []


def test_one_candidate_is_not_worth_a_call(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    called: list[str] = []
    monkeypatch.setattr(
        reranking, "rerank_hits", lambda *a, **k: called.append("x") or []
    )

    assert rerank_component("gross margin", ROWS[:1]) == {}
    assert called == []


def test_a_failing_reranker_keeps_the_fused_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(
        reranking,
        "rerank_hits",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("502 Bad Gateway")),
    )

    assert rerank_component("gross margin", ROWS) == {}


def test_a_degenerate_response_is_no_opinion(monkeypatch: pytest.MonkeyPatch) -> None:
    """All-equal scores must not hand every row the same bonus."""
    _enable(monkeypatch)
    monkeypatch.setattr(
        reranking,
        "rerank_hits",
        lambda query, hits, **k: [{**hit, "_rerank_score": 3.0} for hit in hits],
    )

    assert rerank_component("gross margin", ROWS) == {}


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def test_normalization_keeps_only_the_relative_order() -> None:
    assert _normalized([10.0, 20.0, 30.0]) == [0.0, 0.5, 1.0]
    assert _normalized([-5.0, 5.0]) == [0.0, 1.0]


def test_normalization_treats_a_gap_as_no_opinion() -> None:
    assert _normalized([7.0, 7.0, 7.0]) == [0.0, 0.0, 0.0]


def test_normalization_marks_unreturned_documents_lowest() -> None:
    assert _normalized([float("-inf"), 1.0, 2.0]) == [0.0, 0.0, 1.0]


# ---------------------------------------------------------------------------
# The blend
# ---------------------------------------------------------------------------


def test_a_reranker_settles_an_adjacent_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    """The job it is there for: two near-identical candidates, one position apart.

    A larger jump is deliberately out of reach — the bonus is worth
    ``weight * len(rows)`` positions, so promoting the last of eight rows to
    first would take a weight the fusion should not hand out.
    """
    _enable(monkeypatch)
    rows = [{"id": f"r{i}", "text": "ColumnAttribute: 收入"} for i in range(8)]

    def fake_rerank(query, hits, **_kwargs):
        # Prefer the second row over the first, and nothing else.
        ranked = [
            {**hits[1], "_rerank_score": 9.0},
            {**hits[0], "_rerank_score": 1.0},
        ]
        ranked.extend({**hit, "_rerank_score": 0.0} for hit in hits[2:])
        return ranked

    monkeypatch.setattr(reranking, "rerank_hits", fake_rerank)

    ranked = _hybrid_rank_rows(
        "收入", [dict(r) for r in rows], len(rows), rerank_component("收入", rows)
    )

    assert [row["id"] for row in ranked[:2]] == ["r1", "r0"]
    assert ranked[0]["rerank_bonus"] == pytest.approx(reranking._RERANK_WEIGHT)


def test_certification_outweighs_the_reranker_by_construction() -> None:
    """Both bonuses scale with pool size, so their *ratio* is the invariant.

    Certification is a statement a human made about a definition. It has to
    carry more weight than "the encoder likes this one", or certifying anything
    would be advisory.
    """
    assert reranking._RERANK_WEIGHT < semantic_search.CERTIFIED_RANK_BONUS


def test_a_reranker_cannot_demote_a_certified_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Realistic pool (retrieval fetches k*3 rows): certification still wins.

    The rival starts one vector position ahead, the reranker is maximally in
    its favour and maximally against the certified row, and the certified row
    still leads — because 0.05 * 30 positions outweighs the one-position gap.
    """
    _enable(monkeypatch, languages="cjk")
    rows = [{"id": f"r{i}", "text": "ColumnAttribute: 毛利率"} for i in range(30)]
    rows[0] = {**rows[0], "certified": False}
    rows[1] = {**rows[1], "certified": True}

    def fake_rerank(query, hits, **_kwargs):
        ranked = [{**hits[0], "_rerank_score": 9.0}]
        ranked.append({**hits[1], "_rerank_score": -9.0})
        ranked.extend({**hit, "_rerank_score": 0.0} for hit in hits[2:])
        return ranked

    monkeypatch.setattr(reranking, "rerank_hits", fake_rerank)

    ranked = _hybrid_rank_rows(
        "毛利率", [dict(r) for r in rows], len(rows), rerank_component("毛利率", rows)
    )

    assert ranked[0]["id"] == "r1"


def test_a_reranker_cannot_override_an_exact_lexical_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exact name match (1.0) outweighs the whole rerank range (0.5)."""
    _enable(monkeypatch)
    rows = [
        {"id": "exact", "text": "sql_attribute: 毛利率"},
        {"id": "other", "text": "sql_attribute: 营收"},
    ]
    monkeypatch.setattr(
        reranking,
        "rerank_hits",
        lambda query, hits, **k: [
            {**hits[1], "_rerank_score": 9.0},
            {**hits[0], "_rerank_score": -9.0},
        ],
    )

    ranked = _hybrid_rank_rows(
        "毛利率", [dict(r) for r in rows], 2, rerank_component("毛利率", rows)
    )

    assert ranked[0]["id"] == "exact"


def test_no_bonus_reproduces_the_previous_fusion() -> None:
    """The integration is inert unless the reranker actually contributes."""
    rows = [
        {"id": "a", "text": "ColumnAttribute: 毛利率"},
        {"id": "b", "text": "ColumnAttribute: 毛利率", "certified": True},
        {"id": "c", "text": "ColumnAttribute: 营收"},
    ]

    with_bonus = _hybrid_rank_rows("毛利率", [dict(r) for r in rows], 3, {})
    without = _hybrid_rank_rows("毛利率", [dict(r) for r in rows], 3)

    assert [r["id"] for r in with_bonus] == [r["id"] for r in without]
    assert all("rerank_bonus" not in row for row in with_bonus)


def test_search_applies_the_component_without_network_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retrieval must not acquire an HTTP dependency when the flag is unset."""

    class _Retriever:
        vdb_kwargs: dict = {}

        def query(self, *_args, **_kwargs):
            return [
                {
                    "text": "ColumnAttribute: 毛利率",
                    "_distance": 0.1,
                    "metadata": {"id": "sa-1", "label": "ColumnAttribute"},
                }
            ]

    called: list[str] = []
    monkeypatch.setattr(
        semantic_search, "rerank_component", lambda *a, **k: called.append("x") or {}
    )

    rows = semantic_search.search_semantic_index(
        _Retriever(), "毛利率", label_filter=["ColumnAttribute"]
    )

    assert rows[0]["id"] == "sa-1"
    # Called, and it short-circuits internally because the flag is unset.
    assert called == ["x"]
