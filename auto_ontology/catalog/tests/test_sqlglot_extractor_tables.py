# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""A table named in FROM/JOIN is used even when no column is traced to it.

``SELECT COUNT(*) FROM customers`` names no column of ``customers``, so the
extractor used to drop the only table it had found. Downstream that reads as
"the SQL references nothing in the catalog", which fails the whole question as
unconstructable after the SQL is rewritten a few times -- the rewrite keeps
producing ``COUNT(*)`` because that is what the question asked for.
"""

from __future__ import annotations

import pandas as pd

from auto_ontology.catalog.parsers.sqlglot_extractor import (
    extract_tables_and_columns,
)


class _FakeSchema:
    """The slice of ``Schema`` the extractor touches: existence + columns_df."""

    def __init__(self, name: str, tables: dict[str, list[str]]) -> None:
        self.name = name
        self._tables = {table.lower() for table in tables}
        self.columns_df = pd.DataFrame(
            [
                {"table_name": table, "column_name": column}
                for table, columns in tables.items()
                for column in columns
            ]
        )

    def table_exists(self, table_name: str) -> bool:
        return table_name.lower() in self._tables


SCHEMAS = {
    "retail": _FakeSchema(
        "retail",
        {"customers": ["customer_id", "company_name"], "orders": ["order_id"]},
    )
}


def _extract(sql: str):
    return extract_tables_and_columns(sql=sql, dialects=["postgres"], all_schemas=SCHEMAS)


def test_count_star_still_reports_its_table() -> None:
    extraction = _extract("SELECT COUNT(*) AS n FROM retail.customers")

    assert "retail.customers" in extraction.tables
    assert extraction.tables["retail.customers"].columns == set()
    assert extraction.tables["retail.customers"].schema_name == "retail"


def test_table_without_any_column_reference_is_kept() -> None:
    extraction = _extract("SELECT 1 FROM customers")

    assert list(extraction.tables) == ["customers"]


def test_every_comma_joined_table_is_kept() -> None:
    extraction = _extract("SELECT COUNT(*) FROM customers, orders")

    assert set(extraction.tables) == {"customers", "orders"}


def test_columns_are_still_attributed_when_present() -> None:
    extraction = _extract("SELECT company_name FROM retail.customers")

    assert extraction.tables["retail.customers"].columns == {"company_name"}


def test_column_less_table_is_kept_next_to_one_with_columns() -> None:
    extraction = _extract(
        "SELECT c.company_name FROM customers AS c CROSS JOIN orders AS o"
    )

    assert extraction.tables["customers"].columns == {"company_name"}
    assert extraction.tables["orders"].columns == set()
