# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The hosted request the materialiser builds — shape, leakage, refusals.

No network: ``_payload`` is exercised directly, and the transport is monkeypatched.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from auto_ontology.retrieval.kumo.hosted import (
    DEFAULT_HOSTED_URL,
    HostedConfig,
    HostedKumoModel,
    hosted_enabled,
    hosted_url,
)


class _Column:
    def __init__(self, name: str, stype: str) -> None:
        self.name = name
        self.stype = stype


class _Table:
    def __init__(self, name, columns, primary_key, time_column=None) -> None:
        self.name = name
        self.columns = [_Column(name_, stype_) for name_, stype_ in columns]
        self.primary_key = _Column(primary_key, "ID")
        self.primary_key_columns = (primary_key,)
        self.time_column = _Column(time_column, "timestamp") if time_column else None


class _Edge:
    def __init__(self, src, fkey, dst) -> None:
        self.src_table, self.fkey, self.dst_table = src, fkey, dst


class _Graph:
    def __init__(self, tables, edges) -> None:
        self.tables = {table.name: table for table in tables}
        self.edges = edges


@pytest.fixture()
def invoice_frame() -> dict[str, pd.DataFrame]:
    dates = pd.date_range("2013-01-01", "2016-04-01", freq="3D")
    invoices = pd.DataFrame(
        {
            "invoiceid": range(1, len(dates) + 1),
            "customerid": [i % 12 + 1 for i in range(len(dates))],
            "invoicedate": dates,
            "amount": [float((i % 7) * 10 + 5) for i in range(len(dates))],
        }
    )
    customers = pd.DataFrame(
        {
            "customerid": range(1, 13),
            "creditlimit": [float(100 * i) for i in range(1, 13)],
            "category": ["gold" if i % 2 else "basic" for i in range(1, 13)],
        }
    )
    return {"customers": customers, "invoices": invoices}


@pytest.fixture()
def model(invoice_frame) -> HostedKumoModel:
    graph = _Graph(
        [
            _Table(
                "customers",
                [("customerid", "ID"), ("creditlimit", "numerical"), ("category", "categorical")],
                "customerid",
            ),
            _Table(
                "invoices",
                [
                    ("invoiceid", "ID"),
                    ("customerid", "ID"),
                    ("invoicedate", "timestamp"),
                    ("amount", "numerical"),
                ],
                "invoiceid",
                "invoicedate",
            ),
        ],
        [_Edge("invoices", "customerid", "customers")],
    )
    config = HostedConfig(
        url="http://unused.invalid/predictions",
        api_key=None,
        timeout=5,
        history_anchors=4,
        max_instances=50,
        max_related_rows=10_000,
    )
    return HostedKumoModel(graph, invoice_frame, config)


def _build(model, pql):
    spec = model.validate_pql(pql)
    return model._payload(spec, indices=None, anchor_time=None)


def test_instances_are_one_row_per_entity_and_several_per_anchor(model):
    payload, plan = _build(
        model, "PREDICT COUNT(invoices.*, 0, 90, days) > 0 FOR EACH customers.customerid"
    )
    context = payload["context"]["instance_table"]
    predict = payload["predict"]["instance_table"]

    assert len(predict["rows"]) == plan["predict_instances"] == 12
    assert plan["context_instances"] > plan["predict_instances"]
    assert len(context["rows"]) == plan["context_instances"]
    # Instance ids restart at 0 for the predict batch: they are separate tables.
    assert predict["rows"][0][0] == 0


def test_related_rows_never_postdate_their_anchor(model):
    payload, _ = _build(
        model, "PREDICT COUNT(invoices.*, 0, 90, days) > 0 FOR EACH customers.customerid"
    )
    context = payload["context"]["instance_table"]
    invoices = payload["context"]["related_tables"]["invoices"]
    anchor_of = {row[0]: row[1] for row in context["rows"]}
    id_at = invoices["columns"].index("instance_id")
    date_at = invoices["columns"].index("invoicedate")

    leaked = [
        row
        for row in invoices["rows"]
        if row[date_at] is not None and row[date_at] > anchor_of[row[id_at]]
    ]
    assert leaked == []


