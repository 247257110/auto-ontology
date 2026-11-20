# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Pure helpers of the semantic-index reindex service."""

from __future__ import annotations

from auto_ontology.server.semantic_index.service import _count_rows, _doc_row


def test_doc_row_carries_certified_metadata() -> None:
    row = _doc_row(
        {
            "id": "attr-1",
            "label": "SqlAttribute",
            "name": "毛利率",
            "text": "sql_attribute: 毛利率",
            "source": "manual",
            "certified": True,
        },
        "wideworldimporters",
    )

    assert row["metadata"]["certified"] is True
    assert row["metadata"]["database_name"] == "wideworldimporters"
    assert row["metadata"]["label"] == "SqlAttribute"
    assert row["metadata"]["content_metadata"]["certified"] is True


def test_doc_row_defaults_to_uncertified() -> None:
    row = _doc_row(
        {"id": "ca-1", "label": "CustomAnalysis", "name": "客户营收排名", "text": "x"},
        None,
    )

    assert row["metadata"]["certified"] is False
    assert row["metadata"]["database_name"] is None


def test_count_rows_groups_by_label() -> None:
    rows = [
        _doc_row({"id": "a", "label": "Term", "name": "t", "text": "t"}, "db"),
        _doc_row({"id": "b", "label": "Term", "name": "u", "text": "u"}, "db"),
        _doc_row({"id": "c", "label": "SqlAttribute", "name": "v", "text": "v"}, "db"),
    ]

    assert _count_rows(rows) == {"Term": 2, "SqlAttribute": 1}
