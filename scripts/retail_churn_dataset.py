#!/usr/bin/env python
# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Build a wholesale-distribution library that really does lose customers.

Why another dataset
-------------------

WideWorldImporters is an OLTP sample, not a CRM history: almost every customer
orders every quarter, so a churn label there has a 3% positive rate concentrated
in the first months of the relationship. A model trained on it can only learn
the prior, and no amount of pipeline work changes that.

This script builds the thing that dataset lacks: a wholesaler with 600 business
customers, three and a half years of orders, invoices, payments, support
tickets and web activity — and a churn process with four distinct causes, each
leaving a different trace in the operational tables.

The answer key lives in a separate schema (``scoring``) so it can be joined for
evaluation but is never visible to the model.

What makes the churn realistic
------------------------------

Real attrition is not one phenomenon. Four causes are simulated, weighted the
way a B2B distributor sees them:

===============  ======  ==========================================
cause            share   observable trace before the last order
===============  ======  ==========================================
bankruptcy       12%     none — the account simply stops
competitor       30%     quotes stop, tickets rise, CSAT falls
demand_decline   33%     basket shrinks, category range narrows
non_renewal      25%     silence in the weeks before renewal
===============  ======  ==========================================

Two features keep the problem from becoming trivially separable:

* every customer gets a *visibility* draw, and about one in seven churners is
  nearly invisible regardless of cause;
* one in ten survivors passes through a temporary rough patch — falling
  revenue, a burst of tickets — and then recovers. Those are the false alarms a
  real model has to survive.

Usage
-----

::

    python scripts/retail_churn_dataset.py --dry-run          # rows only
    python scripts/retail_churn_dataset.py                    # build + load
    python scripts/retail_churn_dataset.py --customers 1200 --seed 7
    python scripts/retail_churn_dataset.py --csv out/         # no database

The connection defaults to the ``POSTGRES_*`` settings in ``.env``.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------
# the shape of the world
# --------------------------------------------------------------------------

START = pd.Timestamp("2021-01-01")
END = pd.Timestamp("2025-06-30")
HORIZON_DAYS = 180

REGIONS = [
    (1, "华东", "上海"),
    (2, "华北", "北京"),
    (3, "华南", "广州"),
    (4, "西南", "成都"),
    (5, "华中", "武汉"),
]

# Industry decides which categories a customer can plausibly buy. Without this
# the basket is random and no product-level signal can exist.
INDUSTRIES = [
    # name,        categories,                    share, name suffix
    ("食品加工", ["原料", "包装"], 0.22, "食品"),
    ("连锁餐饮", ["原料", "生鲜", "包装"], 0.18, "餐饮管理"),
    ("医药制造", ["原料", "包装"], 0.11, "医药"),
    ("电子制造", ["包装", "耗材"], 0.16, "电子科技"),
    ("建材批发", ["原料", "耗材"], 0.14, "建材"),
    ("商超零售", ["生鲜", "包装", "耗材"], 0.19, "商贸"),
]

CATEGORIES = ["原料", "包装", "耗材", "生鲜"]

SEGMENTS = ["SMB", "MidMarket", "Enterprise"]
SEGMENT_P = [0.55, 0.30, 0.15]

# Orders per month before the activity draw and seasonality.
SEGMENT_RATE = {"SMB": 0.55, "MidMarket": 1.15, "Enterprise": 2.30}
SEGMENT_LINES = {"SMB": (1, 3), "MidMarket": (2, 6), "Enterprise": (4, 12)}
SEGMENT_QTY = {"SMB": (1, 6), "MidMarket": (5, 40), "Enterprise": (20, 180)}
SEGMENT_CREDIT = {"SMB": (8_000, 60_000), "MidMarket": (60_000, 260_000), "Enterprise": (260_000, 1_200_000)}
SEGMENT_TERMS = {"SMB": 30, "MidMarket": 45, "Enterprise": 60}

CONTRACT_TYPES = ["monthly", "annual", "spot"]
CONTRACT_P = [0.45, 0.40, 0.15]

CHANNELS = ["web", "phone", "field", "edi"]
CHANNEL_P = [0.46, 0.27, 0.18, 0.09]

# Contract type drives the buying rhythm, and this is what makes the problem
# hard in a real CRM: an annual-contract account places four large orders a
# year, so it looks dormant for months while being perfectly healthy. Reading
# recency without the contract context condemns half the good customers.
CONTRACT_RHYTHM = {"monthly": 1.00, "annual": 0.30, "spot": 0.52}
CONTRACT_BASKET = {"monthly": 1.0, "annual": 2.4, "spot": 1.5}

# Month-of-year multiplier for a distributor: Q4 stocking, a July/August lull.
SEASONALITY = np.array(
    [0.86, 0.80, 1.05, 1.00, 1.02, 1.10, 0.85, 0.82, 1.06, 1.11, 1.20, 1.14]
)

CITIES = [
    "上海", "苏州", "宁波", "杭州", "南京", "北京", "天津", "青岛",
    "广州", "深圳", "东莞", "佛山", "成都", "重庆", "武汉", "长沙",
]
NAME_CORE = [
    "恒丰", "嘉禾", "正大", "汇诚", "华源", "金鼎", "瑞泰", "泰和",
    "博远", "联合", "中盛", "新元", "天成", "德胜", "康泰", "宏图",
    "鑫源", "永昌", "立信", "众和", "海通", "广联", "润丰", "安泰",
]
OWNERS = [
    "李伟", "王芳", "张涛", "刘敏", "陈静", "杨帆", "赵磊", "周颖",
    "吴强", "徐丽", "孙浩", "马超", "朱琳", "胡军", "郭鹏", "林晓",
]

TICKET_CATEGORIES = ["物流延迟", "质量问题", "账单争议", "产品咨询", "退换货"]
TICKET_P = [0.31, 0.27, 0.18, 0.16, 0.08]

PAYMENT_METHODS = ["bank_transfer", "acceptgiro", "check"]
PAYMENT_P = [0.84, 0.11, 0.05]

# --------------------------------------------------------------------------
# the churn process
# --------------------------------------------------------------------------

CHURN_CAUSES = ["bankruptcy", "competitor", "demand_decline", "non_renewal"]
CHURN_P = [0.12, 0.30, 0.33, 0.25]

# (days of visible decline before the last order, how hard it bites)
PRECURSOR = {
    "bankruptcy": (0, 0.00),
    "competitor": (155, 0.80),
    "demand_decline": (275, 0.55),
    "non_renewal": (125, 0.50),
}

