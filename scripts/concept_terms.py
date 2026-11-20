# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Seed the cross-table *concept* dimensions the Wide World Importers ontology lacks.

A concept dimension is one business word whose correct answer is a **join path**,
not a column: "地域" is ``customers.deliverycityid → cities → stateprovinces``,
"品类" is the ``stockitemstockgroups`` many-to-many bridge, "销售员" is a foreign
key into the shared ``people`` table. Asked without them, the generator picks a
plausible-looking column and produces a query that runs and answers a different
question — grouping 663 customers by *city* instead of by state, or by
``customername`` when the question said 销售员.

Each concept is written as **two** objects, which is the whole trick:

1. a **Term** representing every table the path touches, so the concept is
   retrievable by its own name and synonyms;
2. a **certified SQL Attribute** under it whose expression *is* the join chain
   and the grouping key.

Step 2 is what makes it bindable. ``metric_grounding`` grounds the question's
dimensions against retrieved ontology objects and, when exactly one certified
SQL Attribute answers, emits ``binding: "required"`` — the prompt then pins the
join path instead of leaving the model to re-derive it. Creating the Term alone
would only add more text for the model to weigh.

Writes go through the backend REST API, not the catalog tables directly: the API
validates the SQL against the catalog and embeds what it writes. A row inserted
straight into Postgres has no vector and is therefore invisible to retrieval
(that is the failure mode ``POST /api/semantic-index/reindex`` exists to repair).

::

    python scripts/concept_terms.py --dry-run
    python scripts/concept_terms.py

    # Against a non-default deployment
    python scripts/concept_terms.py --api http://127.0.0.1:3001

Re-running is safe: an existing Term with the same name is left alone, and an
existing SqlAttribute with the same name is skipped. Use ``--update`` to rewrite
the definitions in place (SQL Attributes are matched by name and replaced).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from api_client import Api, ApiError, DEFAULT_API  # noqa: E402


@dataclass(frozen=True)
class Concept:
    """One cross-table business dimension, as Term + certified SqlAttribute."""

    name: str
    description: str
    synonyms: list[str]
    #: ``schema.table`` names the concept's path touches. Resolved to catalog
    #: ids at run time; a missing table is reported and the concept skipped,
    #: because a Term with no represented table is invisible to every read.
    tables: list[str]
    #: Name of the certified SQL Attribute that carries the join path.
    attribute: str
    #: What the attribute's SQL *proves*: the path, the grouping key, and the
    #: caliber that makes it non-obvious.
    attribute_description: str
    sql: str


