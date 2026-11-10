"""Build a point-in-time copy of ``retail_churn`` that ends at a given anchor.

Why this exists
---------------
The application's Kumo path has no "as of" argument. PQL spells a target window
as an offset from the *end of the data* — ``COUNT(orders.*, 0, 180, days)`` means
"the 180 days after the newest order in the graph", not "after 2024-06-30". So a
question that says "以 2024-06-30 为观察点" cannot be honoured by the query; the
model can only forecast forward from whatever the newest row happens to be.

The honest way to ask a point-in-time question is to hand the system a database
whose newest row *is* that point in time. This script builds one: a copy of
``retail_churn`` truncated at ``--anchor``, carrying exactly what a retention
team would have known on that date, with ``scoring`` (the answer key) removed.

How it is built
---------------
``CREATE DATABASE ... TEMPLATE retail_churn`` copies everything — indexes,
constraints, comments, views — in one statement, and the views keep working
because they are views: once the rows behind them are gone, they recompute from
what is left. Deleting the future rows is then a handful of ``DELETE``s ordered
children-first so no foreign key is ever violated.

What is cut:

* Event tables lose every row dated after the anchor.
* ``support_tickets.closed_at`` is blanked when it falls after the anchor. A
  ticket still open on that date was open; keeping the future close date leaks
  the answer through a column nobody thinks of as a timestamp.
* ``customers`` loses accounts that had not signed up yet; ``products`` loses
  products not yet launched.
* ``scoring`` is dropped outright.

``CREATE DATABASE ... TEMPLATE`` requires no other session on the template, so
the script terminates connections to ``retail_churn`` first. The application
reconnects on its own.

::

    python scripts/retail_churn_asof.py --dry-run
    python scripts/retail_churn_asof.py --anchor 2024-06-30
    python scripts/retail_churn_asof.py --anchor 2024-06-30 --force   # rebuild
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))

from retail_churn_score import _load_env  # noqa: E402

SOURCE = "retail_churn"
DEFAULT_ANCHOR = "2024-06-30"

#: ``(table, predicate)`` — ``{a}`` becomes a quoted anchor date literal.
#: Deletion order is children-first; ``order_lines`` has no date column of its
#: own, so it goes with its orders.
DELETIONS: list[tuple[str, str | None]] = [
    (
        "order_lines",
        "order_id IN (SELECT order_id FROM retail.orders WHERE order_date > {a})",
    ),
    # Both directions: a payment dated after the anchor would leak, and a payment
    # whose invoice is about to be deleted would break the foreign key.
    (
        "payments",
        "paid_date > {a} OR invoice_id IN "
        "(SELECT invoice_id FROM retail.invoices WHERE invoice_date > {a})",
    ),
    ("invoices", "invoice_date > {a}"),
    ("orders", "order_date > {a}"),
    ("support_tickets", "opened_at > {a}"),
    ("web_sessions", "session_date > {a}"),
    ("customers", "signup_date > {a}"),
    ("products", "launched_on > {a}"),
]

#: ``(table, column)`` pairs blanked when they point past the anchor.
FUTURE_COLUMNS = [("support_tickets", "closed_at")]

DROPPED_SCHEMAS = ["scoring"]


def dsn(database: str) -> str:
    _load_env()
    return (
        f"host={os.environ.get('POSTGRES_HOST', 'localhost')} "
        f"port={os.environ.get('POSTGRES_PORT', '5432')} "
        f"user={os.environ.get('POSTGRES_USER', 'postgres')} "
        f"password={os.environ.get('POSTGRES_PASSWORD', '')} "
        f"dbname={database}"
    )


def where_sql(predicate: str, anchor: str) -> str:
    return " WHERE " + predicate.format(a=f"DATE '{anchor}'")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--anchor", default=DEFAULT_ANCHOR)
    parser.add_argument(
        "--database",
        default=None,
        help="target database; defaults to retail_churn_asof_<anchor, no dashes>",
    )
    parser.add_argument("--force", action="store_true", help="drop the target first")
    parser.add_argument("--dry-run", action="store_true", help="print the plan only")
    args = parser.parse_args()

    anchor = args.anchor
    database = args.database or f"retail_churn_asof_{anchor.replace('-', '')}"

    print(f"source  {SOURCE}  (copied whole, then trimmed)")
    print(f"target  {database}")
    print(f"anchor  {anchor}\n")
    for table, predicate in DELETIONS:
        print(f"  delete  retail.{table:22s} {predicate.format(a=anchor)}")
    for table, column in FUTURE_COLUMNS:
        print(f"  blank   retail.{table}.{column:16s} > {anchor}")
    for schema in DROPPED_SCHEMAS:
        print(f"  drop    {schema}.*                (answer key)")
    print("  keep    retail.v_customer_*        views recompute from the survivor rows\n")

    if args.dry_run:
        print("dry run — nothing written")
        return 0

    with psycopg.connect(dsn("postgres"), autocommit=True) as admin:
        exists = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (database,)
        ).fetchone()
        if exists and not args.force:
            print(f"{database} already exists; pass --force to rebuild")
            return 1
        if exists:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database,),
            )
            admin.execute(f'DROP DATABASE "{database}"')
            print(f"dropped {database}")
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (SOURCE,),
        )
        admin.execute(f'CREATE DATABASE "{database}" TEMPLATE "{SOURCE}"')
        print(f"created {database} from template {SOURCE}\n")

    with psycopg.connect(dsn(database), autocommit=True) as conn:
        for schema in DROPPED_SCHEMAS:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        for table, predicate in DELETIONS:
            removed = conn.execute(
                f"DELETE FROM retail.{table}{where_sql(predicate, anchor)}"
            ).rowcount
            left = conn.execute(f"SELECT count(*) FROM retail.{table}").fetchone()[0]
            print(f"  {table:22s} removed {removed:>6d}   kept {left:>7d}")
        for table, column in FUTURE_COLUMNS:
            blanked = conn.execute(
                f"UPDATE retail.{table} SET {column} = NULL "
                f"WHERE {column} > DATE '{anchor}'"
            ).rowcount
            print(f"  blanked {blanked} future value(s) in {table}.{column}")

    # A copy that silently keeps the future is worse than no copy. Check the two
    # things that would make the answer either leak or be unscoreable.
    with psycopg.connect(dsn(database), autocommit=True) as conn:
        print("\nverification:")
        leaks = 0
        checks = [
            ("orders", "max(order_date)", anchor),
            ("invoices", "max(invoice_date)", anchor),
            ("payments", "max(paid_date)", anchor),
            ("support_tickets", "max(opened_at)", anchor),
            ("support_tickets", "max(closed_at)", anchor),
            ("web_sessions", "max(session_date)", anchor),
            ("customers", "max(signup_date)", anchor),
            ("products", "max(launched_on)", anchor),
        ]
        for table, expression, limit in checks:
            value = conn.execute(f"SELECT {expression} FROM retail.{table}").fetchone()[0]
            ok = value is None or str(value) <= limit
            if not ok:
                leaks += 1
            print(f"  {table:18s} {expression:20s} {value}  {'ok' if ok else 'AFTER ANCHOR'}")
        schemas = [
            row[0]
            for row in conn.execute(
                "SELECT schema_name FROM information_schema.schemata ORDER BY 1"
            )
        ]
        print(f"  schemas            {schemas}")
        last = conn.execute(
            "SELECT max(last_order_date) FROM retail.v_customer_last_order"
        ).fetchone()[0]
        print(f"  view last_order    {last}  {'ok' if str(last) <= anchor else 'AFTER ANCHOR'}")
        if leaks or "scoring" in schemas:
            print("\ncopy still contains future data or the answer key")
            return 1
    print("\ncopy ends at the anchor and carries no answer key")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
