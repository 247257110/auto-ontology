# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Behaviour a deployment is judged on, over a dataset shaped like a real one.

The unit tests elsewhere cover each piece on its own. These ask the questions a
deployment actually gets wrong: a filter spelled the way the asker said it rather
than the way the warehouse holds it, and a forecast that reads as being about
next month when the data stopped a year ago.
"""

from __future__ import annotations


import pandas as pd

from auto_ontology.retrieval.kumo.column_reference import (
    build_column_reference,
    safe_values,
)
from auto_ontology.retrieval.kumo.pql_gen import (
    _data_edge,
    _forecast_anchor,
    _stale_data_note,
    _STALE_ANCHOR_DAYS,
)

TIERS = ["Enterprise", "Mid-Market", "SMB"]
REGIONS = ["EMEA", "NAMER", "APJ"]

CATALOG = [
    {
        "name": "customers",
        "schema_name": "main",
        "database_name": "sales",
        "pk": ["customer_id"],
        "columns": [
            {"name": "customer_id", "data_type": "int"},
            {"name": "tier", "data_type": "text", "sample_values": TIERS},
            {"name": "region", "data_type": "text", "sample_values": REGIONS},
            {
                "name": "contact_email",
                "data_type": "text",
                "sample_values": ["ana@acme.com", "bo@acme.com"],
            },
            {"name": "lifetime_value", "data_type": "double"},
        ],
    }
]

STYPES = {
    "customers": {
        "customer_id": "ID",
        "tier": "categorical",
        "region": "categorical",
        "contact_email": "categorical",
        "lifetime_value": "numerical",
    }
}


class _Warehouse:
    """A source whose orders stop on ``last_day``."""

    def __init__(self, last_day: str) -> None:
        self.last_day = pd.Timestamp(last_day)

    def execute(self, sql: str) -> pd.DataFrame:
        return pd.DataFrame({"m": [self.last_day]})


def _reference() -> str:
    return build_column_reference(CATALOG, STYPES)


def test_the_model_is_given_the_spellings_the_warehouse_uses() -> None:
    """A filter on 'enterprise' finds nothing in a column holding 'Enterprise'."""
    reference = _reference()

    for tier in TIERS:
        assert repr(tier) in reference
    assert "tier:" in reference


def test_a_value_the_data_does_not_hold_is_not_offered() -> None:
    reference = _reference()

    assert "Government" not in reference
    assert "LATAM" not in reference


def test_the_list_does_not_claim_to_be_every_value() -> None:
    """It comes from a profiling sample; read as exhaustive it invites a wrong filter."""
    reference = _reference()

    assert "sample" in reference
    assert "not proof of every value" in reference


def test_a_column_holding_contact_details_supplies_no_vocabulary() -> None:
    """Offering it would put customer contact details into every prompt."""
    reference = _reference()

    assert "acme.com" not in reference
    assert safe_values("contact_email", ["ana@acme.com"]) == []


def test_a_measurement_is_not_offered_as_a_vocabulary() -> None:
    """Listing amounts as values to filter on invites a filter on one of them."""
    assert "lifetime_value" not in _reference()


def test_an_id_is_not_offered_as_a_vocabulary() -> None:
    assert "customer_id" not in _reference()


def test_a_model_with_no_vocabulary_is_told_so_rather_than_left_guessing() -> None:
    stypes: dict[str, dict[str, str]] = {"customers": {"tier": "numerical"}}

    assert build_column_reference(CATALOG, stypes) == ""


def test_a_forecast_over_fresh_data_is_anchored_at_today() -> None:
    today = pd.Timestamp("2026-08-28")
    warehouse = _Warehouse("2026-12-31")

    anchor = _forecast_anchor(
        "PREDICT SUM(orders.amt, 0, 30, days) FOR EACH customers.customer_id",
        warehouse,
        {"orders": "ts"},
        now=today,
        table_names={"orders": "main.orders"},
    )

    assert anchor == today
    assert (
        _stale_data_note(
            _data_edge(
                "PREDICT SUM(orders.amt, 0, 30, days) FOR EACH customers.customer_id",
                warehouse,
                {"orders": "ts"},
                table_names={"orders": "main.orders"},
            ),
            now=today,
        )
        is None
    )


def test_a_forecast_over_stale_data_is_still_anchored_today_and_says_so() -> None:
    """The data stopped a year ago; the forecast is still about next month,
    but the reader has to know the history behind it is stale."""
    today = pd.Timestamp("2026-08-28")
    warehouse = _Warehouse("2025-06-30")
    pql = "PREDICT SUM(orders.amt, 0, 30, days) FOR EACH customers.customer_id"

    anchor = _forecast_anchor(
        pql,
        warehouse,
        {"orders": "ts"},
        now=today,
        table_names={"orders": "main.orders"},
    )
    note = _stale_data_note(
        _data_edge(
            pql, warehouse, {"orders": "ts"}, table_names={"orders": "main.orders"}
        ),
        now=today,
    )

    assert anchor == today
    assert note is not None
    assert "2025-06-30" in note
    assert "数据截止到" in note


def test_a_warehouse_loaded_on_a_delay_is_not_flagged_every_time() -> None:
    """A note on every ordinary run is a note nobody reads."""
    today = pd.Timestamp("2026-08-28")

    within = today - pd.Timedelta(days=_STALE_ANCHOR_DAYS)

    assert _stale_data_note(within, now=today) is None
    assert _stale_data_note(within - pd.Timedelta(days=1), now=today) is not None


def test_the_window_is_the_future_even_when_the_data_ends_today() -> None:
    """A forecast from today must not be pulled back to the last warehouse row.

    The old policy backed the anchor off by ``horizon + 7`` days and silently
    answered "the next 90 days" from a date months ago. The window is allowed
    to run past the data: that is exactly what is being predicted.
    """
    today = pd.Timestamp("2026-08-28")
    last_day = pd.Timestamp("2026-09-10")
    warehouse = _Warehouse(str(last_day.date()))

    anchor = _forecast_anchor(
        "PREDICT SUM(orders.amt, 0, 90, days) FOR EACH customers.customer_id",
        warehouse,
        {"orders": "ts"},
        now=today,
        table_names={"orders": "main.orders"},
    )

    assert anchor == today
    assert anchor + pd.Timedelta(days=90) > last_day


def test_no_data_edge_means_no_claim_about_one() -> None:
    assert _stale_data_note(None) is None


def test_a_stale_dataset_still_supplies_real_spellings() -> None:
    """The two failures are independent: stale data still has correct values."""
    today = pd.Timestamp("2026-08-28")
    warehouse = _Warehouse("2025-06-30")

    assert (
        _stale_data_note(
            _data_edge(
                "PREDICT SUM(orders.amt, 0, 30, days) FOR EACH customers.customer_id",
                warehouse,
                {"orders": "ts"},
                table_names={"orders": "main.orders"},
            ),
            now=today,
        )
        is not None
    )
    assert repr("Enterprise") in _reference()
