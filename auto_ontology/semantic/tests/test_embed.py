# SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the pure helpers in auto_ontology.semantic.embed."""

from __future__ import annotations

from auto_ontology.semantic.embed import _build_rows, _format_sample_values


def test_format_sample_values_handles_legacy_json_string_and_native_list() -> None:
    # Legacy Column nodes still store sample_values as a JSON string.
    assert _format_sample_values('["a", "b"]') == " Sample values: a, b."
    # Current writers store a native list.
    assert _format_sample_values(["a", "b"]) == " Sample values: a, b."


def test_format_sample_values_filters_long_values_and_handles_empty() -> None:
    long_value = "x" * 31
    assert _format_sample_values([long_value, "ok"]) == " Sample values: ok."
    assert _format_sample_values(None) == ""
    assert _format_sample_values([]) == ""
    assert _format_sample_values(["a", None, "b"]) == " Sample values: a, b."
    assert _format_sample_values('["a", null, "b"]') == " Sample values: a, b."


def _attr_text(**attr: object) -> str:
    base = {"id": "a1", "name": "Total", "term_name": "Order", "source_column": "t"}
    (row,) = _build_rows("shop", {}, [{**base, **attr}])
    return row["text"]


def test_the_columns_description_is_embedded_not_the_attributes() -> None:
    text = _attr_text(
        description="The long attribute description",
        column_description="Order total",
        sample_values="[10, 20]",
    )

    assert (
        text
        == "ColumnAttribute: Total of Term Order. Order total Sample values: 10, 20."
    )


def test_the_family_is_named_after_the_term() -> None:
    (row,) = _build_rows(
        "shop",
        {"synonyms": ["Purchase"]},
        [
            {
                "id": "a1",
                "name": "Total",
                "term_name": "Order",
                "column_description": "Order total",
                "family_prefix": "NEW_",
            }
        ],
    )

    assert row["text"] == (
        "ColumnAttribute: Total of Term Order (Purchase), family NEW_. Order total"
    )


def test_an_undocumented_column_embeds_its_name_and_samples() -> None:
    assert _attr_text(sample_values='["a"]') == (
        "ColumnAttribute: Total of Term Order.  Sample values: a."
    )


def test_a_certified_attribute_is_flagged_in_metadata() -> None:
    (row,) = _build_rows(
        "shop",
        {},
        [
            {
                "id": "a1",
                "name": "Total",
                "term_name": "Order",
                "source_column": "total",
                "certified": True,
            }
        ],
    )
    assert row["metadata"]["certified"] is True
    assert row["metadata"]["content_metadata"]["certified"] is True


def test_an_uncertified_attribute_carries_no_certified_flag() -> None:
    (row,) = _build_rows(
        "shop",
        {},
        [
            {
                "id": "a1",
                "name": "Total",
                "term_name": "Order",
                "source_column": "total",
            }
        ],
    )
    assert "certified" not in row["metadata"]


def test_a_term_is_certified_only_when_name_and_description_are() -> None:
    from auto_ontology.semantic.embed import _term_certified

    assert _term_certified({"name_certified": True, "description_certified": True})
    assert not _term_certified({"name_certified": True, "description_certified": False})
    assert not _term_certified({"name_certified": False, "description_certified": True})
    assert not _term_certified({})
    assert _term_certified({"certified": True})
    assert not _term_certified({"certified": False})
