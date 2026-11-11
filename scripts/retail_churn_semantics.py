# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Write the business definitions the model needs into the catalog.

Column descriptions are what the text-to-SQL stage reads when it decides which
rows count. Two of them are not decoration:

* ``orders.status`` — cancelled orders are 3.5% of the headline revenue, and a
  query that forgets ``status = 'completed'`` reproduces a wrong number exactly.
* the **end of the data window** (2025-06-30). Without it, "customers who have
  not ordered in 90 days" is computed against ``current_date``, which for this
  dataset is ~500 days past the last order: every one of the 600 customers looks
  dormant, including the 361 that were buying on the final day.

The catalog keeps descriptions across re-ingestion (they are treated as
UI-owned), but not across a database rebuild, hence this file: re-run it after
``retail_churn_dataset.py`` recreates the catalogue.

::

    python scripts/retail_churn_semantics.py            # apply
    python scripts/retail_churn_semantics.py --dry-run  # show what would change

    # A point-in-time copy (scripts/retail_churn_asof.py) needs the same
    # descriptions with a different data end, or the model will measure recency
    # against the original 2025-06-30 and call every customer dormant.
    python scripts/retail_churn_semantics.py \
        --database retail_churn_asof_20240630 --data-end 2024-06-30
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))

from retail_churn_score import _load_env  # noqa: E402

DATABASE = "retail_churn"  # the connected business database, not where the catalog lives
DATA_END = "2025-06-30"

#: Substituted into the descriptions below. Every mention of the last business
#: date has to come from here — a copy whose catalog still claims 2025 makes
#: ``recency`` meaningless, and that is the single feature that matters most.
DATA_END_TOKEN = "{DATA_END}"


def _catalog_dsn() -> str:
    """The metadata database.

    ``retail_churn_score._dsn()`` points at the business data; the catalog lives
    in the application's own database (``POSTGRES_DATABASE``). Descriptions are
    catalog rows, so they are written here, not there.
    """
    import os

    return (
        f"host={os.environ.get('POSTGRES_HOST', 'localhost')} "
        f"port={os.environ.get('POSTGRES_PORT', '5432')} "
        f"user={os.environ.get('POSTGRES_USER', 'postgres')} "
        f"password={os.environ.get('POSTGRES_PASSWORD', '')} "
        f"dbname={os.environ.get('POSTGRES_DATABASE', 'auto_ontology')}"
    )

WINDOW = (
    "数据集覆盖 2021-01-01 至 " + DATA_END_TOKEN + "，最后一条业务记录止于此；"
    "计算「最近 N 天」时窗口终点必须取 " + DATA_END_TOKEN + "，不要用 current_date — "
    "否则每个客户都会被算成几百天未下单。"
)

DESCRIPTIONS: dict[tuple[str, str, str], str] = {
    ("retail", "orders", "status"): (
        "订单状态：completed 已成交（只有这些计入营收、下单行为和流失判定）、"
        "cancelled 已取消（不计营收）、pending 待处理。"
        "聚合金额或统计下单次数时必须过滤 status = 'completed'。"
    ),
    ("retail", "orders", "order_date"): (
        "下单日期。订单只有在 status = 'completed' 后才算一次有效下单。" + WINDOW
    ),
    ("retail", "orders", "order_total"): (
        "订单金额合计，等于该单所有明细行的行金额之和；"
        "仅当 status = 'completed' 时计入营收。"
    ),
    ("retail", "customers", "contract_type"): (
        "合同类型：monthly 月度滚动（基本每月下单）；"
        "annual 年度合同（一年集中采购 4~5 次，两次采购间隔 60~90 天属正常节奏，"
        "不应据此判断客户流失）；spot 现货零散采购。"
    ),
    ("retail", "customers", "segment"): (
        "客户分层：SMB 中小客户、MidMarket 中型客户、Enterprise 大型客户。"
    ),
    ("retail", "customers", "industry"): (
        "客户所属行业，决定其采购品类（餐饮/食品/商超等偏向生鲜与包装，"
        "电子制造/医药偏向原料与耗材）。"
    ),
    ("retail", "customers", "credit_limit"): "授信额度（元）。",
    ("retail", "customers", "payment_terms_days"): "账期天数，发票到期日为开票日加上该天数。",
    ("retail", "support_tickets", "csat"): "工单满意度评分，1（最差）到 5（最好）；空值表示未回访。",
    ("retail", "support_tickets", "priority"): "工单优先级：low / normal / high / urgent。",
    ("retail", "web_sessions", "quote_requests"): "该时段内客户在门户上发起的询价次数，是采购意向的先行指标。",
    ("retail", "web_sessions", "cart_abandonments"): "加购后未下单的次数。",
    ("retail", "invoices", "status"): (
        "发票状态：paid 已回款、open 未到期、overdue 逾期未付、void 作废。"
    ),
    ("retail", "v_customer_last_order", "last_order_date"): (
        "该客户最后一次 completed 订单的日期，用来衡量下单间隔与客户活跃度。"
        "★ 数据截止 " + DATA_END_TOKEN + "，不是今天：判断「最近多久没下单」时基准日必须取 "
        + DATA_END_TOKEN + "，写成 (DATE '" + DATA_END_TOKEN + "' - last_order_date)，"
        "不要用 CURRENT_DATE —— 否则所有客户都会被算成几百天没下单，包括最后一天还在采购的。"
        "Postgres 中两个 date 相减直接得到整数天数，不要再套 TO_DAYS 或 EXTRACT。"
    ),
    ("retail", "v_customer_revenue_monthly", "month"): (
        "月份（当月 1 日）。只有 status = 'completed' 的订单计入。" + WINDOW
    ),
}


def apply(dry_run: bool, database: str, data_end: str) -> int:
    _load_env()
    changed = 0
    missing: list[str] = []

    with psycopg.connect(_catalog_dsn(), autocommit=False) as conn:
        for (schema, table, column), template in DESCRIPTIONS.items():
            description = template.replace(DATA_END_TOKEN, data_end)
            rows = conn.execute(
                """
                SELECT c.id, c.description
                FROM catalog_column c
                JOIN catalog_table t ON t.id = c.table_id
                JOIN catalog_schema s ON s.id = t.schema_id
                JOIN catalog_database d ON d.id = s.database_id
                WHERE d.name = %s AND s.name = %s AND t.name = %s AND c.name = %s
                """,
                (database, schema, table, column),
            ).fetchall()

            if not rows:
                missing.append(f"{schema}.{table}.{column}")
                continue

            for column_id, current in rows:
                if current == description:
                    continue
                changed += 1
                print(f"{'would set' if dry_run else 'set'} {schema}.{table}.{column}")
                if dry_run:
                    continue
                conn.execute(
                    "UPDATE catalog_column SET description = %s WHERE id = %s",
                    (description, column_id),
                )

        if dry_run:
            conn.rollback()
        else:
            conn.commit()

    if missing:
        print("\nnot in the catalog (table filtered out, or not ingested yet):")
        for name in missing:
            print("  " + name)
    print(f"\n{changed} column description(s) {'to update' if dry_run else 'updated'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--database",
        default=DATABASE,
        help="catalog_database.name to write into (the connected business database)",
    )
    parser.add_argument(
        "--data-end",
        default=DATA_END,
        help="last business date in that database; substituted into the descriptions",
    )
    args = parser.parse_args()
    return apply(args.dry_run, args.database, args.data_end)


if __name__ == "__main__":
    raise SystemExit(main())