CONCEPTS: tuple[Concept, ...] = (
    Concept(
        name="客户地域",
        description=(
            "按客户所在州省统计的销售区域。客户表只存城市 ID，不是城市名也不是"
            "州名；要得到“按州/按区域”的结果必须走 deliverycityid → cities → "
            "stateprovinces 三级 join。customers.deliverylocation 是 WKT 点串"
            "（POINT (经度 纬度)），不能当地域维度用。"
        ),
        synonyms=["地域", "地区", "区域", "州省", "省份", "所在州", "销售区域"],
        tables=[
            "sales.customers",
            "application.cities",
            "application.stateprovinces",
        ],
        attribute="客户地域（州省维度）",
        attribute_description=(
            "客户按州省聚合口径：customers.deliverycityid → cities.cityid → "
            "stateprovinces.stateprovinceid，分组键是 stateprovincename。"
            "不要用 customers.deliverylocation（WKT 点串），也不要绕过 cities 直接猜州名。"
        ),
        sql=(
            "SELECT sp.stateprovincename AS region_name, "
            "COUNT(DISTINCT c.customerid) AS customer_count "
            "FROM sales.customers c "
            "JOIN application.cities ct ON ct.cityid = c.deliverycityid "
            "JOIN application.stateprovinces sp "
            "ON sp.stateprovinceid = ct.stateprovinceid "
            "GROUP BY sp.stateprovincename"
        ),
    ),
    Concept(
        name="商品品类",
        description=(
            "按商品品类（stockgroup）统计的销售口径。商品与品类是多对多："
            "227 个商品对应 442 条映射，一个商品可属多个品类，直接按品类汇总"
            "会重复计数，必须经 stockitemstockgroups 桥表并在商品粒度去重。"
        ),
        synonyms=["品类", "类目", "商品类别", "stock group", "品类分组"],
        tables=[
            "warehouse.stockitems",
            "warehouse.stockitemstockgroups",
            "warehouse.stockgroups",
            "sales.invoicelines",
        ],
        attribute="商品品类（桥表维度）",
        attribute_description=(
            "商品按品类聚合口径：stockitems → stockitemstockgroups → stockgroups，"
            "分组键是 stockgroupname。桥表是多对多，跨品类求金额合计时必须先按"
            "stockitemid 去重或使用 AVG 之外的聚合语义，否则同一商品会被计入多个品类。"
        ),
        sql=(
            "SELECT sg.stockgroupname AS stock_group, "
            "SUM(DISTINCT il.extendedprice) AS revenue "
            "FROM sales.invoicelines il "
            "JOIN warehouse.stockitemstockgroups ss "
            "ON ss.stockitemid = il.stockitemid "
            "JOIN warehouse.stockgroups sg ON sg.stockgroupid = ss.stockgroupid "
            "GROUP BY sg.stockgroupname"
        ),
    ),
    Concept(
        name="销售员",
        description=(
            "销售业绩的归属人。销售员记在 invoices.salespersonpersonid / "
            "orders.salespersonpersonid 上，人员信息在 application.people；"
            "客户表没有销售员字段，只能从订单或发票反推。库里有两个人同名 Hudson"
            "（personid 3 和 13），按姓名分组会把两人业绩合并，必须按 personid 分组。"
        ),
        synonyms=["销售代表", "业务员", "salesperson", "sales rep", "业绩归属人"],
        tables=[
            "sales.invoices",
            "application.people",
            "sales.invoicelines",
        ],
        attribute="销售员业绩（按 personid 维度）",
        attribute_description=(
            "销售员业绩口径：invoices.salespersonpersonid → people.personid，"
            "分组键必须是 personid，显示时再取 fullname。按 fullname 分组会把"
            "同名的两位 Hudson 合并成一个冠军，排名结论直接错。"
        ),
        sql=(
            "SELECT p.personid AS salesperson_id, p.fullname AS salesperson_name, "
            "SUM(il.extendedprice) AS revenue "
            "FROM sales.invoicelines il "
            "JOIN sales.invoices i ON i.invoiceid = il.invoiceid "
            "JOIN application.people p ON p.personid = i.salespersonpersonid "
            "GROUP BY p.personid, p.fullname"
        ),
    ),
    Concept(
        name="冷链商品",
        description=(
            "需要冷藏运输的商品（ischillerstock = true），全库 227 个 SKU 中只有 8 个。"
            "冷链数据分三处：商品标志位在 warehouse.stockitems.ischillerstock，"
            "冷库温度历史在 coldroomtemperatures_archive（实时表只有 4 条），"
            "冷藏车温度在 vehicletemperatures。"
        ),
        synonyms=["冷藏品", "冷链", "chiller", "冷藏商品", "冷链商品"],
        tables=[
            "warehouse.stockitems",
            "warehouse.coldroomtemperatures_archive",
            "warehouse.vehicletemperatures",
            "sales.invoicelines",
        ],
        attribute="冷链商品（是否冷藏标志位）",
        attribute_description=(
            "冷链商品口径：warehouse.stockitems.ischillerstock 为 true 的商品，"
            "共 8 个 SKU。判断温度是否超阈值必须查 coldroomtemperatures_archive"
            "（120 万条历史），实时表冷库表只有 4 条，会误判成“没有数据”。"
        ),
        sql=(
            "SELECT s.stockitemid, s.stockitemname, "
            "SUM(il.extendedprice) AS revenue "
            "FROM warehouse.stockitems s "
            "JOIN sales.invoicelines il ON il.stockitemid = s.stockitemid "
            "WHERE s.ischillerstock = true "
            "GROUP BY s.stockitemid, s.stockitemname"
        ),
    ),
)


def _table_ids(api: Api, database: str) -> dict[str, str]:
    """``{"schema.table": id}`` for *database*, read off the catalog tree."""
    listing = api.get("/api/datasources/dbs")
    databases = listing.get("data") or []
    match = next((db for db in databases if db.get("name") == database), None)
    if match is None:
        available = ", ".join(db.get("name", "?") for db in databases)
        raise RuntimeError(
            f"database {database!r} is not in the catalog (have: {available})"
        )

    resolved: dict[str, str] = {}
    payload = api.get(f"/api/schemas/{match['id']}") or {}
    for schema in payload.get("schemas") or []:
        schema_id = schema.get("id")
        schema_name = schema.get("schema_name")
        if not schema_id or not schema_name:
            continue
        tables = api.get(f"/api/tables/{schema_id}")
        for table in tables.get("data") or []:
            name = table.get("name")
            if name:
                resolved[f"{schema_name}.{name}"] = table["id"]
    return resolved