def test_every_frame_declares_the_instance_key_and_the_endpoint_dtypes(model):
    payload, _ = _build(
        model, "PREDICT COUNT(invoices.*, 0, 90, days) > 0 FOR EACH customers.customerid"
    )
    schema = payload["schema"]

    assert schema["instance_table"]["primary_key"] == "instance_id"
    for frame in schema["related_tables"].values():
        assert "instance_id" in frame["columns"]
    assert schema["instance_table"]["columns"]["instance_id"]["dtype"] == "int64"
    assert schema["instance_table"]["columns"]["label"]["dtype"] == "bool"
    for relationship in schema["relationships"]:
        assert relationship["source_table"]


def test_timestamps_are_rfc3339_with_z(model):
    payload, _ = _build(
        model, "PREDICT COUNT(invoices.*, 0, 90, days) > 0 FOR EACH customers.customerid"
    )

    for row in payload["context"]["instance_table"]["rows"]:
        assert row[1].endswith("Z") and "T" in row[1]


def test_multiclass_drops_the_target_from_its_own_features(model):
    payload, _ = _build(model, "PREDICT customers.category FOR EACH customers.customerid")
    customers = payload["context"]["related_tables"]["customers"]

    assert payload["task"]["kind"] == "multiclass_classification"
    assert "category" not in customers["columns"]


def test_regression_declares_a_numeric_label_and_asks_for_no_probabilities(model):
    payload, _ = _build(
        model, "PREDICT SUM(invoices.amount, 0, 60, days) FOR EACH customers.customerid"
    )

    assert payload["schema"]["instance_table"]["columns"]["label"]["dtype"] == "float64"
    assert payload["task"]["target"]["dtype"] == "float64"
    assert payload["output"]["fields"] == ["prediction"]


def test_payload_is_json_clean(model):
    payload, _ = _build(
        model, "PREDICT COUNT(invoices.*, 0, 90, days) > 0 FOR EACH customers.customerid"
    )

    json.dumps(payload, allow_nan=False)  # raises on NaN/NaT


def test_link_and_forecast_are_refused_with_a_repair_message(model):
    for pql in (
        "PREDICT LIST_DISTINCT(invoices.invoiceid, 0, 90, days) RANK TOP 3 "
        "FOR EACH customers.customerid",
        "PREDICT SUM(invoices.amount, 0, 1, months) FORECAST 3 TIMEFRAMES "
        "FOR customers.customerid = 1",
    ):
        with pytest.raises(ValueError):
            model.validate_pql(pql)


def test_entity_without_a_route_to_the_event_table_is_refused(model):
    spec = model.validate_pql(
        "PREDICT COUNT(invoices.*, 0, 90, days) > 0 FOR EACH invoices.invoiceid"
    )
    # invoices is its own entity here: no path needed, the rows are the instances.
    payload, _ = model._payload(spec, indices=None, anchor_time=None)
    assert payload["task"]["entity_table_names"] == ["invoices"]


def test_predict_returns_the_engine_frame_shape(model, monkeypatch):
    responses = {
        "predictions": [
            {"row_index": 0, "prediction": True, "probabilities": {"false": 0.2, "true": 0.8}},
            {"row_index": 1, "prediction": False, "probabilities": {"false": 0.9, "true": 0.1}},
        ]
    }
    monkeypatch.setattr(model, "_post", lambda payload: responses)

    frame = model.predict(
        "PREDICT COUNT(invoices.*, 0, 90, days) > 0 FOR EACH customers.customerid"
    )

    assert list(frame.columns)[:2] == ["ENTITY", "anchor_timestamp"]
    assert "True_PROB" in frame.columns
    assert frame.loc[frame["ENTITY"] == 1, "True_PROB"].iloc[0] == pytest.approx(0.8)


def test_switch_reads_the_hosted_url_and_expands_the_public_alias(monkeypatch):
    monkeypatch.delenv("KUMO_HOSTED_API_URL", raising=False)
    assert hosted_url() is None
    assert hosted_enabled() is False

    monkeypatch.setenv("KUMO_HOSTED_API_URL", "public")
    assert hosted_url() == DEFAULT_HOSTED_URL
    assert hosted_enabled() is True

    monkeypatch.setenv("KUMO_HOSTED_API_URL", "https://gateway.example/predictions")
    assert hosted_url() == "https://gateway.example/predictions"


