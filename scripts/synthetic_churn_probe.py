#!/usr/bin/env python
"""Score the hosted predictor on a library whose churn mechanism we control.

WWI cannot answer the question "does this pipeline learn anything?" because
almost every customer orders every quarter, so the churn label has almost no
positive examples and every prediction collapses onto the prior. This script
builds a small B2B library where customers really do stop ordering, feeds it
through the same materialisation path the application uses, and scores the
returned probabilities against a held-out future the model never saw.

The split is the important part: the model gets orders up to ``CUTOFF`` and
nothing after it, while scoring reads the full history. So a high AUC means the
model forecast the future rather than reproduced what it was handed.

Usage::

    python scripts/synthetic_churn_probe.py [--customers 400] [--dry-run]

``--dry-run`` builds the payload and prints its size without spending a call on
the endpoint, which is what you want while tuning the data shape.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from auto_ontology.retrieval.kumo.framed_graph import build_framed_graph
from auto_ontology.retrieval.kumo.hosted import HostedConfig, HostedKumoModel

# -- the shape of the world ------------------------------------------------

CUTOFF = pd.Timestamp("2016-09-30")  # last day the model may see
HORIZON = 90                         # days the label looks ahead
START = pd.Timestamp("2015-01-01")
FUTURE_END = CUTOFF + pd.Timedelta(days=HORIZON + 60)

SEGMENTS = ("SMB", "MidMarket", "Enterprise")
SEG_P = (0.50, 0.35, 0.15)
MONTHLY_ORDERS = {"SMB": 0.55, "MidMarket": 1.15, "Enterprise": 2.10}
ORDER_VALUE = {"SMB": 120.0, "MidMarket": 320.0, "Enterprise": 780.0}
REGIONS = ("North", "South", "East", "West")
CHANNELS = ("web", "phone", "field")


def generate(customers: int, seed: int = 20261109) -> dict[str, pd.DataFrame]:
    """A wholesale library with a latent churn process and observable warnings.

    Each customer is given a risk score from attributes a real system would
    hold (segment, credit limit), plus a noisy draw. High-risk customers enter a
    decline period — orders thin out and support tickets appear — and then stop
    ordering. The noise is deliberate: without it every customer would be
    perfectly predictable and a high AUC would prove nothing about the pipeline.
    """
    rng = np.random.default_rng(seed)

    segment = rng.choice(SEGMENTS, size=customers, p=SEG_P)
    credit = np.where(
        segment == "Enterprise",
        rng.uniform(20_000, 60_000, customers),
        np.where(
            segment == "MidMarket",
            rng.uniform(8_000, 25_000, customers),
            rng.uniform(1_000, 9_000, customers),
        ),
    )
    region = rng.choice(REGIONS, size=customers)
    opened = pd.to_datetime("2013-01-01") + pd.to_timedelta(
        rng.integers(0, 700, customers), unit="D"
    )

    # Risk: small accounts with thin credit are likelier to walk, and the draw
    # below is the part no table records.
    credit_rank = (credit - credit.min()) / max(credit.max() - credit.min(), 1.0)
    seg_risk = np.select(
        [segment == "SMB", segment == "MidMarket"], [0.62, 0.30], default=0.0
    )
    latent = 2.4 * (0.55 * seg_risk + 0.45 * (1.0 - credit_rank)) - 1.55
    risk = 1.0 / (1.0 + np.exp(-(latent + rng.normal(0, 0.55, customers))))

    # Who leaves, and when. Everyone has the same chance of a decline, but the
    # ones who leave early are the risky ones — that is the signal to find.
    declines = rng.random(customers) < np.clip(0.10 + 0.80 * risk, 0.0, 0.95)
    decline_start = pd.Series(
        pd.to_datetime("2015-08-01")
        + pd.to_timedelta(rng.integers(0, 430, customers), unit="D"),
        dtype="datetime64[ns]",
    )
    churn_day = decline_start + pd.to_timedelta(rng.integers(45, 115, customers), unit="D")
    decline_start = decline_start.where(declines)
    churn_day = churn_day.where(declines)

    order_rows: list[tuple] = []
    ticket_rows: list[tuple] = []
    order_id = 0
    ticket_id = 0

    for i in range(customers):
        cid = i + 1
        rate = MONTHLY_ORDERS[segment[i]]
        value = ORDER_VALUE[segment[i]]
        start = max(opened[i], START)
        stop = churn_day[i] if declines[i] else FUTURE_END
        # A renewed customer is worth more than a first-year one.
        tenure = max((start - opened[i]).days / 365.0, 0.0)

        when = start
        while when < stop:
            when = when + pd.Timedelta(days=float(rng.exponential(30.0 / rate)))
            if when >= stop or when > FUTURE_END:
                break
            if when < START:
                continue
            fading = declines[i] and when >= decline_start[i]
            # A declining account orders less often and in smaller baskets, but
            # not to zero — the drop is the warning, not the label.
            if fading and rng.random() > 0.35:
                continue
            amount = value * float(rng.lognormal(0.0, 0.45))
            amount *= 1.0 + min(tenure, 1.0) * 0.25
            if fading:
                amount *= 0.55
            order_id += 1
            order_rows.append(
                (
                    order_id,
                    cid,
                    when.normalize(),
                    round(amount, 2),
                    str(rng.choice(CHANNELS, p=(0.55, 0.30, 0.15))),
                )
            )

        # Tickets are the second warning: a declining customer complains.
        if declines[i]:
            tick = decline_start[i]
            last_ticket = min(churn_day[i], FUTURE_END)
            while tick < last_ticket:
                tick = tick + pd.Timedelta(days=float(rng.integers(12, 34)))
                if tick >= last_ticket:
                    break
                if tick > CUTOFF:
                    break
                ticket_id += 1
                ticket_rows.append(
                    (
                        ticket_id,
                        cid,
                        tick.normalize(),
                        str(rng.choice(("High", "Medium"), p=(0.62, 0.38))),
                        round(float(rng.uniform(2, 40)), 1),
                    )
                )
        elif rng.random() < 0.25:
            ticket_id += 1
            ticket_rows.append(
                (
                    ticket_id,
                    cid,
                    (START + pd.Timedelta(days=float(rng.integers(0, 600)))),
                    "Low",
                    round(float(rng.uniform(1, 8)), 1),
                )
            )

    orders = pd.DataFrame(
        order_rows, columns=["orderid", "customerid", "orderdate", "amount", "channel"]
    )
    orders["orderdate"] = pd.to_datetime(orders["orderdate"])

    tickets = pd.DataFrame(
        ticket_rows,
        columns=["ticketid", "customerid", "openeddate", "severity", "resolvehours"],
    )
    if len(tickets):
        tickets["openeddate"] = pd.to_datetime(tickets["openeddate"])

    people = pd.DataFrame(
        {
            "customerid": np.arange(1, customers + 1),
            "segment": segment,
            "creditlimit": np.round(credit, 2),
            "region": region,
            "accountopeneddate": opened,
        }
    )

    # The answer key. Kept out of the frames the model sees.
    truth = pd.DataFrame({"customerid": people["customerid"], "churn_date": churn_day})
    return {"customers": people, "orders": orders, "support_tickets": tickets}, truth


def load_kumo_env(path: Path = ROOT / ".env") -> None:
    """Take the Kumo settings from ``.env`` when the shell has not already."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("KUMO_") and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def build_model(frames: dict[str, pd.DataFrame], **overrides) -> HostedKumoModel:
    """Wire the frames the same way ``predictor`` does on the hosted path."""
    graph = build_framed_graph(frames, {"customers": ["customerid"]})
    # The contracts are known here, so state them instead of guessing.
    for src, column, dst in (
        ("orders", "customerid", "customers"),
        ("support_tickets", "customerid", "customers"),
    ):
        if column in frames[src].columns:
            graph.link(src, column, dst)
    # An account's opening date is not an event: anchoring on it would place
    # instances in 2013 instead of at the end of the observed history.
    if graph.tables["customers"].time_column:
        graph.tables["customers"].time_column = None

    config = HostedConfig.from_env()
    if config is None:
        raise SystemExit(
            "no hosted endpoint configured: set KUMO_HOSTED_API_URL (and "
            "KUMO_HOSTED_API_KEY) in .env or the environment"
        )
    if overrides:
        config = dataclasses.replace(config, **overrides)
    return HostedKumoModel(graph, frames, config)


