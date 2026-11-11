#!/usr/bin/env python
# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Score the hosted predictor against the ``retail_churn`` library.

What makes this a real test
---------------------------

At each anchor the model is handed **only the rows that existed by that date**.
The label is computed afterwards from the full history. So the AUC measures a
forecast, not a recollection: nothing the model sees contains the answer.

Customers who had already left before the anchor are excluded — predicting the
departure of someone who is gone is not a prediction. Accounts that never placed
a first order are excluded too: they are leads, not churn, and they look exactly
like the riskiest customers on earth. What is left is the risk set a retention
team would actually score.

Anchors must sit at least a full horizon before the end of the data, otherwise
no churn in that window can have been observed yet and the anchor scores nothing.

Usage
-----

::

    python scripts/retail_churn_score.py
    python scripts/retail_churn_score.py --anchors 2024-03-31 2024-06-30
    python scripts/retail_churn_score.py --dry-run          # payload only
    python scripts/retail_churn_score.py --history-anchors 2
    python scripts/retail_churn_score.py --save-scores /tmp/scores

Requires ``KUMO_HOSTED_API_URL`` / ``KUMO_HOSTED_API_KEY`` (read from ``.env``).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from auto_ontology.retrieval.kumo.framed_graph import build_framed_graph
from auto_ontology.retrieval.kumo.hosted import HostedConfig, HostedKumoModel

DATABASE = "retail_churn"
HORIZON_DAYS = 180

PQL = "PREDICT COUNT(orders.*, 0, {h}, days) = 0 FOR EACH customers.customer_id"

# Declared rather than inferred: in an evaluation the schema should be given,
# not guessed, so a bad AUC means the model missed rather than the graph did.
LINKS = [
    ("orders", "customer_id", "customers"),
    ("order_lines", "order_id", "orders"),
    ("order_lines", "product_id", "products"),
    ("invoices", "order_id", "orders"),
    ("invoices", "customer_id", "customers"),
    ("payments", "invoice_id", "invoices"),
    ("support_tickets", "customer_id", "customers"),
    ("web_sessions", "customer_id", "customers"),
    ("customers", "region_id", "regions"),
    ("orders", "region_id", "regions"),
]

KEYS = {
    "regions": ["region_id"],
    "customers": ["customer_id"],
    "products": ["product_id"],
    "orders": ["order_id"],
    "order_lines": ["line_id"],
    "invoices": ["invoice_id"],
    "payments": ["payment_id"],
    "support_tickets": ["ticket_id"],
    "web_sessions": ["session_id"],
}

DATE_COLUMNS = {
    "customers": ["signup_date"],
    "products": ["launched_on", "discontinued_on"],
    "orders": ["order_date"],
    "invoices": ["invoice_date", "due_date"],
    "payments": ["paid_date"],
    "support_tickets": ["opened_at", "closed_at"],
    "web_sessions": ["session_date"],
}

# Each table is read with its own cut-off so nothing after the anchor leaks in.
CUTOFF_FILTERS = {
    "regions": None,
    "products": None,
    "customers": ("signup_date", "<="),
    "orders": ("order_date", "<="),
    "order_lines": ("__order_date", "<="),
    "invoices": ("invoice_date", "<="),
    "payments": ("paid_date", "<="),
    "support_tickets": ("opened_at", "<="),
    "web_sessions": ("session_date", "<="),
}

QUERIES = {
    "regions": "SELECT * FROM retail.regions",
    "customers": "SELECT * FROM retail.customers",
    "products": "SELECT * FROM retail.products",
    "orders": "SELECT * FROM retail.orders",
    "order_lines": """
        SELECT l.*, o.order_date AS __order_date
        FROM retail.order_lines l JOIN retail.orders o USING (order_id)
    """,
    "invoices": "SELECT * FROM retail.invoices",
    "payments": "SELECT * FROM retail.payments",
    "support_tickets": "SELECT * FROM retail.support_tickets",
    "web_sessions": "SELECT * FROM retail.web_sessions",
}


