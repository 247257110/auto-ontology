# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Small, dependency-free helpers for Chinese semantic text.

These helpers deliberately normalize text without translating it.  Translation
would make the catalog less faithful to the business language and is not
needed for the existing multilingual embedding model.  The functions are used
at the boundaries of the semantic layer so the rest of the retrieval pipeline
can remain unchanged.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_WHITESPACE_RE = re.compile(r"\s+")


def contains_cjk(text: str) -> bool:
    """Return whether *text* contains at least one CJK ideograph."""
    return bool(_CJK_RE.search(text))


def normalize_chinese_text(text: str) -> str:
    """Normalize user/catalog text while preserving its original language.

    NFKC handles full-width Latin characters and punctuation commonly mixed
    into Chinese questions.  Whitespace and zero-width characters are removed
    deterministically; Chinese wording, aliases, and identifiers are not
    translated or stemmed.
    """
    normalized = unicodedata.normalize("NFKC", text or "")
    normalized = normalized.replace("\u200b", "").replace("\ufeff", "")
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def normalize_aliases(aliases: Iterable[str] | None) -> list[str]:
    """Normalize and deduplicate aliases, retaining their first-seen order."""
    result: list[str] = []
    seen: set[str] = set()
    for alias in aliases or []:
        normalized = normalize_chinese_text(str(alias))
        key = normalized.casefold()
        if normalized and key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def format_aliases(aliases: Iterable[str] | None) -> str:
    """Return a compact, stable alias block for embedding text."""
    values = normalize_aliases(aliases)
    return ", ".join(values)


__all__ = [
    "contains_cjk",
    "format_aliases",
    "normalize_aliases",
    "normalize_chinese_text",
]