# Three-and-a-half-year cumulative churn, before the contract adjustment.
CHURN_BY_SEGMENT = {"SMB": 0.34, "MidMarket": 0.22, "Enterprise": 0.12}
CHURN_BY_CONTRACT = {"monthly": 0.06, "annual": -0.05, "spot": 0.10}

FALSE_ALARM_SHARE = 0.10   # survivors that wobble and recover
INVISIBLE_SHARE = 0.14     # churners whose trace is nearly absent

# Real accounts go quiet without leaving. This is the single most important
# detail in the whole dataset: without it, "days since the last order" separates
# churners from survivors almost perfectly (AUC ~0.99) and the model learns
# nothing else. A dormancy is shorter than the 180-day label window, so a
# customer who sleeps and comes back is correctly *not* a churn.
DORMANCY_RATE = {"SMB": 1.05, "MidMarket": 0.62, "Enterprise": 0.34}
SHORT_SLEEP = (45, 150)
LONG_SLEEP = (185, 240)


def _days(count) -> pd.Timedelta:
    """A timedelta that does not warn.

    ``pd.Timedelta(days=n)`` builds a numpy "generic" unit on pandas 2.3 with
    numpy 2.5, and every addition of one to a Timestamp raises a deprecation
    warning — twenty thousand of them in one test run. Naming the unit keeps the
    run readable and survives the numpy change.
    """
    return pd.Timedelta(int(count), "D")


def _dormancy_length(rng: np.random.Generator) -> int:
    """Most silences are seasonal; a sixth run past the 180-day label window.

    The long ones matter: a customer who goes quiet for eight months and then
    comes back is a negative example with a terrible recency value. Without
    them, "days since last order" alone scores AUC 0.99 and the rest of the
    schema is decoration. But too many of them and the churn label fills up
    with silences nothing could have foreseen, which makes the task hard for
    the wrong reason.
    """
    if rng.random() < 0.93:
        return int(rng.integers(*SHORT_SLEEP))
    return int(rng.integers(*LONG_SLEEP))


@dataclass
class World:
    """Everything the loader writes, already keyed the way the tables are."""

    regions: pd.DataFrame
    customers: pd.DataFrame
    products: pd.DataFrame
    orders: pd.DataFrame
    order_lines: pd.DataFrame
    invoices: pd.DataFrame
    payments: pd.DataFrame
    tickets: pd.DataFrame
    web_sessions: pd.DataFrame
    churn: pd.DataFrame

    def tables(self) -> dict[str, pd.DataFrame]:
        return {
            "regions": self.regions,
            "customers": self.customers,
            "products": self.products,
            "orders": self.orders,
            "order_lines": self.order_lines,
            "invoices": self.invoices,
            "payments": self.payments,
            "support_tickets": self.tickets,
            "web_sessions": self.web_sessions,
            "churn_events": self.churn,
        }


def _company_names(
    rng: np.random.Generator, industry_idx: np.ndarray, suffixes: list[str]
) -> list[str]:
    """Names that read like a customer list, with the trade in the right place.

    A food wholesaler is never called "…电子科技有限公司": the suffix has to
    follow the industry or the first row of the table looks fabricated.
    """
    seen: set[str] = set()
    names: list[str] = []
    while len(names) < len(industry_idx):
        suffix = suffixes[int(industry_idx[len(names)])]
        name = (
            CITIES[int(rng.integers(len(CITIES)))]
            + NAME_CORE[int(rng.integers(len(NAME_CORE)))]
            + suffix
        )
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _month_factor(day: pd.Timestamp, industry: str) -> float:
    factor = float(SEASONALITY[day.month - 1])
    if industry == "食品加工" and day.month in (11, 12):
        factor *= 1.10
    if industry in ("电子制造", "建材批发") and day.month in (7, 8):
        factor *= 0.90
    return factor


def _price_index(day: pd.Timestamp, launched: pd.Timestamp) -> float:
    """Two list-price rises a year, compounding — no step changes mid-quarter."""
    years = (day - launched).days / 365.25
    return float(1.0 + 0.042 * max(years, 0.0))


def _decline_progress(day: pd.Timestamp, start, end) -> float:
    """0 before the decline begins, 1 at the last order."""
    if start is None or end is None:
        return 0.0
    if day < start:
        return 0.0
    span = max((end - start).days, 1)
    return float(min((day - start).days / span, 1.0))


