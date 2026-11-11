# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Invariants for the synthetic retail library.

These are not tests of the simulator's numbers — those are supposed to move when
the parameters change. They pin the properties that would make a result wrong
rather than different: a label that disagrees with its own definition, a company
whose name contradicts its industry, a payment term no business uses.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "retail_churn_dataset", ROOT / "scripts" / "retail_churn_dataset.py"
)
assert _SPEC is not None and _SPEC.loader is not None
retail = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(retail)


@pytest.fixture(scope="module")
def world():
    return retail.generate(customers=140, seed=11)


def test_company_name_carries_the_trade_it_actually_practises(world):
    """A food wholesaler must not be called "…电子科技有限公司"."""
    suffix_for = {row[0]: row[3] for row in retail.INDUSTRIES}
    for industry, name in zip(
        world.customers["industry"], world.customers["company_name"]
    ):
        assert name.endswith(suffix_for[industry] + "有限公司"), (industry, name)


def test_payment_terms_are_ones_a_business_would_actually_offer(world):
    """Net-15 is not a B2B term; it reads like a data-entry mistake."""
    assert set(world.customers["payment_terms_days"]) <= {30, 45, 60, 75}


def test_a_customer_who_never_ordered_is_not_counted_as_churned(world):
    """Never converting and then leaving are different problems."""
    bought = set(world.orders.loc[world.orders["status"] == "completed", "customer_id"])
    silent = world.churn[~world.churn["customer_id"].isin(bought)]

    assert len(silent), "the sample produced no unconverted customers at all"
    assert silent["churn_date"].isna().all()
    assert silent["never_activated"].all()


def test_every_churn_label_is_a_180_day_silence(world):
    """The label must agree with the query it will be scored against.

    A customer marked as churned whose last order sits inside the label window
    is unlabelable: `COUNT(orders.*, 0, 180, days) = 0` would say they are
    active. That mismatch was worth 0.07 AUC when it slipped through.
    """
    done = world.orders[world.orders["status"] == "completed"]
    last_order = done.groupby("customer_id")["order_date"].max()

    labelled = world.churn[world.churn["churn_date"].notna()]
    assert len(labelled), "no churned customers to check"

    final = last_order.reindex(labelled["customer_id"].to_numpy())
    silence = (retail.END - final).dt.days

    # `churn_date` is the last order date, so the silence it implies must be at
    # least the label window; otherwise the two definitions disagree.
    assert (silence >= retail.HORIZON_DAYS).all(), silence.describe()
    assert (labelled["churn_date"].to_numpy() == final.to_numpy()).all()


def test_no_churn_predates_the_relationship(world):
    """A relationship cannot end before it starts."""
    signed = world.customers.set_index("customer_id")["signup_date"]
    labelled = world.churn[world.churn["churn_date"].notna()]
    tenure = (labelled["churn_date"] - labelled["customer_id"].map(signed)).dt.days
    assert (tenure > 0).all(), tenure.min()


def test_orders_and_tickets_stay_inside_the_observation_window(world):
    """Nothing may leak past the last observed day."""
    assert world.orders["order_date"].max() <= retail.END
    assert world.orders["order_date"].min() >= retail.START
    if len(world.tickets):
        assert world.tickets["opened_at"].max() <= retail.END


def test_line_totals_add_up_to_the_order_total(world):
    """The header total is a roll-up; if it drifts, every revenue query drifts."""
    rolled = world.order_lines.groupby("order_id")["line_total"].sum().round(2)
    headers = world.orders.set_index("order_id")["order_total"].round(2)
    merged = pd.concat([rolled, headers], axis=1, join="inner").dropna()
    assert len(merged), "no orders joined to their lines"
    assert (merged.iloc[:, 0] - merged.iloc[:, 1]).abs().max() <= 0.02


def test_a_live_customer_does_not_go_quiet_for_years(world):
    """Silence longer than the label window is a churn, not a gap.

    An account that orders twice, vanishes for two and a half years and then
    comes back is labelled "still a customer" at every anchor in between, while
    looking to any model like the deadest account on the books. A handful of
    these is realistic — a customer who nearly left and was wined back — but
    before the renewal clock went in, 13% of the book had a gap over a year and
    the whole top decile of the ranking was made of them.
    """
    done = world.orders[world.orders["status"] == "completed"]
    longest = (
        done.sort_values("order_date")
        .groupby("customer_id")["order_date"]
        .apply(lambda days: days.diff().dt.days.max())
    )

    assert longest.max(skipna=True) <= 730, longest.max(skipna=True)
    over_a_year = int((longest > 365).sum())
    assert over_a_year / len(longest) <= 0.10, (over_a_year, len(longest))