def _load_env(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith(("POSTGRES_", "KUMO_")) and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def _dsn() -> str:
    return (
        f"host={os.environ.get('POSTGRES_HOST', 'localhost')} "
        f"port={os.environ.get('POSTGRES_PORT', '5432')} "
        f"user={os.environ.get('POSTGRES_USER', 'postgres')} "
        f"password={os.environ.get('POSTGRES_PASSWORD', '')} "
        f"dbname={DATABASE}"
    )


def _read(conn, sql: str) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute(sql)
        columns = [description[0] for description in cur.description]
        rows = cur.fetchall()
    frame = pd.DataFrame(rows, columns=columns)
    # Postgres `numeric` arrives as Decimal, which pandas stores as object. Left
    # alone, the schema step reads a money column as a 30k-value categorical and
    # the endpoint rejects the whole request.
    for column in frame.columns:
        if frame[column].dtype != object:
            continue
        sample = frame[column].dropna()
        if len(sample) and isinstance(sample.iloc[0], Decimal):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def load_frames(cutoff: pd.Timestamp | None) -> dict[str, pd.DataFrame]:
    """Everything the model is allowed to see, as of *cutoff*."""
    import psycopg

    conn = psycopg.connect(_dsn())
    try:
        frames: dict[str, pd.DataFrame] = {}
        for name, sql in QUERIES.items():
            frame = _read(conn, sql)
            for column in list(DATE_COLUMNS.get(name, [])) + ["__order_date"]:
                if column in frame:
                    frame[column] = pd.to_datetime(frame[column])
            rule = CUTOFF_FILTERS.get(name)
            if rule is not None and cutoff is not None:
                column, _op = rule
                if column in frame:
                    frame = frame[frame[column] <= cutoff]
            # The join date only exists to apply the cut-off; left in place it
            # travels to the endpoint as an unnamed high-cardinality column.
            frame = frame.drop(columns=["__order_date"], errors="ignore")
            frames[name] = frame.reset_index(drop=True)

        truth = _read(conn, "SELECT * FROM scoring.churn_events")
        truth["churn_date"] = pd.to_datetime(truth["churn_date"])
    finally:
        conn.close()

    return frames, truth


def build_model(frames: dict[str, pd.DataFrame], **overrides) -> HostedKumoModel:
    graph = build_framed_graph(frames, KEYS)
    for src, column, dst in LINKS:
        if src in frames and dst in frames and column in frames[src].columns:
            graph.link(src, column, dst)
    # An account's signup date is not an event: anchoring on it would place
    # instances at registration rather than at the end of the observed window.
    for name in ("customers", "products"):
        if graph.tables.get(name) and graph.tables[name].time_column:
            graph.tables[name].time_column = None

    config = HostedConfig.from_env()
    if config is None:
        raise SystemExit(
            "no hosted endpoint configured: set KUMO_HOSTED_API_URL "
            "(and KUMO_HOSTED_API_KEY) in .env or the environment"
        )
    config = dataclasses.replace(config, **overrides)
    return HostedKumoModel(graph, frames, config)


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------


def auc(scores: np.ndarray, labels: np.ndarray) -> float:
    labels = labels.astype(bool)
    positives = int(labels.sum())
    negatives = len(labels) - positives
    if positives == 0 or negatives == 0:
        return float("nan")
    order = np.argsort(scores, kind="mergesort")
    sorted_scores = scores[order]
    ranks = np.empty(len(scores), dtype="float64")
    ranks[order] = np.arange(1, len(scores) + 1)
    # Ties share their average rank; a constant score must land on 0.5, not 1.
    start = 0
    for index in range(1, len(sorted_scores) + 1):
        if index == len(sorted_scores) or sorted_scores[index] != sorted_scores[start]:
            if index - start > 1:
                ranks[order[start:index]] = ranks[order[start:index]].mean()
            start = index
    return float(
        (ranks[labels].sum() - positives * (positives + 1) / 2) / (positives * negatives)
    )


def average_precision(scores: np.ndarray, labels: np.ndarray) -> float:
    labels = labels.astype(bool)
    if not labels.any():
        return float("nan")
    order = np.argsort(-scores, kind="mergesort")
    hits = labels[order]
    cumulative = np.cumsum(hits)
    precision = cumulative / np.arange(1, len(hits) + 1)
    return float((precision * hits).sum() / hits.sum())


def precision_at(
    scores: np.ndarray, labels: np.ndarray, share: float
) -> tuple[float, float]:
    """Precision over the customers a retention team would actually work.

    The endpoint returns quantised probabilities, so a hard "top 10%" cut slices
    through a block of identical scores and the metric ends up measuring the
    sort order *inside* that block rather than the model. On this dataset that
    made a 0.64-AUC run look worse than the base rate. Taking everyone at or
    above the k-th score keeps the ranking honest — the second value returns how
    much of the book that turns out to be, so the number can be read for what it
    is instead of being over-interpreted.
    """
    labels = labels.astype(bool)
    if not labels.any():
        return float("nan"), float("nan")
    k = max(1, int(round(len(scores) * share)))
    cutoff = np.sort(scores)[::-1][k - 1]
    chosen = scores >= cutoff
    return float(labels[chosen].mean()), float(chosen.mean())


def hand_baseline(
    frames: dict[str, pd.DataFrame],
    truth: pd.DataFrame,
    anchor: pd.Timestamp,
    customers: np.ndarray,
    labels: np.ndarray,
) -> float | None:
    """A hand-built linear score on the same risk set.

    Six columns, all computable at the anchor from the operational tables:
    recency, order counts in the last two quarters, their ratio, ticket volume,
    mean CSAT, and whether there is any order history at all. It is the
    benchmark a competent analyst would build in an afternoon, and it is the
    number the relationship model has to beat to justify itself. If this scores
    well and the model does not, the signal is in the data and the problem is in
    the materialisation, not in the library.
    """
    done = frames["orders"][frames["orders"]["status"] == "completed"]
    tickets = frames["support_tickets"]
    index = pd.Index(customers, name="customer_id")

    last = done.groupby("customer_id")["order_date"].max()
    recent = done[done["order_date"] > anchor - pd.Timedelta(days=90)]
    prior = done[
        (done["order_date"] <= anchor - pd.Timedelta(days=90))
        & (done["order_date"] > anchor - pd.Timedelta(days=180))
    ]
    recent_tickets = tickets[tickets["opened_at"] > anchor - pd.Timedelta(days=90)]

    features = pd.DataFrame(
        {
            "recency": (anchor - last.reindex(index)).dt.days,
            "orders_90d": recent.groupby("customer_id").size().reindex(index).fillna(0),
            "orders_prev_90d": prior.groupby("customer_id").size().reindex(index).fillna(0),
            "tickets_90d": recent_tickets.groupby("customer_id").size().reindex(index).fillna(0),
            "csat_90d": recent_tickets.groupby("customer_id")["csat"].mean().reindex(index),
        },
        index=index,
    )
    features["order_drop"] = features["orders_90d"] / (features["orders_prev_90d"] + 1.0)
    features["csat_90d"] = features["csat_90d"].fillna(features["csat_90d"].median())
    # Customers with no order on record at all must stay in the comparison —
    # they are the riskiest ones, and dropping them would hand the baseline a
    # cleaner population than the model is scored on.
    features["never_ordered"] = features["recency"].isna().astype(float)
    features["recency"] = features["recency"].fillna(HORIZON_DAYS * 4.0)
    if not len(features):
        return None

    usable = pd.Index(customers).get_indexer(features.index)
    target = labels[usable].astype(float)

    design = (features - features.mean()) / features.std().replace(0, 1.0)
    design.insert(0, "intercept", 1.0)
    matrix = design.to_numpy()
    # Ridge-regularised least squares: a tiny problem, no need for a solver.
    beta = np.linalg.solve(
        matrix.T @ matrix + 1e-3 * np.eye(matrix.shape[1]), matrix.T @ target
    )
    return round(auc(matrix @ beta, target.astype(bool)), 4)


def evaluate(
    frame: pd.DataFrame,
    truth: pd.DataFrame,
    anchor: pd.Timestamp,
    frames: dict[str, pd.DataFrame],
) -> tuple[dict, np.ndarray, np.ndarray, pd.DataFrame]:
    """Join predictions to the answer key and score the risk set."""
    prediction = frame.rename(columns={"ENTITY": "customer_id"})
    prediction["customer_id"] = prediction["customer_id"].astype(int)
    merged = prediction.merge(truth, on="customer_id", how="left")
    # Already gone before the anchor: not a forecast target.
    alive = merged[merged["churn_date"].isna() | (merged["churn_date"] > anchor)]
    # An account that never placed a first order is not a lost customer. It also
    # looks exactly like the riskiest account on earth — no orders, no payments,
    # no tickets — so leaving it in fills the top of the ranking with leads and
    # scores the model on a definitional artefact rather than on forecasting.
    leads = int(alive["never_activated"].sum())
    alive = alive[~alive["never_activated"].fillna(False)]
    left = (
        alive["churn_date"].notna()
        & (alive["churn_date"] <= anchor + pd.Timedelta(days=HORIZON_DAYS))
    )
    scores = alive["True_PROB"].to_numpy(dtype="float64")
    labels = left.to_numpy()
    p_at_10, top_share = precision_at(scores, labels, 0.10)

    report = {
        "anchor": str(anchor.date()),
        "scored": int(len(alive)),
        "leads_dropped": leads,
        "positives": int(labels.sum()),
        "positive_rate": round(float(labels.mean()), 4) if len(labels) else None,
        "auc": round(auc(scores, labels), 4) if len(labels) else None,
        "ap": round(average_precision(scores, labels), 4) if len(labels) else None,
        "p_at_10": round(p_at_10, 4) if len(labels) else None,
        "top_share": round(top_share, 4) if len(labels) else None,
        "distinct_scores": int(len(np.unique(scores))) if len(scores) else 0,
        "lift_at_10": None,
        "prob_quantiles": {
            str(q): round(float(np.quantile(scores, q)), 4)
            for q in (0.1, 0.5, 0.9)
        }
        if len(scores)
        else None,
    }
    if report["p_at_10"] and report["positive_rate"]:
        report["lift_at_10"] = round(report["p_at_10"] / report["positive_rate"], 2)

    by_cause = {}
    for cause in ("competitor", "demand_decline", "non_renewal", "bankruptcy"):
        mask = alive["churn_cause"].eq(cause).to_numpy() & labels
        if mask.sum() >= 8:
            # One-vs-rest within the scored population.
            by_cause[cause] = round(auc(scores, mask), 4)
    report["auc_by_cause"] = by_cause

    # The benchmark any retention team already has: how long since the last
    # order. If the model cannot beat this, the extra tables bought nothing.
    completed = frames["orders"][frames["orders"]["status"] == "completed"]
    last_order = completed.groupby("customer_id")["order_date"].max()
    recency = (anchor - alive["customer_id"].map(last_order)).dt.days
    known = recency.notna().to_numpy()
    if known.sum() and labels[known].any():
        report["recency_auc"] = round(
            auc(recency[known].to_numpy(dtype="float64"), labels[known]), 4
        )
    else:
        report["recency_auc"] = None

    # A customer silent for longer than the horizon answers the PQL by himself —
    # including him measures how well the model restates the past, not how well
    # it forecasts. This subset is where the question is genuinely open, so the
    # two numbers should be read together.
    silent = np.nan_to_num(recency.to_numpy(dtype="float64"), nan=1e9)
    open_mask = silent < HORIZON_DAYS
    if labels[open_mask].sum() >= 8 and (~labels[open_mask]).sum() >= 8:
        report["auc_open"] = round(auc(scores[open_mask], labels[open_mask]), 4)
        report["open_share"] = round(float(open_mask.mean()), 4)
        report["open_positive_rate"] = round(float(labels[open_mask].mean()), 4)
    else:
        report["auc_open"] = None

    report["hand_baseline_auc"] = hand_baseline(
        frames, truth, anchor, alive["customer_id"].to_numpy(), labels
    )
    detail = alive[["customer_id", "churn_date"]].copy()
    detail["score"] = scores
    detail["label"] = labels
    return report, scores, labels, detail


# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--anchors",
        nargs="*",
        default=["2023-09-30", "2023-12-31", "2024-03-31", "2024-06-30"],
        help=(
            "quarter-end dates to forecast from. Keep them at least "
            f"{HORIZON_DAYS} days before the end of the data: a churn needs that "
            "much silence to be labelled at all, so an anchor inside the final "
            "window has no positives by construction."
        ),
    )
    parser.add_argument("--history-anchors", type=int, default=4)
    parser.add_argument("--lookback-days", type=int, default=365)
    parser.add_argument("--max-instances", type=int, default=1200)
    parser.add_argument("--max-related-rows", type=int, default=300_000)
    parser.add_argument("--dry-run", action="store_true", help="build the payload, do not send")
    parser.add_argument(
        "--all-tables",
        action="store_true",
        help="send every related table instead of only the ones the PQL names",
    )
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument(
        "--save-scores",
        type=Path,
        default=None,
        help="dump customer_id / score / label per anchor, for inspection",
    )
    args = parser.parse_args()

    _load_env()
    pql = PQL.format(h=HORIZON_DAYS)
    print(f"pql: {pql}\n")

    results = []
    pooled_scores: list[np.ndarray] = []
    pooled_labels: list[np.ndarray] = []
    for anchor_text in args.anchors:
        anchor = pd.Timestamp(anchor_text)
        frames, truth = load_frames(anchor)
        model = build_model(
            frames,
            history_anchors=args.history_anchors,
            lookback_days=args.lookback_days,
            max_instances=args.max_instances,
            max_related_rows=args.max_related_rows,
        )
        if args.all_tables:
            # What the model could see before trimming was introduced. The gap
            # between the two runs is the price of sending fewer tables.
            model._needed_tables = lambda entity, spec: set(model._tables)

        if args.dry_run:
            spec = model.validate_pql(pql)
            payload, plan = model._payload(spec, indices=None, anchor_time=anchor)
            size = len(json.dumps(payload, default=str).encode("utf8"))
            print(
                f"{anchor.date()}  orders={len(frames['orders']):>6,}  "
                f"payload={size / 1024 / 1024:6.1f} MiB  "
                f"context={plan['context_instances']}  predict={plan['predict_instances']}  "
                f"rows={plan['related_rows']:,}"
            )
            continue

        frame = model.predict(pql, anchor_time=anchor)
        report, scores, labels, detail = evaluate(frame, truth, anchor, frames)
        if args.save_scores:
            args.save_scores.mkdir(parents=True, exist_ok=True)
            detail.to_csv(args.save_scores / f"{anchor.date()}.csv", index=False)
        if report["auc"] is None:
            # No positives in this window: the anchor sits too close to the end
            # of the data for a 180-day silence to have been observed.
            print(f"{report['anchor']}  n={report['scored']:>4}  no positives, skipped")
            continue
        results.append(report)
        pooled_scores.append(scores)
        pooled_labels.append(labels)
        print(
            f"{report['anchor']}  n={report['scored']:>4}  pos={report['positive_rate']:.1%}  "
            f"AUC={report['auc']:.3f}  AP={report['ap']:.3f}  "
            f"P@top={report['p_at_10']:.3f} (top {report['top_share']:.0%})  "
            f"lift={report['lift_at_10']}  distinct={report['distinct_scores']}  "
            f"open_auc={report['auc_open']}  "
            f"recency_rule={report['recency_auc']}  "
            f"4feat_rule={report['hand_baseline_auc']}"
        )

    if results:
        print("\n--- per cause (one-vs-rest AUC) ---")
        causes = ("competitor", "demand_decline", "non_renewal", "bankruptcy")
        for cause in causes:
            values = [r["auc_by_cause"][cause] for r in results if cause in r["auc_by_cause"]]
            if values:
                print(f"  {cause:<16} {np.mean(values):.3f}  (anchors: {', '.join(f'{v:.3f}' for v in values)})")

        # Pooling the anchors gives one number over ~40 positives instead of
        # four numbers over ten, which is the difference between a signal and
        # an anecdote.
        if pooled_labels:
            all_scores = np.concatenate(pooled_scores)
            all_labels = np.concatenate(pooled_labels)
            print("\n--- pooled ---")
            print(
                f"  n={len(all_labels)}  positives={int(all_labels.sum())} "
                f"({all_labels.mean():.1%})"
            )
            pooled_p, pooled_share = precision_at(all_scores, all_labels, 0.10)
            print(f"  AUC={auc(all_scores, all_labels):.3f}  "
                  f"AP={average_precision(all_scores, all_labels):.3f}  "
                  f"P@top={pooled_p:.3f} (top {pooled_share:.0%})  "
                  f"lift={pooled_p / all_labels.mean():.2f}")

        mean_auc = float(np.mean([r["auc"] for r in results if r["auc"] is not None]))
        open_aucs = [r["auc_open"] for r in results if r.get("auc_open") is not None]
        hand = [r["hand_baseline_auc"] for r in results if r.get("hand_baseline_auc") is not None]
        print(f"\nmean AUC across anchors: {mean_auc:.3f}")
        if open_aucs:
            print(f"mean AUC, open questions only: {np.mean(open_aucs):.3f}")
        if hand:
            print(f"mean AUC, hand-built baseline: {np.mean(hand):.3f}")
        if args.json:
            args.json.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf8")
            print(f"wrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