def auc(score: np.ndarray, label: np.ndarray) -> float:
    """Rank-based AUC, so a tie-heavy score cannot be scored generously."""
    order = np.argsort(score)
    ranks = np.empty(len(score), dtype=float)
    ranks[order] = np.arange(1, len(score) + 1)
    # Average the ranks inside a tie group.
    frame = pd.DataFrame({"s": score, "r": ranks})
    ranks = frame.groupby("s")["r"].transform("mean").to_numpy()
    positives = label.sum()
    negatives = len(label) - positives
    if not positives or not negatives:
        return float("nan")
    return float(
        (ranks[label].sum() - positives * (positives + 1) / 2) / (positives * negatives)
    )


def score(frame: pd.DataFrame, orders: pd.DataFrame, horizon: int) -> dict:
    """Compare the returned probabilities with what actually happened after."""
    anchors = frame["anchor_timestamp"].unique()
    if len(anchors) != 1:
        print(f"! judge expected one anchor, got {anchors}")
    # The endpoint echoes the anchor in UTC; the library's dates are naive
    # calendar days. Compare on the day the window opens, matching the
    # ``(anchor, anchor + horizon]`` rule the labels are built with.
    anchor = pd.Timestamp(anchors[0])
    if anchor.tz is not None:
        anchor = anchor.tz_convert(None)
    anchor = anchor.normalize()
    window = orders[
        (orders["orderdate"] > anchor)
        & (orders["orderdate"] <= anchor + pd.Timedelta(days=horizon))
    ]
    ordered = set(window["customerid"])
    label = ~frame["ENTITY"].isin(ordered).to_numpy()

    out = {
        "anchor": str(anchor),
        "entities": len(frame),
        "positive_rate": round(float(label.mean()), 4),
        "probability_quantiles": {
            q: float(f"{frame['True_PROB'].quantile(q):.3g}")
            for q in (0.0, 0.1, 0.5, 0.9, 1.0)
        },
        "distinct_probabilities": int(frame["True_PROB"].nunique()),
    }
    out["auc"] = auc(frame["True_PROB"].to_numpy(), label)

    # How much better than random is the top decile?
    top = frame["True_PROB"].rank(pct=True, method="first") > 0.9
    base = label.mean()
    out["precision_at_10pct"] = float(label[top].mean()) if top.any() else float("nan")
    out["lift_at_10pct"] = (
        float(out["precision_at_10pct"] / base) if base and top.any() else float("nan")
    )
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--customers", type=int, default=400)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pql", default=None)
    args = parser.parse_args()
    load_kumo_env()

    frames, truth = generate(args.customers)
    full_orders = frames["orders"]

    # What the model may see: the same tables, cut at CUTOFF.
    seen = {name: df for name, df in frames.items()}
    seen["orders"] = full_orders[full_orders["orderdate"] <= CUTOFF].reset_index(drop=True)
    seen["support_tickets"] = frames["support_tickets"][
        frames["support_tickets"]["openeddate"] <= CUTOFF
    ].reset_index(drop=True)

    print(
        f"library: {len(seen['customers'])} customers, {len(seen['orders'])} orders, "
        f"{len(seen['support_tickets'])} tickets, observed through {CUTOFF.date()}"
    )
    print(f"answer key: {int(truth['churn_date'].notna().sum())} customers do stop ordering")

    model = build_model(seen)
    pql = args.pql or (
        f"PREDICT COUNT(orders.*, 0, {HORIZON}, days) = 0 FOR EACH customers.customerid"
    )
    print(f"pql: {pql}")

    if args.dry_run:
        spec = model.validate_pql(pql)
        payload, _plan = model._payload(spec, indices=None, anchor_time=None)
        blob = json.dumps(payload, allow_nan=False)
        print(
            f"payload: {len(blob) / 1024**2:.1f} MiB, "
            f"context={len(payload['context']['instance_table']['rows'])} instances, "
            f"related rows="
            f"{sum(len(t['rows']) for t in payload['context']['related_tables'].values())}"
        )
        return 0

    started = time.perf_counter()
    frame = model.predict(pql)
    elapsed = time.perf_counter() - started
    print(f"answered in {elapsed:.1f}s, {len(frame)} rows")

    # What the model was actually trained on. A prior-shaped output usually
    # means the context windows held almost no positive examples, not that the
    # model ignored its inputs.
    spec = model.last_spec
    payload, _plan = model._payload(spec, indices=None, anchor_time=None)
    table = payload["context"]["instance_table"]
    li = table["columns"].index("label")
    ai = table["columns"].index("anchor_time")
    labels = pd.Series([row[li] for row in table["rows"]])
    anchors_of = pd.Series([row[ai] for row in table["rows"]])
    print("context labels by anchor:")
    for name, group in labels.groupby(anchors_of):
        print(f"  {name}: {len(group):>5} instances, {float(group.mean()):.3f} positive")

    result = score(frame, full_orders, HORIZON)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