def test_config_caps_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("KUMO_HOSTED_API_URL", "public")
    monkeypatch.setenv("KUMO_HOSTED_API_KEY", "nvapi-test")
    monkeypatch.setenv("KUMO_HISTORY_ANCHORS", "3")
    monkeypatch.setenv("KUMO_MAX_INSTANCES", "17")

    config = HostedConfig.from_env()

    assert config is not None
    assert config.api_key == "nvapi-test"
    assert config.history_anchors == 3
    assert config.max_instances == 17


@pytest.fixture()
def two_hop_model() -> HostedKumoModel:
    """A detail table whose route to the entity is two hops, with no date of its own."""
    frames = {
        "customers": pd.DataFrame(
            {"customerid": [1, 2, 3], "creditlimit": [100.0, 200.0, 300.0]}
        ),
        "invoices": pd.DataFrame(
            {
                "invoiceid": [1, 2, 3, 4, 5, 6],
                "customerid": [1, 1, 2, 2, 3, 3],
                "invoicedate": pd.to_datetime(
                    ["2014-01-05", "2014-06-05", "2014-02-05",
                     "2014-08-05", "2014-03-05", "2014-09-05"]
                ),
            }
        ),
        "invoicelines": pd.DataFrame(
            {
                "invoicelineid": [1, 2, 3, 4, 5, 6],
                "invoiceid": [1, 2, 3, 4, 5, 6],
                "extendedprice": [10.0, 20.0, 30.0, 40.0, 50.0, 60.0],
            }
        ),
    }
    graph = _Graph(
        [
            _Table("customers", [("customerid", "ID"), ("creditlimit", "numerical")], "customerid"),
            _Table(
                "invoices",
                [("invoiceid", "ID"), ("customerid", "ID"), ("invoicedate", "timestamp")],
                "invoiceid",
                "invoicedate",
            ),
            _Table(
                "invoicelines",
                [("invoicelineid", "ID"), ("invoiceid", "ID"), ("extendedprice", "numerical")],
                "invoicelineid",
            ),
        ],
        [
            _Edge("invoices", "customerid", "customers"),
            _Edge("invoicelines", "invoiceid", "invoices"),
        ],
    )
    config = HostedConfig(
        url="http://unused.invalid/predictions",
        api_key=None,
        timeout=5,
        history_anchors=3,
        max_instances=50,
        max_related_rows=10_000,
    )
    return HostedKumoModel(graph, frames, config)


def test_a_two_hop_detail_table_is_attributed_through_its_parent(two_hop_model):
    """A line belongs to the customer that owns its invoice, not to a lookalike id.

    Reading the second hop off the key the first hop produced joined invoice ids to
    customer ids: almost every line fell out, and the few that matched were credited
    to the wrong customer.
    """
    payload, _ = _build(
        two_hop_model,
        "PREDICT SUM(invoicelines.extendedprice, 0, 30, days) FOR EACH customers.customerid",
    )

    instances = payload["context"]["instance_table"]
    entity_of = {row[0]: row[2] for row in instances["rows"]}
    owner_of = {1: 1, 2: 1, 3: 2, 4: 2, 5: 3, 6: 3}

    lines = payload["context"]["related_tables"]["invoicelines"]
    columns = lines["columns"]
    at = {name: columns.index(name) for name in ("instance_id", "invoiceid")}
    assert lines["rows"], "the detail table carried no rows at all"
    for row in lines["rows"]:
        assert entity_of[row[at["instance_id"]]] == owner_of[row[at["invoiceid"]]]

    labels = [row[-1] for row in instances["rows"]]
    assert any(label > 0 for label in labels), "no instance ever saw a line amount"


def test_a_declared_categorical_too_wide_for_the_endpoint_is_sent_as_text():
    """The endpoint refuses a categorical above 10k values; free text is a string."""
    from auto_ontology.retrieval.kumo.hosted import _column_spec

    wide = pd.Series([f"note {i}" for i in range(15_035)])
    narrow = pd.Series(["gold", "basic"] * 5)

    assert _column_spec(wide, "categorical")["stype"] == "text"
    assert _column_spec(narrow, "categorical")["stype"] == "categorical"


