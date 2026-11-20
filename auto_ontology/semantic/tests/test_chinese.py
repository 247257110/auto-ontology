# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from auto_ontology.semantic.chinese import (
    contains_cjk,
    format_aliases,
    normalize_aliases,
    normalize_chinese_text,
)


def test_normalize_chinese_text_handles_full_width_and_zero_width_text() -> None:
    assert normalize_chinese_text("  营收　（本月）\u200b ") == "营收 (本月)"


def test_normalize_aliases_deduplicates_without_translating() -> None:
    aliases = normalize_aliases(["收入", " 收入 ", "Revenue", "Ｒｅｖｅｎｕｅ"])

    assert aliases == ["收入", "Revenue"]
    assert format_aliases(aliases) == "收入, Revenue"


def test_contains_cjk() -> None:
    assert contains_cjk("客户收入")
    assert not contains_cjk("customer revenue")
