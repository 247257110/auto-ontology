# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Seed the analysis *patterns* a question shape needs, not just metric formulas.

A SqlAttribute pins one metric's formula (``毛利率``). A CustomAnalysis teaches a
whole query shape — multi-table joins, window functions, bucketing, anti-joins —
and it reaches SQL generation *and* intent validation. Asking "按 30/60/90 天
拆一下应收账龄" with no such pattern in the catalog leaves the model to invent the
bucketing, and it invents a different one every time.

Six patterns were already seeded by hand (ranking, trend, ABC, funnel,
salesperson, latency buckets). These are the six question shapes from the data
dictionary that were still uncovered, each verified against the real database
before being written here:

===========================  ==========================================  =============
 pattern                      what it teaches                             verified
===========================  ==========================================  =============
 应收账龄分桶                  aging buckets off a running-balance ledger   267,011.44
 回款率（开票 vs 收款）         two legs off one signed ledger               99.87%
 客户应收对账                  per-entity ledger reconciliation             39 customers
 订单到发票转化率               anti-join ratio off a LEFT JOIN               95.81%
 负毛利商品                    HAVING on an aggregate (finds real losses)    4 SKUs
 盘点差异                      filter by transaction-type *name*, not id     -18 units
===========================  ==========================================  =============

**Judgment calls, recorded so nobody "helpfully" re-adds them.** Five candidate
shapes were measured and deliberately left out, because a pattern whose
verified output is degenerate teaches the wrong thing — the same reasoning
``docs/wideworldimporters-数据字典与问法.md`` §9.4 applies to its own examples:

* 客户流失 / RFM 分层 — every one of the 663 customers bought within 61 days of
  the data end, so stratification yields one bucket.
* 库存周转天数 — median stock on hand is 76,373 units against a median lifetime
  issue of 6,111, so "turnover days" is over 10,000 for every SKU.
* 拣货完成率 — of the 70,510 picked orders, 100.00% are fully picked.
* 缺货补单比例 — ``isundersupplybackordered`` is true for all 73,595 orders.
* 信用额度使用率 — two defensible calibers (sum of running balances vs the last
  balance) disagree by 1.8x, and neither is the caliber the certified 应收余额
  attribute uses. Shipping one would create a third answer to the same question.

Two rules these SQL statements follow on purpose, and the reason the first run
of them was wrong: **filter on transaction-type *name*, never on the numeric id.**
The ids are data, not vocabulary — type 3 is "Customer Payment Received", not
"payment", and invoices are type 1, so an id-based guess inverted the collection
rate into a negative number. The second is that any date arithmetic anchors on
the data's end (2016-05-31) rather than ``CURRENT_DATE``, which is years past it.

::

    python scripts/custom_analyses.py --dry-run
    python scripts/custom_analyses.py
    python scripts/custom_analyses.py --update     # rewrite in place
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from api_client import Api, ApiError, DEFAULT_API  # noqa: E402

#: The last day with data in Wide World Importers. Every aging window here is
#: measured against it, not against today — see ``time_anchor.py`` for why.
DATA_END = "2016-05-31"


@dataclass(frozen=True)
class Pattern:
    """One query shape, with the reasoning a future reader needs."""

    name: str
    description: str
    sql: str


