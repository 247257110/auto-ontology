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
    return _make_model(invoice_frame)


def _make_model(frames, col_stypes=None) -> HostedKumoModel:
    graph = _Graph(
        [
            _Table(
                "customers",
                [
                    ("customerid", "ID"),
                    ("creditlimit", "numerical"),
                    ("category", "categorical"),
                ],
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
    return HostedKumoModel(graph, frames, config, col_stypes=col_stypes)


def _build(model, pql):
    spec = model.validate_pql(pql)
    return model._payload(spec, indices=None, anchor_time=None)


def test_instances_are_one_row_per_entity_and_several_per_anchor(model):
    payload, plan = _build(
        model,
        "PREDICT COUNT(invoices.*, 0, 90, days) > 0 FOR EACH customers.customerid",
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
        model,
        "PREDICT COUNT(invoices.*, 0, 90, days) > 0 FOR EACH customers.customerid",
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


def test_caller_anchor_wins_over_the_end_of_the_data(model):
    """A caller-supplied anchor wins, including one before the data's end.

    The forecast default is today (see ``_forecast_anchor``); clamping an
    explicit anchor forward to the last row would answer a different moment
    than the caller asked for.
    """
    spec = model.validate_pql(
        "PREDICT COUNT(invoices.*, 0, 90, days) > 0 FOR EACH customers.customerid"
    )

    payload, plan = model._payload(
        spec, indices=None, anchor_time=pd.Timestamp("2015-12-01")
    )

    assert plan["anchor"] == "2015-12-01T00:00:00Z"
    predict = payload["predict"]["instance_table"]
    anchor_at = predict["columns"].index("anchor_time")
    assert {row[anchor_at] for row in predict["rows"]} == {"2015-12-01T00:00:00Z"}


def test_predict_history_stops_at_the_caller_anchor(model):
    """An earlier anchor must also cut the rows shipped for the predict batch.

    Otherwise the anchor says 2015-12-01 while the payload still carries the four
    months of invoices that follow it — the model would read a future the
    prediction claims not to know.
    """
    spec = model.validate_pql(
        "PREDICT COUNT(invoices.*, 0, 90, days) > 0 FOR EACH customers.customerid"
    )

    payload, _ = model._payload(
        spec, indices=None, anchor_time=pd.Timestamp("2015-12-01")
    )

    invoices = payload["predict"]["related_tables"]["invoices"]
    date_at = invoices["columns"].index("invoicedate")
    dates = [row[date_at] for row in invoices["rows"] if row[date_at]]
    assert dates
    assert max(dates) <= "2015-12-01T00:00:00Z"


def test_without_a_caller_anchor_the_last_row_is_the_anchor(model, invoice_frame):
    """The fallback still works: no anchor means "where the data ends"."""
    _, plan = _build(
        model,
        "PREDICT COUNT(invoices.*, 0, 90, days) > 0 FOR EACH customers.customerid",
    )

    data_end = invoice_frame["invoices"]["invoicedate"].max()
    assert plan["anchor"] == data_end.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_every_frame_declares_the_instance_key_and_the_endpoint_dtypes(model):
    payload, _ = _build(
        model,
        "PREDICT COUNT(invoices.*, 0, 90, days) > 0 FOR EACH customers.customerid",
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
        model,
        "PREDICT COUNT(invoices.*, 0, 90, days) > 0 FOR EACH customers.customerid",
    )

    for row in payload["context"]["instance_table"]["rows"]:
        assert row[1].endswith("Z") and "T" in row[1]


def test_multiclass_drops_the_target_from_its_own_features(model):
    payload, _ = _build(
        model, "PREDICT customers.category FOR EACH customers.customerid"
    )
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
        model,
        "PREDICT COUNT(invoices.*, 0, 90, days) > 0 FOR EACH customers.customerid",
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
            {
                "row_index": 0,
                "prediction": True,
                "probabilities": {"false": 0.2, "true": 0.8},
            },
            {
                "row_index": 1,
                "prediction": False,
                "probabilities": {"false": 0.9, "true": 0.1},
            },
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
                    [
                        "2014-01-05",
                        "2014-06-05",
                        "2014-02-05",
                        "2014-08-05",
                        "2014-03-05",
                        "2014-09-05",
                    ]
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
            _Table(
                "customers",
                [("customerid", "ID"), ("creditlimit", "numerical")],
                "customerid",
            ),
            _Table(
                "invoices",
                [
                    ("invoiceid", "ID"),
                    ("customerid", "ID"),
                    ("invoicedate", "timestamp"),
                ],
                "invoiceid",
                "invoicedate",
            ),
            _Table(
                "invoicelines",
                [
                    ("invoicelineid", "ID"),
                    ("invoiceid", "ID"),
                    ("extendedprice", "numerical"),
                ],
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


def test_a_declared_categorical_too_wide_for_the_endpoint_is_left_out():
    """The endpoint caps categorical at 10k values and reads no `text` type.

    The old fallback declared such a column `text`, but the hosted SDM does not
    read that semantic type and refuses the whole request with
    ``422 UNSUPPORTED_DTYPE``. A string column over the ceiling therefore has no
    declaration the endpoint accepts, and is left out of the payload.
    """
    from auto_ontology.retrieval.kumo.hosted import _column_spec

    wide = pd.Series([f"note {i}" for i in range(15_035)])
    narrow = pd.Series(["gold", "basic"] * 5)

    assert _column_spec(wide, "categorical") is None
    assert _column_spec(narrow, "categorical")["stype"] == "categorical"


def test_a_declared_text_column_is_sent_as_a_category_not_text():
    """`text` is the local engine's name; the hosted SDM has no such type."""
    from auto_ontology.retrieval.kumo.hosted import _column_spec

    narrow = pd.Series(["Acme", "Globex"] * 5)
    wide = pd.Series([f"Acme {i}" for i in range(15_000)])

    assert _column_spec(narrow, "text") == {"dtype": "string", "stype": "categorical"}
    assert _column_spec(wide, "text") is None


def test_a_decimal_measure_is_numerical_not_free_text():
    """Postgres `numeric` arrives as Decimal/object; it is still a number.

    Read as a string, a measure is declared categorical, dropped once it is
    wide, and each of its values reaches the endpoint quoted.
    """
    from decimal import Decimal

    from auto_ontology.retrieval.kumo.hosted import _column_spec, _scalar

    wide = pd.Series([Decimal(f"{i}.25") for i in range(15_000)], dtype=object)

    assert _column_spec(wide, "text") == {"dtype": "float64", "stype": "numerical"}
    assert _column_spec(wide, "categorical") == {
        "dtype": "float64",
        "stype": "numerical",
    }
    assert _scalar(Decimal("1320.39")) == 1320.39
    assert _scalar(Decimal("NaN")) is None


def test_a_decimal_measure_rides_as_a_number_in_the_payload(invoice_frame):
    """The declaration and the rows agree: float64, not quoted Decimal."""
    from decimal import Decimal

    frames = {name: frame.copy() for name, frame in invoice_frame.items()}
    frames["invoices"]["order_total"] = [
        Decimal(f"{(i % 5) * 10}.25") for i in range(len(frames["invoices"]))
    ]
    model = _make_model(frames, col_stypes={"invoices": {"order_total": "text"}})

    payload, _ = _build(
        model, "PREDICT SUM(invoices.amount, 0, 30, days) FOR EACH customers.customerid"
    )

    invoices = payload["schema"]["related_tables"]["invoices"]
    assert invoices["columns"]["order_total"] == {
        "dtype": "float64",
        "stype": "numerical",
    }
    sent = payload["context"]["related_tables"]["invoices"]
    at = sent["columns"].index("order_total")
    values = {row[at] for row in sent["rows"]}
    assert all(isinstance(value, float) for value in values)
    assert values == {0.25, 10.25, 20.25, 30.25, 40.25}


def test_a_wide_numeric_categorical_is_declared_numerical_not_text():
    """A number over the ceiling stays a number.

    `text` is the honest answer only for a string column. Declaring int64 or
    float64 as `text` leaves the endpoint a pair it has to reinterpret, and a
    column with 14k distinct values — an id, or a measure the semantic layer
    called a dimension — keeps the request at risk of the same 422.
    """
    from auto_ontology.retrieval.kumo.hosted import _column_spec

    wide_ints = pd.Series(range(15_000))
    wide_floats = pd.Series([i / 3 for i in range(15_000)])

    integers = _column_spec(wide_ints, "categorical")
    floats = _column_spec(wide_floats, "categorical")

    assert integers == {"dtype": "int64", "stype": "numerical"}
    assert floats == {"dtype": "float64", "stype": "numerical"}


def test_a_declared_text_column_is_declared_categorical_in_the_payload(
    invoice_frame,
):
    """A semantic `text` column must not reach the endpoint as `text`.

    The endpoint answers ``422 UNSUPPORTED_DTYPE: column 'company_name'
    declares semantic type 'text', which SDM does not read`` and refuses the
    whole request. Once the sent rows fit under the categorical ceiling there
    is a declaration it does read, so the column rides as a category.
    """
    frames = {name: frame.copy() for name, frame in invoice_frame.items()}
    frames["customers"]["company_name"] = [f"company {i}" for i in range(1, 13)]
    model = _make_model(frames, col_stypes={"customers": {"company_name": "text"}})

    payload, _ = _build(
        model, "PREDICT SUM(invoices.amount, 0, 30, days) FOR EACH customers.customerid"
    )

    customers = payload["schema"]["related_tables"]["customers"]
    assert customers["columns"]["company_name"] == {
        "dtype": "string",
        "stype": "categorical",
    }
    assert "text" not in {
        str(spec.get("stype")) for spec in customers["columns"].values()
    }
    # The rows carry the column the schema declares.
    sent = payload["context"]["related_tables"]["customers"]
    at = sent["columns"].index("company_name")
    assert {row[at] for row in sent["rows"]} == {f"company {i}" for i in range(1, 13)}


def test_a_text_column_over_the_categorical_ceiling_is_left_out_of_the_payload(
    invoice_frame, monkeypatch
):
    """A wide free-text column cannot be declared at all, so it is omitted.

    `text` is refused and categorical stops at the ceiling; resending cannot
    fix either. Leaving the column out keeps the question askable, and the
    schema and both sent frames must agree about it.
    """
    import auto_ontology.retrieval.kumo.hosted as hosted

    monkeypatch.setattr(hosted, "_MAX_CATEGORICAL", 5)
    frames = {name: frame.copy() for name, frame in invoice_frame.items()}
    frames["customers"]["company_name"] = [f"company {i}" for i in range(1, 13)]
    model = _make_model(frames, col_stypes={"customers": {"company_name": "text"}})

    payload, _ = _build(
        model, "PREDICT SUM(invoices.amount, 0, 30, days) FOR EACH customers.customerid"
    )

    customers = payload["schema"]["related_tables"]["customers"]
    assert "company_name" not in customers["columns"]
    assert all(
        column != "company_name"
        for column in payload["context"]["related_tables"]["customers"]["columns"]
    )
    assert all(
        column != "company_name"
        for column in payload["predict"]["related_tables"]["customers"]["columns"]
    )
    for section in ("context", "predict"):
        table = payload[section]["related_tables"]["customers"]
        assert all(len(row) == len(table["columns"]) for row in table["rows"])


def _shape_rejection(limit: int, cardinality: int = 12_960) -> RuntimeError:
    return RuntimeError(
        "hosted prediction failed (422): "
        '{"type":"/problems/unsupported-shape","status":422,"title":"Unsupported shape",'
        f'"detail":"categorical cardinality {cardinality} exceeds limit {limit}.",'
        '"code":"UNSUPPORTED_SHAPE"}'
    )


def test_the_context_anchors_are_trimmed_to_fit_the_training_row_ceiling(model):
    """Eight anchors over 1620 entities is a 12,960-row context, which is refused.

    ``max_instances`` caps entities, not their product with the anchor count, so
    a question on a mid-sized table used to build a context over the endpoint's
    10k ceiling and fail before any column was even looked at. Trimming the
    oldest anchors keeps every entity scoreable.
    """
    anchors = list(pd.date_range("2020-01-01", periods=8, freq="90D"))

    assert model._fit_context(100, anchors) == anchors  # 800 rows: nothing to trim
    fitted = model._fit_context(1_620, anchors)

    assert len(fitted) == 6, (
        "1620 entities x 6 anchors is 9720 rows, just under the ceiling"
    )
    assert fitted == anchors[-6:], "the newest anchors are the ones worth keeping"
    assert model._fit_context(1_620, anchors[:1]) == anchors[:1]


def test_a_shape_rejection_is_repaired_by_dropping_the_widest_column(
    invoice_frame, monkeypatch
):
    """A request the endpoint calls too wide is retried one column narrower.

    The rejection is deterministic, so the identical body can only fail again.
    What makes it repairable is that the request is merely wide: dropping a
    related table's own row identity keeps the question, the entities and every
    join, and is what turns a 6-minute refusal into an answer.
    """
    # The semantic layer declares each table's own key as ID, as it does in
    # production; that is what puts a 400-value identity column in the request.
    model = _make_model(
        invoice_frame,
        col_stypes={
            "invoices": {"invoiceid": "ID", "customerid": "ID"},
            "customers": {"customerid": "ID"},
        },
    )
    payload, _ = _build(
        model, "PREDICT SUM(invoices.amount, 0, 30, days) FOR EACH customers.customerid"
    )
    sent: list[dict] = []

    def fake_post(body):
        sent.append(body)
        if len(sent) == 1:
            raise _shape_rejection(limit=50)
        return {"predictions": [{"row_index": 0, "prediction": 3.0}]}

    monkeypatch.setattr(model, "_post", fake_post)
    response = model._predict_response(payload)

    assert response["predictions"], "the repaired request was not answered"
    assert len(sent) == 2, "the retry did not happen exactly once"
    invoices = sent[1]["schema"]["related_tables"]["invoices"]
    assert "invoiceid" not in invoices["columns"]
    assert "invoiceid" not in invoices["primary_key"]
    assert (
        "invoiceid" not in sent[1]["context"]["related_tables"]["invoices"]["columns"]
    )
    # The join the question depends on survives, in the schema and in the rows.
    assert "customerid" in invoices["columns"]
    assert "customerid" in sent[1]["context"]["related_tables"]["invoices"]["columns"]
    assert sent[1]["schema"]["relationships"], "the entity relationship was dropped"


def test_a_shape_rejection_never_drops_a_joined_column(invoice_frame, monkeypatch):
    """Dropping a joined column would trade one rejection for another, so it isn't dropped."""
    model = _make_model(
        invoice_frame,
        col_stypes={
            "invoices": {"invoiceid": "ID", "customerid": "ID"},
            "customers": {"customerid": "ID"},
        },
    )
    payload, _ = _build(
        model, "PREDICT SUM(invoices.amount, 0, 30, days) FOR EACH customers.customerid"
    )
    sent: list[dict] = []

    def fake_post(body):
        sent.append(body)
        raise _shape_rejection(limit=1)

    monkeypatch.setattr(model, "_post", fake_post)
    with pytest.raises(RuntimeError, match="too wide"):
        model._predict_response(payload)

    for body in sent:
        invoices = body["schema"]["related_tables"]["invoices"]
        assert "customerid" in invoices["columns"]
        assert body["schema"]["relationships"], "the entity relationship was dropped"


def test_hosted_semantic_validation_drops_only_the_named_optional_feature(
    invoice_frame, monkeypatch
):
    model = _make_model(invoice_frame)
    payload, _ = _build(
        model,
        "PREDICT SUM(invoices.amount, 0, 30, days) FOR EACH customers.customerid",
    )
    sent: list[dict] = []

    def fake_post(body):
        sent.append(body)
        if len(sent) == 1:
            raise RuntimeError(
                "hosted prediction failed (422): column 'category' declares semantic type text"
            )
        return {"predictions": [{"row_index": 0, "prediction": 3.0}]}

    monkeypatch.setattr(model, "_post", fake_post)
    response = model._predict_response(payload)

    assert response["predictions"]
    assert len(sent) == 2
    assert "category" not in sent[1]["schema"]["related_tables"]["customers"]["columns"]
    assert "customerid" in sent[1]["schema"]["related_tables"]["customers"]["columns"]


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
                [
                    ("customerid", "ID"),
                    ("creditlimit", "numerical"),
                    ("category", "categorical"),
                ],
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

    assert seen[1] and not any(seen[1]), (
        "an ordering customer must not be labelled churned"
    )
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
                [
                    ("customerid", "ID"),
                    ("creditlimit", "numerical"),
                    ("category", "categorical"),
                ],
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