def test_a_customer_who_did_not_order_is_labelled_true_for_the_zero_case():
    """`= 0` must hold for a customer with no rows in the window at all.

    The windowed aggregate is a group-by, so an entity with nothing inside the
    window never appears in it. Reading that absence as "the comparison could
    not be evaluated" marked every quiet customer as active — the exact
    opposite of a churn label, and silently: the model only ever saw "nobody
    leaves", then predicted the prior.
    """
    dates = pd.date_range("2016-01-01", "2016-12-31", freq="3D")
    invoices = pd.DataFrame(
        {
            "invoiceid": range(1, len(dates) + 1),
            "customerid": [1] * len(dates),  # only customer 1 ever buys
            "invoicedate": dates,
            "amount": [10.0] * len(dates),
        }
    )
    customers = pd.DataFrame(
        {
            "customerid": [1, 2],
            "creditlimit": [100.0, 200.0],
            "category": ["gold", "basic"],
        }
    )
    graph = _Graph(
        [
            _Table(
                "customers",
                [("customerid", "ID"), ("creditlimit", "numerical"), ("category", "categorical")],
                "customerid",
            ),
            _Table(
                "invoices",
                [
                    ("invoiceid", "ID"),
                    ("customerid", "ID"),
                    ("invoicedate", "timestamp"),
                    ("amount", "numerical"),
                ],
                "invoiceid",
                "invoicedate",
            ),
        ],
        [_Edge("invoices", "customerid", "customers")],
    )
    model = HostedKumoModel(
        graph,
        {"customers": customers, "invoices": invoices},
        HostedConfig(
            url="http://unused.invalid/predictions",
            api_key=None,
            timeout=5,
            history_anchors=4,
            max_instances=50,
            max_related_rows=10_000,
        ),
    )

    payload, _ = _build(
        model,
        "PREDICT COUNT(invoices.*, 0, 90, days) = 0 FOR EACH customers.customerid",
    )

    table = payload["context"]["instance_table"]
    key_at = table["columns"].index("customerid")
    label_at = table["columns"].index("label")
    seen: dict[object, set] = {}
    for row in table["rows"]:
        seen.setdefault(row[key_at], set()).add(row[label_at])

    assert seen[1] and not any(seen[1]), "an ordering customer must not be labelled churned"
    assert seen[2] and all(seen[2]), "a silent customer must be labelled churned"


def test_lookback_keeps_only_the_history_near_each_anchor(invoice_frame):
    """A capped lookback trims rows that are older than the window asks for.

    Nothing about the anchor or the label changes — only how far behind the
    anchor the related rows may reach, which is the difference between a 398 MB
    upload and a request the endpoint will accept.
    """
    graph = _Graph(
        [
            _Table(
                "customers",
                [("customerid", "ID"), ("creditlimit", "numerical"), ("category", "categorical")],
                "customerid",
            ),
            _Table(
                "invoices",
                [
                    ("invoiceid", "ID"),
                    ("customerid", "ID"),
                    ("invoicedate", "timestamp"),
                    ("amount", "numerical"),
                ],
                "invoiceid",
                "invoicedate",
            ),
        ],
        [_Edge("invoices", "customerid", "customers")],
    )
    model = HostedKumoModel(
        graph,
        invoice_frame,
        HostedConfig(
            url="http://unused.invalid/predictions",
            api_key=None,
            timeout=5,
            history_anchors=4,
            max_instances=50,
            max_related_rows=10_000,
            lookback_days=365,
        ),
    )

    payload, _ = _build(
        model,
        "PREDICT COUNT(invoices.*, 0, 90, days) > 0 FOR EACH customers.customerid",
    )

    instances = payload["context"]["instance_table"]
    id_at = instances["columns"].index("instance_id")
    anchor_at = instances["columns"].index("anchor_time")

    def naive(value):
        stamp = pd.Timestamp(value)
        return stamp.tz_convert(None) if stamp.tz is not None else stamp

    anchor_of = {row[id_at]: naive(row[anchor_at]) for row in instances["rows"]}

    related = payload["context"]["related_tables"]["invoices"]
    columns = related["columns"]
    instance_at = columns.index("instance_id")
    date_at = columns.index("invoicedate")
    assert related["rows"], "the lookback discarded everything"
    for row in related["rows"]:
        anchor = anchor_of[row[instance_at]]
        assert anchor - pd.Timedelta(days=365) <= naive(row[date_at])
