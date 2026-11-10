"""Score a churn shortlist produced by the application against the truth table.

The application answers §"哪些客户最可能流失" with a list of customer ids. This
turns that list into the only number that matters — how many of them actually
stopped ordering in the following 180 days — and compares it with the base rate
among the customers a retention team would have been shown.

Three things it checks that the prose answer cannot show:

* **Risk set.** A forecast may only name customers who were still buying at the
  anchor. Naming someone who left two years ago is not a prediction, and a
  staleness-only ranking does exactly that: at the end of this dataset the 20
  most stale accounts are all long gone.
* **Anchor discipline.** Truth is read from ``scoring.churn_events`` (never
  exposed to the model) and only churn dates inside the 180-day window count.
* **Base rate.** Without it a precision of 0.15 looks either great or terrible
  depending on whether the base rate is 0.03 or 0.12.

::

    python scripts/check_churn_answer.py --anchor 2024-06-30 87 110 100 ...
    python scripts/check_churn_answer.py --anchor 2024-06-30 --csv /tmp/ids.csv

    # straight from an ask_app.py transcript: take the top 20 by predicted
    # probability and read the anchor the prediction actually used
    python scripts/check_churn_answer.py --from-transcript /tmp/q.txt
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))

from retail_churn_score import HORIZON_DAYS, _dsn, _load_env  # noqa: E402


def _rows_from_transcript(path: Path) -> list[dict]:
    """The ``ROWS:`` block an ``ask_app.py`` transcript ends with.

    The prose answer is not where the ranking lives — the formatter summarises
    ("scored 87 entities") and leaves the scored entities in this array. Reading
    it here keeps the number the scorer reports tied to what the predictor
    actually returned instead of to a table copied out by hand.
    """
    text = path.read_text(encoding="utf8")
    marker = text.rfind("ROWS:")
    if marker < 0:
        raise SystemExit(f"no ROWS block in {path}")
    blob = text[marker + len("ROWS:") :].strip()
    if blob == "None":
        raise SystemExit("the transcript has no rows (the prediction failed)")
    try:
        # ``raw_decode`` stops at the end of the JSON value, so the separator
        # line and the elapsed-time footer after it are ignored rather than
        # parsed as trailing garbage.
        rows, _ = json.JSONDecoder().raw_decode(blob)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"could not parse the ROWS block: {exc}") from exc
    if not isinstance(rows, list):
        raise SystemExit("ROWS is not a list")
    return rows


def _top_entities(rows: list[dict], top: int, column: str) -> tuple[list[int], str | None]:
    """Highest-risk entities first, plus the anchor the prediction used."""
    ranked = sorted(rows, key=lambda row: row.get(column, 0.0), reverse=True)[:top]
    ids = [int(row["ENTITY"]) for row in ranked]
    anchor = next((str(row.get("anchor_timestamp", ""))[:10] for row in rows), None)
    return ids, anchor if anchor and re.match(r"\d{4}-\d{2}-\d{2}", anchor) else None


def load(customer_ids: list[int], anchor: str) -> pd.DataFrame:
    _load_env()
    window_end = pd.Timestamp(anchor) + pd.Timedelta(days=HORIZON_DAYS)
    with psycopg.connect(_dsn()) as conn:
        truth = pd.read_sql("SELECT * FROM scoring.churn_events", conn)
        customers = pd.read_sql("SELECT * FROM retail.customers", conn)
        orders = pd.read_sql(
            "SELECT customer_id, order_date, status FROM retail.orders", conn
        )
    truth["churn_date"] = pd.to_datetime(truth["churn_date"])
    orders["order_date"] = pd.to_datetime(orders["order_date"])
    done = orders[orders["status"] == "completed"]

    last_before = (
        done[done["order_date"] <= pd.Timestamp(anchor)]
        .groupby("customer_id")["order_date"]
        .max()
    )
    frame = pd.DataFrame({"customer_id": customer_ids})
    frame["picked_rank"] = range(1, len(frame) + 1)
    frame = frame.merge(
        customers[["customer_id", "company_name", "segment", "contract_type"]],
        on="customer_id",
        how="left",
    )
    frame = frame.merge(truth[["customer_id", "churn_date", "churn_cause"]], on="customer_id", how="left")
    frame["last_order_before"] = frame["customer_id"].map(last_before)
    frame["days_since_last_order"] = (
        pd.Timestamp(anchor) - frame["last_order_before"]
    ).dt.days
    frame["was_alive_at_anchor"] = frame["churn_date"].isna() | (
        frame["churn_date"] > pd.Timestamp(anchor)
    )
    # A positive has to fall *after* the anchor. Leaving the lower bound off
    # scores a list of customers who left two years earlier as 20/20 — they did
    # churn, just not in the window being forecast.
    frame["churned_in_window"] = (
        frame["churn_date"].notna()
        & (frame["churn_date"] > pd.Timestamp(anchor))
        & (frame["churn_date"] <= window_end)
    )
    frame["usable"] = frame["was_alive_at_anchor"] & frame["last_order_before"].notna()
    frame["window_end"] = window_end.date().isoformat()
    return frame


def base_rate(anchor: str) -> tuple[int, int]:
    """Positives and risk-set size for the same anchor, from the operational rows."""
    _load_env()
    window_end = pd.Timestamp(anchor) + pd.Timedelta(days=HORIZON_DAYS)
    with psycopg.connect(_dsn()) as conn:
        truth = pd.read_sql("SELECT * FROM scoring.churn_events", conn)
        orders = pd.read_sql(
            "SELECT customer_id, order_date, status FROM retail.orders", conn
        )
    truth["churn_date"] = pd.to_datetime(truth["churn_date"])
    orders["order_date"] = pd.to_datetime(orders["order_date"])
    done = orders[orders["status"] == "completed"]
    last_before = (
        done[done["order_date"] <= pd.Timestamp(anchor)]
        .groupby("customer_id")["order_date"]
        .max()
    )
    alive = truth[
        truth["churn_date"].isna() | (truth["churn_date"] > pd.Timestamp(anchor))
    ]["customer_id"]
    at_risk = [cid for cid in alive if cid in last_before.index]
    positives = truth[
        truth["customer_id"].isin(at_risk)
        & truth["churn_date"].notna()
        & (truth["churn_date"] <= window_end)
    ]
    return len(positives), len(at_risk)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--anchor", default=None, help="as-of date, YYYY-MM-DD")
    parser.add_argument("customer_ids", nargs="*", type=int)
    parser.add_argument("--csv", type=Path, default=None, help="file with one id per line")
    parser.add_argument(
        "--from-transcript",
        type=Path,
        default=None,
        help="an ask_app.py transcript; takes the top --top entities by probability",
    )
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument(
        "--probability",
        default="True_PROB",
        help="which score column to rank by (True_PROB, or False_PROB for reversed)",
    )
    args = parser.parse_args()

    ids = list(args.customer_ids)
    if args.csv:
        ids += [int(v) for v in args.csv.read_text().split() if v.strip().isdigit()]
    anchor = args.anchor

    if args.from_transcript:
        rows = _rows_from_transcript(args.from_transcript)
        picked, used_anchor = _top_entities(rows, args.top, args.probability)
        ids = picked + ids
        # The predictor picks its own window start — it is not necessarily the
        # anchor the question asked for. Scoring against the anchor in the data
        # would then measure the wrong 180 days, so prefer what it reported.
        if used_anchor and not anchor:
            anchor = used_anchor
            print(f"anchor taken from the transcript: {anchor}\n")
        elif used_anchor and anchor != used_anchor:
            print(f"note: transcript used {used_anchor}, scoring against {anchor}\n")

    if not ids:
        parser.error("no customer ids given")
    if not anchor:
        parser.error("--anchor is required unless the transcript carries one")

    frame = load(ids, anchor)
    positives, at_risk = base_rate(anchor)
    rate = positives / at_risk if at_risk else float("nan")

    print(f"anchor {anchor}  window ends {frame['window_end'].iloc[0]}")
    print(f"base rate: {positives}/{at_risk} = {rate:.1%}\n")
    print(
        frame[
            [
                "picked_rank",
                "customer_id",
                "company_name",
                "segment",
                "contract_type",
                "days_since_last_order",
                "was_alive_at_anchor",
                "churned_in_window",
                "churn_cause",
            ]
        ].to_string(index=False)
    )

    hits = int(frame["churned_in_window"].sum())
    usable = int(frame["usable"].sum())
    already_gone = int((~frame["was_alive_at_anchor"]).sum())
    never_bought = int(frame["last_order_before"].isna().sum())

    print(f"\npicked            {len(frame)}")
    print(f"already gone      {already_gone}   (not a prediction)")
    print(f"never ordered     {never_bought}")
    print(f"usable            {usable}")
    print(f"churned in window {hits}   precision {hits / len(frame):.3f}")
    if usable:
        print(f"precision on usable picks {hits / usable:.3f}")
    print(f"lift over base rate {hits / len(frame) / rate:.2f}x" if rate else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