PATTERNS: tuple[Pattern, ...] = (
    Pattern(
        name="应收账龄分桶（30/60/90 天）",
        description=(
            "把未结应收按账龄分桶：每张未结发票的账龄 = 数据末端日期 − 最老未结发票日，"
            "用 CASE 分成 0-30 / 31-60 / 61-90 / 90 天以上，再统计客户数与金额。"
            "示范两件事：账龄必须相对数据末端而不是 CURRENT_DATE（本库数据止于 "
            f"{DATA_END}，用 CURRENT_DATE 会把全部余额算进“90 天以上”）；"
            "余额取 customertransactions.outstandingbalance 的客户级汇总，与已认证的"
            "“应收余额”指标口径一致。"
        ),
        sql=f"""WITH ar AS (
  SELECT c.customerid,
         SUM(ct.outstandingbalance) AS open_balance,
         MIN(i.invoicedate) AS oldest_open_invoice
  FROM sales.customertransactions ct
  JOIN sales.customers c ON c.customerid = ct.customerid
  LEFT JOIN sales.invoices i ON i.invoiceid = ct.invoiceid
  WHERE ct.outstandingbalance > 0
  GROUP BY c.customerid
)
SELECT CASE
         WHEN (DATE '{DATA_END}' - oldest_open_invoice) <= 30 THEN '0-30天'
         WHEN (DATE '{DATA_END}' - oldest_open_invoice) <= 60 THEN '31-60天'
         WHEN (DATE '{DATA_END}' - oldest_open_invoice) <= 90 THEN '61-90天'
         ELSE '90天以上'
       END AS aging_bucket,
       COUNT(*) AS customer_count,
       SUM(open_balance) AS open_balance
FROM ar
GROUP BY 1
ORDER BY MIN(DATE '{DATA_END}' - oldest_open_invoice)""",
    ),
    Pattern(
        name="回款率（开票 vs 收款）",
        description=(
            "回款率 = 收款合计 / 开票合计，两个数都从 sales.customertransactions 这一张"
            "带符号台账里拆出来：按 application.transactiontypes.transactiontypename 区分"
            "“Customer Invoice”（正数）和“Customer Payment Received”（负数，取反），"
            "再相除并加 NULLIF 防除零。"
            "关键点：必须按交易类型**名称**过滤，不能按 transactiontypeid 猜——本库开票是 "
            "id 1、收款是 id 3，按 id 猜会把结果算成负数。"
        ),
        sql="""SELECT
  SUM(CASE WHEN tt.transactiontypename = 'Customer Invoice'
           THEN ct.transactionamount ELSE 0 END) AS invoiced_amount,
  SUM(CASE WHEN tt.transactiontypename = 'Customer Payment Received'
           THEN -ct.transactionamount ELSE 0 END) AS collected_amount,
  SUM(CASE WHEN tt.transactiontypename = 'Customer Payment Received'
           THEN -ct.transactionamount ELSE 0 END)
    / NULLIF(SUM(CASE WHEN tt.transactiontypename = 'Customer Invoice'
                      THEN ct.transactionamount ELSE 0 END), 0) AS collection_rate
FROM sales.customertransactions ct
JOIN application.transactiontypes tt ON tt.transactiontypeid = ct.transactiontypeid""",
    ),
    Pattern(
        name="客户应收对账（已开票 - 已收款）",
        description=(
            "逐客户对账：同一张台账按客户分组，同时算出已开票、已收款和未结余额，"
            "只保留仍有余额的客户。示范“一张带符号流水表按类型拆成多条腿再按实体汇总”的写法，"
            "以及 HAVING 过滤汇总结果而不是 WHERE 过滤明细。"
        ),
        sql="""SELECT c.customerid, c.customername,
       SUM(CASE WHEN tt.transactiontypename = 'Customer Invoice'
                THEN ct.transactionamount ELSE 0 END) AS invoiced_amount,
       SUM(CASE WHEN tt.transactiontypename = 'Customer Payment Received'
                THEN -ct.transactionamount ELSE 0 END) AS collected_amount,
       SUM(ct.outstandingbalance) AS open_balance
FROM sales.customertransactions ct
JOIN sales.customers c ON c.customerid = ct.customerid
JOIN application.transactiontypes tt ON tt.transactiontypeid = ct.transactiontypeid
GROUP BY c.customerid, c.customername
HAVING SUM(ct.outstandingbalance) > 0
ORDER BY open_balance DESC""",
    ),
    Pattern(
        name="订单到发票转化率",
        description=(
            "转化率 = 已开票订单数 / 订单总数。用 LEFT JOIN 子查询找出没有发票的订单，"
            "以 COUNT(i.orderid) 只数匹配上的行。"
            "不要用 NOT IN 或内连接：内连接会直接丢掉未开票的订单，分母跟着变小，"
            "转化率永远是 100%。"
        ),
        sql="""SELECT
  COUNT(*) AS order_count,
  COUNT(i.orderid) AS invoiced_order_count,
  COUNT(i.orderid)::numeric / NULLIF(COUNT(*), 0) AS conversion_rate
FROM sales.orders o
LEFT JOIN (SELECT DISTINCT orderid FROM sales.invoices) i ON i.orderid = o.orderid""",
    ),
    Pattern(
        name="负毛利商品（亏损 SKU）",
        description=(
            "按商品汇总毛利额，用 HAVING 只留下总毛利为负的商品，按亏损额升序排列。"
            "示范两件事：亏钱卖的判定要在**商品级汇总**上做，单行 lineprofit < 0 会把"
            "偶发的单笔折扣也算成“亏损商品”；以及用 HAVING 过滤聚合结果而不是 WHERE。"
        ),
        sql="""SELECT s.stockitemid, s.stockitemname,
       SUM(il.lineprofit) AS total_profit,
       SUM(il.extendedprice) AS revenue,
       COUNT(*) AS line_count
FROM sales.invoicelines il
JOIN warehouse.stockitems s ON s.stockitemid = il.stockitemid
GROUP BY s.stockitemid, s.stockitemname
HAVING SUM(il.lineprofit) < 0
ORDER BY total_profit ASC""",
    ),
    Pattern(
        name="盘点差异（Stock Adjustment）",
        description=(
            "盘点调整汇总：从库存流水里筛出交易类型名称为 'Stock Adjustment at Stocktake' "
            "的记录，统计笔数、净数量与净金额。"
            "关键点同样是按交易类型**名称**过滤而不是 id（本库是 12，但 id 是数据不是词汇）；"
            "quantity 正为入、负为出，所以“净数量”直接求和即可，不要用 ABS。"
        ),
        sql="""SELECT tt.transactiontypename,
       COUNT(*) AS adjustment_count,
       SUM(st.quantity) AS net_quantity,
       SUM(st.quantity * s.unitprice) AS net_value
FROM warehouse.stockitemtransactions st
JOIN application.transactiontypes tt ON tt.transactiontypeid = st.transactiontypeid
JOIN warehouse.stockitems s ON s.stockitemid = st.stockitemid
WHERE tt.transactiontypename = 'Stock Adjustment at Stocktake'
GROUP BY tt.transactiontypename""",
    ),
)