def _existing_terms(api: Api) -> dict[str, dict[str, Any]]:
    listing = api.get("/api/terms?limit=1000")
    return {row["name"]: row for row in (listing.get("data") or [])}


def _attributes_of(api: Api, term_id: str) -> dict[str, dict[str, Any]]:
    listing = api.get(f"/api/terms/{term_id}/sql-attributes?limit=1000")
    return {row["name"]: row for row in (listing.get("data") or [])}


def apply(  # noqa: C901 — a linear seed loop reads better than a state machine
    api: Api,
    *,
    database: str,
    dry_run: bool,
    update: bool,
) -> int:
    table_ids = _table_ids(api, database)
    terms = _existing_terms(api)
    planned = 0

    for concept in CONCEPTS:
        missing = [name for name in concept.tables if name not in table_ids]
        if missing:
            print(
                f"skip {concept.name}: table(s) not in the catalog: {', '.join(missing)}"
            )
            continue

        term = terms.get(concept.name)
        if term is None:
            planned += 1
            print(
                f"{'would create' if dry_run else 'create'} Term {concept.name!r} "
                f"({len(concept.tables)} table(s))"
            )
            if not dry_run:
                created = api.post(
                    "/api/terms",
                    {
                        "name": concept.name,
                        "description": concept.description,
                        "synonyms": concept.synonyms,
                        "table_ids": [table_ids[name] for name in concept.tables],
                        "name_certified": True,
                        "description_certified": True,
                    },
                )
                term = created.get("data") or {}
        else:
            print(f"keep  Term {concept.name!r} (already exists)")

        term_id = term.get("id")
        if not term_id:
            print(f"skip {concept.name}: no term id")
            continue

        attributes = _attributes_of(api, term_id)
        existing = attributes.get(concept.attribute)
        body = {
            "name": concept.attribute,
            "description": concept.attribute_description,
            "expression": concept.sql,
            "term_id": term_id,
        }

        if existing is None:
            planned += 1
            print(
                f"{'would create' if dry_run else 'create'}   SqlAttribute "
                f"{concept.attribute!r}"
            )
            if not dry_run:
                # Validate first: the API refuses SQL that resolves to no
                # catalogued table, which is exactly the mistake worth catching
                # before a 422 arrives on create.
                api.post(
                    "/api/sql-attributes/validate",
                    {"expression": concept.sql, "term_id": term_id},
                )
                created = api.post("/api/sql-attributes", body)
                attr_id = (created.get("data") or {}).get("id")
                # Certification is a separate PATCH: the create shape has no
                # ``certified`` field, and it is the flag that lets grounding
                # bind this definition at all.
                if attr_id:
                    api.patch(f"/api/sql-attributes/{attr_id}", {"certified": True})
        elif update:
            planned += 1
            print(
                f"{'would update' if dry_run else 'update'}   SqlAttribute "
                f"{concept.attribute!r}"
            )
            if not dry_run:
                api.post("/api/sql-attributes/validate", body)
                api.put(f"/api/sql-attributes/{existing['id']}", body)
                api.patch(f"/api/sql-attributes/{existing['id']}", {"certified": True})
        else:
            print(f"keep  SqlAttribute {concept.attribute!r} (already exists)")

    if planned and not dry_run:
        # Every write above embeds its own row, so this is belt-and-braces for
        # the Term rows in particular: a Term created without a vector is
        # invisible, and the concept would silently do nothing.
        print("\nreindexing the semantic layer so the new concepts are retrievable…")
        report = api.post("/api/semantic-index/reindex", {"databases": [database]})
        print(json.dumps(report, ensure_ascii=False, indent=2))

    print(f"\n{planned} change(s) {'planned' if dry_run else 'applied'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api",
        default=DEFAULT_API,
        help="backend base URL (default: %(default)s)",
    )
    parser.add_argument(
        "--database",
        default="wideworldimporters",
        help="catalog database name (default: %(default)s)",
    )
    parser.add_argument("--dry-run", action="store_true", help="report, change nothing")
    parser.add_argument(
        "--update",
        action="store_true",
        help="rewrite existing concept SqlAttributes in place instead of skipping",
    )
    args = parser.parse_args()

    try:
        return apply(
            Api(args.api),
            database=args.database,
            dry_run=args.dry_run,
            update=args.update,
        )
    except (ApiError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