def generate(customers: int = 600, seed: int = 20261109) -> World:
    """Simulate the library. Deterministic for a given seed and size."""
    rng = np.random.default_rng(seed)

    regions = pd.DataFrame(REGIONS, columns=["region_id", "region_name", "hq_city"])

    # -- products ---------------------------------------------------------
    # Prices by category; a category's spread is what makes baskets differ.
    price_band = {
        "原料": (18.0, 210.0),
        "包装": (1.8, 26.0),
        "耗材": (4.0, 68.0),
        "生鲜": (7.0, 95.0),
    }
    product_rows = []
    for pid in range(1, 141):
        category = str(rng.choice(CATEGORIES))
        low, high = price_band[category]
        price = float(np.exp(rng.uniform(np.log(low), np.log(high))))
        margin = float(rng.uniform(0.18, 0.46))
        launched = START - _days(int(rng.integers(90, 2200)))
        discontinued = pd.NaT
        if rng.random() < 0.10:
            discontinued = START + _days(int(rng.integers(200, 1400)))
        product_rows.append(
            (
                pid,
                f"{category[:2]}-{pid:04d}",
                f"{category}{int(rng.integers(1, 60))}号规格",
                category,
                round(price, 2),
                round(price * (1 - margin), 2),
                launched.normalize(),
                discontinued.normalize() if pd.notna(discontinued) else pd.NaT,
            )
        )
    products = pd.DataFrame(
        product_rows,
        columns=[
            "product_id", "sku", "product_name", "category",
            "list_price", "standard_cost", "launched_on", "discontinued_on",
        ],
    )
    products_by_category = {
        category: products.loc[products["category"] == category, "product_id"].to_numpy()
        for category in CATEGORIES
    }

    # -- customers --------------------------------------------------------
    segment = rng.choice(SEGMENTS, size=customers, p=SEGMENT_P)
    contract = rng.choice(CONTRACT_TYPES, size=customers, p=CONTRACT_P)
    industry_idx = rng.choice(
        len(INDUSTRIES), size=customers, p=[row[2] for row in INDUSTRIES]
    )
    industry = np.array([INDUSTRIES[i][0] for i in industry_idx])
    region_id = rng.integers(1, len(REGIONS) + 1, customers)
    owner = rng.choice(OWNERS, size=customers)

    suffixes = [row[3] + "有限公司" for row in INDUSTRIES]
    company = _company_names(rng, industry_idx, suffixes)

    # 40% of the book predates the observation window: those are the settled
    # accounts, and they churn less than the ones signed during it.
    incumbent = rng.random(customers) < 0.40
    tenure_days = np.where(
        incumbent,
        rng.integers(400, 2900, customers),
        0,
    )
    signup = np.where(
        incumbent,
        START - pd.to_timedelta(tenure_days, unit="D"),
        START + pd.to_timedelta(rng.integers(0, 1420, customers), unit="D"),
    )
    signup = pd.to_datetime(signup)

    credit = np.array(
        [rng.uniform(*SEGMENT_CREDIT[s]) for s in segment]
    )
    # Terms move in 15-day steps and never below 30: a net-15 B2B account is
    # not a thing, and it would look like a data-entry error.
    terms = np.array(
        [
            SEGMENT_TERMS[s] + (15 if rng.random() < 0.28 else 0)
            for s in segment
        ]
    )

    # Digital adoption is a customer property: EDI-only accounts leave no web
    # trace at all, and that absence is itself informative but not conclusive.
    digital = rng.random(customers) < 0.62

    # -- who leaves, when, and why ---------------------------------------
    base = np.array([CHURN_BY_SEGMENT[s] for s in segment])
    base = base + np.array([CHURN_BY_CONTRACT[c] for c in contract])
    base = np.where(incumbent, base * 0.85, base * 1.05)
    # A chronically strained account walks sooner.
    credit_rank = (credit - credit.min()) / max(float(credit.max() - credit.min()), 1.0)
    base = np.clip(base + 0.10 * (1.0 - credit_rank), 0.02, 0.95)
    churns = rng.random(customers) < base

    cause = rng.choice(CHURN_CAUSES, size=customers, p=CHURN_P)
    # Annual contracts are the ones that lapse; spot buyers just evaporate.
    cause = np.where(
        (contract == "annual") & (rng.random(customers) < 0.45), "non_renewal", cause
    )
    cause = np.where(
        (contract == "spot") & (rng.random(customers) < 0.55), "competitor", cause
    )

    # Churn date spread across the window, skewed a little toward the middle so
    # both the early and the late anchors see positives.
    span_days = (END - START).days
    draw = rng.beta(2.2, 1.9, customers)
    churn_date = START + pd.to_timedelta((draw * span_days).astype(int), unit="D")
    # Nobody churns before they exist, and a relationship that lasted two
    # months is not a relationship ending.
    earliest = pd.Series(signup) + _days(150)
    churn_date = pd.Series(churn_date).where(pd.Series(churn_date) >= earliest, earliest)
    churn_date = churn_date.where(pd.Series(churns))
    # A churn after the last observation is right-censoring, not a label.
    churn_date = churn_date.where(churn_date <= END)

    visibility = rng.beta(2.0, 2.0, customers)
    visibility = np.where(rng.random(customers) < INVISIBLE_SHARE, visibility * 0.14, visibility)

    decline_start = pd.Series([pd.NaT] * customers, dtype="datetime64[ns]")
    for i in range(customers):
        if pd.isna(churn_date[i]):
            continue
        lead, strength = PRECURSOR[cause[i]]
        if lead <= 0:
            continue
        start_at = churn_date[i] - _days(int(lead * (0.75 + 0.5 * visibility[i])))
        decline_start[i] = max(start_at, pd.Timestamp(signup[i]) + _days(30))

    decline_strength = np.array(
        [PRECURSOR[c][1] * visibility[i] for i, c in enumerate(cause)]
    )

    # Survivors who wobble: a rough patch with no ending.
    false_alarm = (~pd.Series(churns).to_numpy()) & (rng.random(customers) < FALSE_ALARM_SHARE)
    for i in np.where(false_alarm)[0]:
        at = START + _days(int(rng.integers(200, span_days - 260)))
        decline_start[i] = at
        decline_strength[i] = float(rng.uniform(0.30, 0.55))

    # -- orders, lines, invoices, payments --------------------------------
    order_rows: list[tuple] = []
    line_rows: list[tuple] = []
    invoice_rows: list[tuple] = []
    payment_rows: list[tuple] = []

    order_id = 0
    line_id = 0
    invoice_id = 0
    payment_id = 0

    all_days = pd.date_range(START, END, freq="D")
    sleeps: list[list[tuple[pd.Timestamp, pd.Timestamp]]] = [[] for _ in range(customers)]

    for i in range(customers):
        cid = i + 1
        seg = segment[i]
        ind = industry[i]
        allowed = INDUSTRIES[industry_idx[i]][1]
        base_rate = SEGMENT_RATE[seg] / 30.0 * CONTRACT_RHYTHM[contract[i]]
        basket = CONTRACT_BASKET[contract[i]]
        activity = float(np.exp(rng.normal(0.0, 0.34)))
        first_day = max(pd.Timestamp(signup[i]), START)
        last_day = (
            pd.Timestamp(churn_date[i]) if pd.notna(churn_date[i]) else END
        )
        if last_day <= first_day:
            continue
        stop = min(last_day, END)
        ramp_days = 180.0
        has_digital = bool(digital[i])
        priority_customer = rng.random() < 0.35
        last_order_day = None
        # One order every this many days on average, before the rise-and-fall
        # shape below. Used to notice when an account is overdue.
        cycle_days = 1.0 / max(base_rate, 1e-6)

        # Periods of silence that are not churn. Kept shorter than the label
        # window so a returning customer stays a negative example.
        n_sleeps = int(rng.poisson(DORMANCY_RATE[seg]))
        usable = max((stop - first_day).days - 90, 1)
        for _ in range(n_sleeps):
            begin = first_day + _days(int(rng.integers(90, max(usable, 91))))
            length = _dormancy_length(rng)
            if contract[i] == "annual":
                # An annual account already buys four times a year; a long nap on
                # top of that would fake attrition the business does not have.
                length = min(length, 120)
            sleeps[i].append((begin, begin + _days(length)))

        for day in pd.date_range(first_day, stop, freq="D"):
            if any(begin <= day <= finish for begin, finish in sleeps[i]):
                continue
            rate = base_rate * activity * _month_factor(day, ind)
            # New accounts build up; the first two quarters are quieter.
            ramp = min((day - first_day).days / ramp_days, 1.0) * 0.5 + 0.5
            progress = _decline_progress(day, decline_start[i], churn_date[i])
            strength = decline_strength[i]
            if progress > 0:
                # A fading account does not fall off a cliff: the deepest dip is
                # ~70% below its own baseline, and only in the final weeks.
                rate *= max(1.0 - 0.72 * strength * progress, 0.10)
            elif last_order_day is not None:
                # A contracted account carries a renewal clock: the longer it has
                # been since the last order, the likelier the next one. Without
                # this the annual rhythm's exponential tail produces two-year
                # silences that look exactly like churn but are labelled alive —
                # the single biggest source of unlearnable noise in this data.
                overdue = max((day - last_order_day).days / cycle_days - 1.0, 0.0)
                rate *= 1.0 + 2.6 * min(overdue, 1.5)
            if rng.random() >= rate * ramp:
                continue

            # basket
            low, high = SEGMENT_LINES[seg]
            n_lines = int(rng.integers(low, high + 1))
            if progress > 0:
                n_lines = max(1, int(round(n_lines * (1.0 - 0.45 * strength * progress))))
                # Narrowing range: a fading account stops exploring.
                if rng.random() < 0.65 * strength * progress and len(allowed) > 1:
                    allowed_now = [allowed[0]]
                else:
                    allowed_now = allowed
            else:
                allowed_now = allowed

            discount = float(rng.uniform(0.0, 0.08))
            if day.month in (6, 11):
                discount = float(rng.uniform(0.05, 0.18))
            if progress > 0:
                # A customer about to leave starts asking for price relief.
                discount += 0.06 * strength * progress

            status = "completed"
            roll = rng.random()
            if roll < 0.04:
                status = "cancelled"
            elif roll < 0.06 and day > END - _days(20):
                status = "pending"

            order_id += 1
            total = 0.0
            lines_here: list[tuple] = []
            for _ in range(n_lines):
                cat = str(rng.choice(allowed_now))
                pool = products_by_category[cat]
                pid = int(rng.choice(pool))
                product = products.iloc[pid - 1]
                if pd.notna(product["discontinued_on"]) and day > product["discontinued_on"]:
                    if rng.random() < 0.5:
                        continue
                q_low, q_high = SEGMENT_QTY[seg]
                qty = max(1, int(rng.integers(q_low, q_high + 1) * basket))
                if progress > 0:
                    qty = max(1, int(round(qty * (1.0 - 0.35 * strength * progress))))
                unit_price = float(product["list_price"]) * _price_index(day, product["launched_on"])
                # A little haggling on every line, more on large orders.
                line_disc = min(discount + float(rng.uniform(0.0, 0.05)), 0.42)
                line_total = round(qty * unit_price * (1.0 - line_disc), 2)
                returned = 0
                if status == "completed":
                    return_rate = {"原料": 0.015, "包装": 0.035, "耗材": 0.06, "生鲜": 0.09}[cat]
                    if rng.random() < return_rate:
                        returned = int(rng.integers(1, max(2, qty // 3 + 1)))
                line_id += 1
                lines_here.append(
                    (
                        line_id, order_id, pid, qty,
                        round(unit_price, 2), round(line_disc, 4),
                        round(line_total, 2), returned,
                    )
                )
                total += line_total

            if not lines_here:
                order_id -= 1
                continue

            order_rows.append(
                (
                    order_id, cid, day.normalize(),
                    str(rng.choice(CHANNELS, p=CHANNEL_P)),
                    status, round(total, 2), round(discount, 4),
                    int(region_id[i]),
                )
            )
            line_rows.extend(lines_here)
            if status == "completed":
                last_order_day = day

            if status != "completed":
                continue

            # -- invoice + payment ----------------------------------------
            invoice_id += 1
            issued = day + _days(int(rng.integers(0, 4)))
            due = issued + _days(int(terms[i]))
            # Payment behaviour degrades with the decline: this is the earliest
            # trace a finance team sees, months before revenue drops.
            # Most invoices clear a few days early; late payment is the
            # exception, and it is the first place a decline shows up.
            delay = int(rng.normal(-4.5, 5.0))
            if progress > 0:
                delay += int(rng.uniform(6, 34) * strength * progress)
            paid = due + _days(delay)
            settled = paid <= END and rng.random() >= 0.07
            invoice_rows.append(
                (invoice_id, order_id, cid, issued.normalize(), due.normalize(),
                 round(total, 2), "paid" if settled else "open")
            )
            if settled:
                payment_id += 1
                payment_rows.append(
                    (
                        payment_id, invoice_id, paid.normalize(),
                        round(total, 2),
                        str(rng.choice(PAYMENT_METHODS, p=PAYMENT_P)),
                    )
                )

    # -- support tickets ---------------------------------------------------
    ticket_rows: list[tuple] = []
    ticket_id = 0
    for i in range(customers):
        cid = i + 1
        progress_window_start = decline_start[i]
        first_day = max(pd.Timestamp(signup[i]), START)
        last_day = min(
            pd.Timestamp(churn_date[i]) if pd.notna(churn_date[i]) else END, END
        )
        if last_day <= first_day:
            continue
        months = max((last_day - first_day).days / 30.0, 1.0)
        # Baseline: a few tickets a year, fewer for quiet accounts.
        expected = months * 0.42 * float(np.exp(rng.normal(0, 0.4)))
        count = int(rng.poisson(expected))
        if pd.notna(progress_window_start):
            window_months = max((last_day - progress_window_start).days / 30.0, 0.5)
            count += int(rng.poisson(window_months * 1.15 * max(decline_strength[i], 0.05)))
        for _ in range(count):
            at = first_day + _days(
                rng.integers(0, max((last_day - first_day).days, 1))
            )
            if any(begin <= at <= finish for begin, finish in sleeps[i]):
                continue
            in_decline = pd.notna(progress_window_start) and at >= progress_window_start
            priority = str(rng.choice(["Low", "Medium", "High"], p=[0.55, 0.32, 0.13]))
            if in_decline:
                priority = str(rng.choice(["Low", "Medium", "High"], p=[0.18, 0.42, 0.40]))
            csat = float(np.clip(rng.normal(4.25, 0.55), 1.0, 5.0))
            if in_decline:
                csat = float(np.clip(rng.normal(2.85, 0.75), 1.0, 5.0))
            ticket_id += 1
            ticket_rows.append(
                (
                    ticket_id, cid, at.normalize(),
                    str(rng.choice(TICKET_CATEGORIES, p=TICKET_P)),
                    priority,
                    round(csat, 2),
                    round(float(np.clip(rng.normal(9.0 if not in_decline else 27.0, 7.0), 0.5, 160.0)), 1),
                    at.normalize() + _days(int(rng.integers(1, 14))),
                )
            )

    # -- web activity ------------------------------------------------------
    session_rows: list[tuple] = []
    session_id = 0
    for i in range(customers):
        if not digital[i]:
            # No web presence at all — a NULL is the honest record for an
            # EDI-only account, and forcing a zero would fake a signal.
            continue
        cid = i + 1
        first_day = max(pd.Timestamp(signup[i]), START)
        last_day = min(
            pd.Timestamp(churn_date[i]) if pd.notna(churn_date[i]) else END, END
        )
        if last_day <= first_day:
            continue
        base_sessions = {"SMB": 6, "MidMarket": 22, "Enterprise": 58}[segment[i]]
        base_sessions *= float(np.exp(rng.normal(0, 0.35)))
        for month_start in pd.date_range(first_day, last_day, freq="MS"):
            day = month_start + _days(int(rng.integers(0, 26)))
            if day > last_day:
                continue
            progress = _decline_progress(day, decline_start[i], churn_date[i])
            strength = decline_strength[i]
            asleep = any(begin <= day <= finish for begin, finish in sleeps[i])
            sessions = int(rng.poisson(base_sessions))
            views = int(sessions * rng.uniform(2.2, 6.5))
            quotes = int(rng.poisson(max(sessions * 0.12, 0.1)))
            if asleep:
                # A dormant account still gets the newsletter; it just does not
                # shop. Traffic thins out but stops short of zero.
                sessions = max(1, int(sessions * 0.16))
                views = max(1, int(views * 0.18))
                quotes = 0
            if progress > 0:
                sessions = int(sessions * max(1.0 - 0.72 * strength * progress, 0.05))
                quotes = int(quotes * max(1.0 - 0.88 * strength * progress, 0.0))
                views = int(views * max(1.0 - 0.60 * strength * progress, 0.05))
            session_id += 1
            session_rows.append(
                (
                    session_id, cid, day.normalize(), sessions, views, quotes,
                    int(rng.integers(0, max(quotes, 1) + 2)),
                    round(float(rng.uniform(0, 1)), 3),
                )
            )

    orders = pd.DataFrame(
        order_rows,
        columns=[
            "order_id", "customer_id", "order_date", "channel",
            "status", "order_total", "discount_pct", "region_id",
        ],
    )
    order_lines = pd.DataFrame(
        line_rows,
        columns=[
            "line_id", "order_id", "product_id", "quantity",
            "unit_price", "discount_pct", "line_total", "returned_qty",
        ],
    )
    invoices = pd.DataFrame(
        invoice_rows,
        columns=[
            "invoice_id", "order_id", "customer_id", "invoice_date",
            "due_date", "amount", "status",
        ],
    )
    payments = pd.DataFrame(
        payment_rows,
        columns=["payment_id", "invoice_id", "paid_date", "amount", "method"],
    )
    tickets = pd.DataFrame(
        ticket_rows,
        columns=[
            "ticket_id", "customer_id", "opened_at", "category",
            "priority", "csat", "first_response_hours", "closed_at",
        ],
    )
    web_sessions = pd.DataFrame(
        session_rows,
        columns=[
            "session_id", "customer_id", "session_date", "sessions",
            "page_views", "quote_requests", "cart_abandonments", "avg_engagement",
        ],
    )

    people = pd.DataFrame(
        {
            "customer_id": np.arange(1, customers + 1),
            "company_name": company,
            "industry": industry,
            "segment": segment,
            "region_id": region_id,
            "account_owner": owner,
            "credit_limit": np.round(credit, 2),
            "payment_terms_days": terms,
            "contract_type": contract,
            "signup_date": signup,
            "has_web_portal": digital,
        }
    )

    # The answer key, with the cause attached so an evaluation can separate
    # "predictable attrition" from "the account simply vanished".
    churn = pd.DataFrame(
        {
            "customer_id": np.arange(1, customers + 1),
            "churn_date": churn_date,
            "churn_cause": np.where(pd.Series(churns), cause, None),
            "decline_start": decline_start,
            "is_censored": churn_date.isna(),
        }
    )

    # A customer who never placed a first order did not churn — they never
    # converted. Counting them as attrition inflates the rate and teaches the
    # model that signing up is the same as leaving.
    bought = set(int(value) for value in orders.loc[orders["status"] == "completed", "customer_id"])
    never = ~churn["customer_id"].isin(bought)

    # The label is defined by the data, not by the draw that scheduled it.
    # `COUNT(orders.*, 0, 180, days) = 0` is what the model will be scored
    # against, so membership in the positive class has to be exactly "the last
    # order is at least HORIZON_DAYS before the window closes". A customer
    # scheduled to leave in the final quarter has not actually left yet, and
    # keeping them as a positive would teach a label the query contradicts.
    last_order = (
        orders.loc[orders["status"] == "completed"]
        .groupby("customer_id")["order_date"]
        .max()
    )
    final_order = churn["customer_id"].map(last_order)
    silence = (END - final_order).dt.days
    derived = silence.fillna(-1) >= HORIZON_DAYS

    churn["churn_date"] = final_order.where(derived)
    churn["churn_cause"] = churn["churn_cause"].where(derived)
    churn["decline_start"] = churn["decline_start"].where(derived)

    # Some accounts fall quiet without having been scheduled to. From the
    # outside that is a lapsed agreement, not a collapse, so it joins the
    # non-renewal bucket rather than becoming an unexplained positive.
    imputed = derived & churn["churn_cause"].isna() & ~never
    churn.loc[imputed, "churn_cause"] = "non_renewal"

    churn["is_censored"] = churn["churn_date"].isna()
    churn["never_activated"] = never

    return World(
        regions=regions,
        customers=people,
        products=products,
        orders=orders,
        order_lines=order_lines,
        invoices=invoices,
        payments=payments,
        tickets=tickets,
        web_sessions=web_sessions,
        churn=churn,
    )


# --------------------------------------------------------------------------
# self-checks
# --------------------------------------------------------------------------


def _auc(scores: np.ndarray, labels: np.ndarray) -> float:
    """Rank-based AUC, no sklearn dependency."""
    labels = labels.astype(bool)
    if labels.all() or not labels.any():
        return float("nan")
    order = np.argsort(scores)
    ranks = np.empty(len(scores), dtype="float64")
    ranks[order] = np.arange(1, len(scores) + 1)
    # Average ranks for ties so a constant score lands on 0.5.
    _, inverse, counts = np.unique(scores, return_inverse=True, return_counts=True)
    if (counts > 1).any():
        for group, count in enumerate(counts):
            if count > 1:
                mask = inverse == group
                ranks[mask] = ranks[mask].mean()
    positives = int(labels.sum())
    negatives = len(labels) - positives
    return float(
        (ranks[labels].sum() - positives * (positives + 1) / 2) / (positives * negatives)
    )


def describe(world: World) -> dict:
    """Numbers to check before trusting the data with a model."""
    orders = world.orders
    customers = world.customers
    churn = world.churn

    lapsed = churn["churn_date"].notna()
    converted = ~churn["never_activated"] if "never_activated" in churn else pd.Series(True, index=churn.index)
    report: dict = {
        "rows": {name: len(frame) for name, frame in world.tables().items()},
        "customers": int(len(customers)),
        "converted": int(converted.sum()),
        "never_activated": int((~converted).sum()),
        "churned": int(lapsed.sum()),
        "churn_rate": round(float(lapsed[converted].mean()), 4) if converted.any() else 0.0,
        "churn_by_segment": {
            str(seg): round(float(lapsed[converted & (customers["segment"] == seg)].mean()), 4)
            for seg in SEGMENTS
        },
        "churn_by_cause": {
            str(cause): int((churn["churn_cause"] == cause).sum())
            for cause in CHURN_CAUSES
        },
        "window": [str(orders["order_date"].min().date()), str(orders["order_date"].max().date())],
        "revenue": round(float(orders.loc[orders["status"] == "completed", "order_total"].sum()), 2),
    }

    # Does the library actually carry a pre-churn trace? Compare order rate in
    # the 120 days before the last order against the same customer's earlier
    # baseline. A real decline shows up here; a random label would not.
    rates = []
    for cid, churn_day, decline in zip(
        churn["customer_id"], churn["churn_date"], churn["decline_start"]
    ):
        if pd.isna(churn_day) or pd.isna(decline):
            continue
        mine = orders[(orders["customer_id"] == cid) & (orders["status"] == "completed")]
        late = mine[(mine["order_date"] > churn_day - _days(120)) & (mine["order_date"] <= churn_day)]
        early = mine[(mine["order_date"] > churn_day - _days(480)) & (mine["order_date"] <= churn_day - _days(240))]
        if len(early) >= 3:
            rates.append(len(late) / max(len(early), 1))
    if rates:
        report["decline_ratio_median"] = round(float(np.median(rates)), 3)

    # A single-feature baseline, measured the way the model is actually used:
    # stand at an anchor, look only at what was known by then, and ask whether
    # the customer left in the following HORIZON_DAYS. Scoring at the end of the
    # window instead would hand the feature the answer and report AUC 0.99.
    completed = orders[orders["status"] == "completed"]
    joined = churn.copy()

    anchors = pd.date_range(
        START + _days(540), END - _days(HORIZON_DAYS),
        freq=pd.offsets.QuarterEnd(),
    )
    per_anchor = []
    pooled_scores: list[float] = []
    pooled_labels: list[bool] = []
    for anchor in anchors:
        # Only what was visible at the anchor. Using the full history here would
        # hand the feature the answer: an account that leaves in three months
        # still has a small recency today, and the AUC would report a leak.
        seen = completed[completed["order_date"] <= anchor]
        visible_last = seen.groupby("customer_id")["order_date"].max()
        at_risk = joined[joined["churn_date"].isna() | (joined["churn_date"] > anchor)]
        if "never_activated" in at_risk:
            at_risk = at_risk[~at_risk["never_activated"]]
        if "signup_date" in customers.columns:
            signed = customers.set_index("customer_id")["signup_date"]
            at_risk = at_risk[at_risk["customer_id"].map(signed) <= anchor]
        left = (
            at_risk["churn_date"].notna()
            & (at_risk["churn_date"] > anchor)
            & (at_risk["churn_date"] <= anchor + _days(HORIZON_DAYS))
        )
        recency = (anchor - at_risk["customer_id"].map(visible_last)).dt.days
        usable = recency.notna() & (recency < HORIZON_DAYS)
        if not usable.any() or left[usable].sum() < 5:
            continue
        per_anchor.append(
            {
                "anchor": str(anchor.date()),
                "customers": int(usable.sum()),
                "positive_rate": round(float(left[usable].mean()), 4),
                "recency_auc": round(
                    _auc(recency[usable].to_numpy(dtype="float64"),
                         left[usable].to_numpy()), 3
                ),
            }
        )
        pooled_scores.extend(recency[usable].tolist())
        pooled_labels.extend(left[usable].tolist())

    if per_anchor:
        report["anchor_baseline"] = per_anchor
        report["pooled_recency_auc"] = round(
            _auc(np.array(pooled_scores, dtype="float64"), np.array(pooled_labels)), 3
        )
    return report


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

DDL = """
CREATE SCHEMA IF NOT EXISTS retail;
CREATE SCHEMA IF NOT EXISTS scoring;

DROP TABLE IF EXISTS scoring.churn_events CASCADE;
DROP TABLE IF EXISTS retail.web_sessions CASCADE;
DROP TABLE IF EXISTS retail.support_tickets CASCADE;
DROP TABLE IF EXISTS retail.payments CASCADE;
DROP TABLE IF EXISTS retail.invoices CASCADE;
DROP TABLE IF EXISTS retail.order_lines CASCADE;
DROP TABLE IF EXISTS retail.orders CASCADE;
DROP TABLE IF EXISTS retail.products CASCADE;
DROP TABLE IF EXISTS retail.customers CASCADE;
DROP TABLE IF EXISTS retail.regions CASCADE;

CREATE TABLE retail.regions (
    region_id     integer PRIMARY KEY,
    region_name   varchar(32)  NOT NULL,
    hq_city       varchar(32)  NOT NULL
);

CREATE TABLE retail.customers (
    customer_id        integer PRIMARY KEY,
    company_name       varchar(128) NOT NULL,
    industry           varchar(32)  NOT NULL,
    segment            varchar(16)  NOT NULL,
    region_id          integer      NOT NULL REFERENCES retail.regions(region_id),
    account_owner      varchar(32),
    credit_limit       numeric(14,2) NOT NULL,
    payment_terms_days integer      NOT NULL,
    contract_type      varchar(16)  NOT NULL,
    signup_date        date         NOT NULL,
    has_web_portal     boolean      NOT NULL
);

CREATE TABLE retail.products (
    product_id      integer PRIMARY KEY,
    sku             varchar(32) NOT NULL UNIQUE,
    product_name    varchar(96) NOT NULL,
    category        varchar(16) NOT NULL,
    list_price      numeric(12,2) NOT NULL,
    standard_cost   numeric(12,2) NOT NULL,
    launched_on     date         NOT NULL,
    discontinued_on date
);

CREATE TABLE retail.orders (
    order_id      integer PRIMARY KEY,
    customer_id   integer NOT NULL REFERENCES retail.customers(customer_id),
    order_date    date    NOT NULL,
    channel       varchar(16) NOT NULL,
    status        varchar(16) NOT NULL,
    order_total   numeric(14,2) NOT NULL,
    discount_pct  numeric(6,4)  NOT NULL,
    region_id     integer NOT NULL REFERENCES retail.regions(region_id)
);

CREATE TABLE retail.order_lines (
    line_id      integer PRIMARY KEY,
    order_id     integer NOT NULL REFERENCES retail.orders(order_id),
    product_id   integer NOT NULL REFERENCES retail.products(product_id),
    quantity     integer NOT NULL,
    unit_price   numeric(12,2) NOT NULL,
    discount_pct numeric(6,4)  NOT NULL,
    line_total   numeric(14,2) NOT NULL,
    returned_qty integer NOT NULL
);

CREATE TABLE retail.invoices (
    invoice_id   integer PRIMARY KEY,
    order_id     integer NOT NULL REFERENCES retail.orders(order_id),
    customer_id  integer NOT NULL REFERENCES retail.customers(customer_id),
    invoice_date date    NOT NULL,
    due_date     date    NOT NULL,
    amount       numeric(14,2) NOT NULL,
    status       varchar(16) NOT NULL
);

CREATE TABLE retail.payments (
    payment_id integer PRIMARY KEY,
    invoice_id integer NOT NULL REFERENCES retail.invoices(invoice_id),
    paid_date  date    NOT NULL,
    amount     numeric(14,2) NOT NULL,
    method     varchar(24) NOT NULL
);

CREATE TABLE retail.support_tickets (
    ticket_id            integer PRIMARY KEY,
    customer_id          integer NOT NULL REFERENCES retail.customers(customer_id),
    opened_at            date    NOT NULL,
    category             varchar(24) NOT NULL,
    priority             varchar(12) NOT NULL,
    csat                 numeric(3,2),
    first_response_hours numeric(6,1),
    closed_at            date
);

CREATE TABLE retail.web_sessions (
    session_id        integer PRIMARY KEY,
    customer_id       integer NOT NULL REFERENCES retail.customers(customer_id),
    session_date      date    NOT NULL,
    sessions          integer NOT NULL,
    page_views        integer NOT NULL,
    quote_requests    integer NOT NULL,
    cart_abandonments integer NOT NULL,
    avg_engagement    numeric(4,3) NOT NULL
);

CREATE TABLE scoring.churn_events (
    customer_id    integer PRIMARY KEY REFERENCES retail.customers(customer_id),
    churn_date     date,
    churn_cause    varchar(24),
    decline_start  date,
    is_censored    boolean NOT NULL,
    never_activated boolean NOT NULL DEFAULT false
);
"""

COMMENTS = """
COMMENT ON SCHEMA retail IS '批发分销业务库：客户、产品、订单、回款、服务与网站行为';
COMMENT ON TABLE retail.regions IS '销售区域维表';
COMMENT ON COLUMN retail.regions.region_name IS '大区名称，如华东、华南';
COMMENT ON TABLE retail.customers IS 'B2B 客户主数据，一行一个签约客户';
COMMENT ON COLUMN retail.customers.segment IS '客户分层：SMB 小微 / MidMarket 中型 / Enterprise 大型';
COMMENT ON COLUMN retail.customers.contract_type IS '合同类型：monthly 月度、annual 年度、spot 单次采购';
COMMENT ON COLUMN retail.customers.credit_limit IS '授信额度，反映客户规模与账期风险';
COMMENT ON COLUMN retail.customers.payment_terms_days IS '约定账期天数，30/45/60';
COMMENT ON COLUMN retail.customers.has_web_portal IS '是否开通线上下单门户；纯 EDI 客户为 false，无网站行为数据';
COMMENT ON TABLE retail.products IS '产品目录，含类别、牌价与成本';
COMMENT ON COLUMN retail.products.category IS '产品大类：原料、包装、耗材、生鲜';
COMMENT ON TABLE retail.orders IS '订单头，一行一次下单';
COMMENT ON COLUMN retail.orders.status IS '订单状态：completed 已完成、cancelled 已取消、pending 待处理';
COMMENT ON COLUMN retail.orders.order_total IS '订单金额合计（含税前的行金额汇总）';
COMMENT ON TABLE retail.order_lines IS '订单明细行，一单多行';
COMMENT ON COLUMN retail.order_lines.returned_qty IS '退货数量，0 表示无退货';
COMMENT ON TABLE retail.invoices IS '发票/应收单，按订单开具';
COMMENT ON COLUMN retail.invoices.due_date IS '到期日 = 开票日 + 客户账期';
COMMENT ON COLUMN retail.invoices.status IS '收款状态：paid 已收、open 未结清';
COMMENT ON TABLE retail.payments IS '回款记录，一张发票对应一笔回款';
COMMENT ON COLUMN retail.payments.paid_date IS '实际回款日期；晚于 due_date 即为逾期';
COMMENT ON TABLE retail.support_tickets IS '客户服务工单';
COMMENT ON COLUMN retail.support_tickets.csat IS '客户满意度评分 1-5，越低越不满';
COMMENT ON COLUMN retail.support_tickets.first_response_hours IS '首次响应耗时（小时）';
COMMENT ON TABLE retail.web_sessions IS '线上下单门户的月度行为汇总；未开通门户的客户无记录';
COMMENT ON COLUMN retail.web_sessions.quote_requests IS '报价请求次数，是购买意向的先行指标';
COMMENT ON TABLE scoring.churn_events IS '流失真值（评分用，模型不可见）：客户最后下单日与流失原因';
COMMENT ON COLUMN scoring.churn_events.churn_date IS '客户最后一次有效下单的日期；为空表示到观测期末仍在合作（右删失）';
COMMENT ON COLUMN scoring.churn_events.churn_cause IS '流失原因：bankruptcy 经营终止、competitor 被竞品替换、demand_decline 需求萎缩、non_renewal 合同未续';
COMMENT ON COLUMN scoring.churn_events.never_activated IS '注册后从未成交，属未转化而非流失，评估时应排除';
"""

VIEWS = """
CREATE OR REPLACE VIEW retail.v_customer_revenue_monthly AS
SELECT
    c.customer_id,
    c.company_name,
    c.segment,
    date_trunc('month', o.order_date)::date AS month,
    count(*)                AS order_count,
    sum(o.order_total)      AS revenue
FROM retail.customers c
JOIN retail.orders o ON o.customer_id = c.customer_id
WHERE o.status = 'completed'
GROUP BY 1, 2, 3, 4;

CREATE OR REPLACE VIEW retail.v_customer_last_order AS
SELECT
    c.customer_id,
    c.company_name,
    max(o.order_date) AS last_order_date,
    count(*)          AS lifetime_orders,
    sum(o.order_total) AS lifetime_revenue
FROM retail.customers c
LEFT JOIN retail.orders o
       ON o.customer_id = c.customer_id AND o.status = 'completed'
GROUP BY 1, 2;

CREATE INDEX IF NOT EXISTS ix_orders_customer_date ON retail.orders (customer_id, order_date);
CREATE INDEX IF NOT EXISTS ix_orders_date ON retail.orders (order_date);
CREATE INDEX IF NOT EXISTS ix_lines_order ON retail.order_lines (order_id);
CREATE INDEX IF NOT EXISTS ix_invoices_customer ON retail.invoices (customer_id, invoice_date);
CREATE INDEX IF NOT EXISTS ix_payments_invoice ON retail.payments (invoice_id);
CREATE INDEX IF NOT EXISTS ix_tickets_customer ON retail.support_tickets (customer_id, opened_at);
CREATE INDEX IF NOT EXISTS ix_sessions_customer ON retail.web_sessions (customer_id, session_date);
"""

TABLES_IN_ORDER = [
    ("regions", "retail.regions"),
    ("customers", "retail.customers"),
    ("products", "retail.products"),
    ("orders", "retail.orders"),
    ("order_lines", "retail.order_lines"),
    ("invoices", "retail.invoices"),
    ("payments", "retail.payments"),
    ("support_tickets", "retail.support_tickets"),
    ("web_sessions", "retail.web_sessions"),
    ("churn_events", "scoring.churn_events"),
]


def _connect(database: str, autocommit: bool = False):
    import psycopg

    return psycopg.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ.get("POSTGRES_USER", "postgres"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
        dbname=database,
        autocommit=autocommit,
    )


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


def _cell(value):
    """A COPY-safe value: pandas sentinels become SQL NULL, numpy scalars unwrap."""
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, np.generic):
        value = value.item()
        if value is None:
            return None
        if isinstance(value, float) and math.isnan(value):
            return None
    return value


def load(world: World, database: str, recreate_db: bool = True) -> None:
    """Create the database if needed, build the schema, copy the rows in."""
    import psycopg

    try:
        admin = _connect("postgres", autocommit=True)
        with admin.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,))
            exists = cur.fetchone() is not None
            if not exists:
                cur.execute(f'CREATE DATABASE "{database}"')
                print(f"created database {database}")
            elif recreate_db:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (database,),
                )
                cur.execute(f'DROP DATABASE IF EXISTS "{database}"')
                cur.execute(f'CREATE DATABASE "{database}"')
                print(f"recreated database {database}")
        admin.close()
    except psycopg.Error as error:
        raise SystemExit(f"cannot create database {database}: {error}") from error

    conn = _connect(database)
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute(COMMENTS)
        conn.commit()

        for key, qualified in TABLES_IN_ORDER:
            frame = world.tables()[key]
            if frame.empty:
                print(f"  {qualified:<28} 0 rows (skipped)")
                continue
            columns = ", ".join(frame.columns)
            with conn.cursor() as cur:
                with cur.copy(f"COPY {qualified} ({columns}) FROM STDIN") as copy:
                    for row in frame.itertuples(index=False, name=None):
                        copy.write_row([_cell(value) for value in row])
            conn.commit()
            print(f"  {qualified:<28} {len(frame):>8,} rows")

        with conn.cursor() as cur:
            cur.execute(VIEWS)
        conn.commit()
    finally:
        conn.close()