#: The list endpoints cap ``limit`` at 100. Six patterns are seeded here, but a
#: deployment may hold many more, so paging is not optional.
_PAGE_SIZE = 100


def _existing(api: Api) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    skip = 0
    while True:
        listing = api.get(f"/api/custom-analyses?skip={skip}&limit={_PAGE_SIZE}")
        rows = listing.get("data") or []
        for row in rows:
            found[row["name"]] = row
        if len(rows) < _PAGE_SIZE:
            return found
        skip += _PAGE_SIZE


def apply(api: Api, *, dry_run: bool, update: bool) -> int:
    existing = _existing(api)
    planned = 0

    for pattern in PATTERNS:
        current = existing.get(pattern.name)
        body = {
            "name": pattern.name,
            "description": pattern.description,
            "sql": pattern.sql,
        }

        if current is None:
            planned += 1
            print(f"{'would create' if dry_run else 'create'} {pattern.name!r}")
        elif update:
            planned += 1
            print(f"{'would update' if dry_run else 'update'} {pattern.name!r}")
        else:
            print(f"keep  {pattern.name!r} (already exists)")
            continue

        if dry_run:
            continue

        # Validate first: the API refuses SQL that resolves to no catalogued
        # table, and an analysis the catalog cannot parse is invisible to
        # retrieval — it would be created and never used.
        api.post("/api/custom-analyses/validate", {"sql": pattern.sql})
        if current is None:
            api.post("/api/custom-analyses", body)
        else:
            api.put(f"/api/custom-analyses/{current['id']}", body)

    if planned and not dry_run:
        # Creating an analysis already embeds it; this is belt-and-braces so a
        # pattern cannot end up in the catalog and invisible to retrieval. It is
        # best-effort: an older deployment predates the reindex endpoint, and
        # failing the whole seed over a defensive extra step would be wrong.
        print("\nreindexing the semantic layer so the new patterns are retrievable…")
        try:
            report = api.post("/api/semantic-index/reindex", {})
        except ApiError as exc:
            print(
                f"  skipped (this build has no reindex endpoint: {exc}). "
                "Each analysis was embedded on create, so retrieval should "
                "already see them; verify with a search."
            )
        else:
            print(
                "  reindex:",
                {k: report.get(k) for k in ("dry_run", "total", "written", "skipped")},
            )
            if report.get("skipped"):
                print(f"  NOT reindexed: {report.get('reason')}")

    print(f"\n{planned} change(s) {'planned' if dry_run else 'applied'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api", default=DEFAULT_API, help="backend base URL (default: %(default)s)"
    )
    parser.add_argument("--dry-run", action="store_true", help="report, change nothing")
    parser.add_argument(
        "--update",
        action="store_true",
        help="rewrite existing patterns in place instead of skipping them",
    )
    args = parser.parse_args()

    try:
        return apply(Api(args.api), dry_run=args.dry_run, update=args.update)
    except ApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