def write_csv(world: World, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, frame in world.tables().items():
        frame.to_csv(directory / f"{name}.csv", index=False)
    print(f"wrote {len(world.tables())} csv files to {directory}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--customers", type=int, default=600)
    parser.add_argument("--seed", type=int, default=20261109)
    parser.add_argument("--database", default="retail_churn")
    parser.add_argument("--csv", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true", help="simulate and report only")
    parser.add_argument("--keep", action="store_true", help="do not recreate the database")
    args = parser.parse_args()

    _load_env()
    world = generate(args.customers, args.seed)

    report = describe(world)
    print("\n--- shape ---")
    for name, rows in report["rows"].items():
        print(f"  {name:<16} {rows:>9,}")
    print("\n--- churn ---")
    print(f"  customers         {report['customers']}")
    print(f"  churned           {report['churned']} ({report['churn_rate']:.1%})")
    print(f"  by segment        {report['churn_by_segment']}")
    print(f"  by cause          {report['churn_by_cause']}")
    print("\n--- signal ---")
    print(f"  window            {report['window']}")
    print(f"  completed revenue {report['revenue']:,.0f}")
    if "decline_ratio_median" in report:
        print(f"  decline ratio     {report['decline_ratio_median']:.2f} (120d before churn / baseline)")
    if "pooled_recency_auc" in report:
        print(f"  recency AUC       {report['pooled_recency_auc']:.3f} (pooled over anchors)")
    for row in report.get("anchor_baseline", []):
        print(
            f"    {row['anchor']}  n={row['customers']:>4}  "
            f"pos={row['positive_rate']:.1%}  recency_auc={row['recency_auc']:.3f}"
        )

    if args.csv:
        write_csv(world, args.csv)
    if args.dry_run:
        return 0

    print("\n--- load ---")
    load(world, args.database, recreate_db=not args.keep)
    print(f"\ndone: database {args.database}, schemas retail + scoring")
    print(
        "connect: postgresql://<user>:<password>@" 
        f"{os.environ.get('POSTGRES_HOST', 'localhost')}:{os.environ.get('POSTGRES_PORT', '5432')}/{args.database}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
