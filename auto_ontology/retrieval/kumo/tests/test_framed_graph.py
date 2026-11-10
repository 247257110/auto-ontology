# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The link fallback used when the catalog brings no join paths.

The hosted backend cannot lean on the engine's ``infer_links``, so the graph has
to recover its own edges. These tests pin how strict that recovery is: a shared
column name is never enough on its own.
"""

from __future__ import annotations

import pandas as pd
import pytest

from auto_ontology.retrieval.kumo.framed_graph import (
    build_framed_graph,
    infer_links_by_name,
)


@pytest.fixture()
def frames() -> dict[str, pd.DataFrame]:
    """The shape the warehouse actually has: invoice lines hang off invoices."""
    return {
        "customers": pd.DataFrame(
            {
                "customerid": range(1, 13),
                "customername": [f"customer {i}" for i in range(1, 13)],
            }
        ),
        "invoices": pd.DataFrame(
            {
                "invoiceid": range(1, 41),
                "customerid": [i % 12 + 1 for i in range(40)],
                "invoicedate": pd.date_range("2013-01-01", periods=40, freq="30D"),
                "amount": [float(i * 10 + 5) for i in range(40)],
            }
        ),
        "invoicelines": pd.DataFrame(
            {
                "invoicelineid": range(1, 121),
                "invoiceid": [i % 40 + 1 for i in range(120)],
                "quantity": [i % 9 + 1 for i in range(120)],
            }
        ),
    }


_KEYS = {
    "customers": ["customerid"],
    "invoices": ["invoiceid"],
    "invoicelines": ["invoicelineid"],
}


def test_a_foreign_key_named_after_the_parent_key_still_links(frames):
    """The common warehouse convention must not be mistaken for a self-key."""
    graph = build_framed_graph(frames, _KEYS)

    added = infer_links_by_name(graph, frames)

    assert added == 2
    assert ("invoices", "customerid", "customers") in {
        (e.src_table, e.fkey, e.dst_table) for e in graph.edges
    }
    assert ("invoicelines", "invoiceid", "invoices") in {
        (e.src_table, e.fkey, e.dst_table) for e in graph.edges
    }


def test_two_tables_keyed_by_their_own_id_are_not_related(frames):
    """Two independently numbered tables share a name but nothing else."""
    frames["products"] = pd.DataFrame(
        {"id": range(1, 13), "productname": [f"product {i}" for i in range(1, 13)]}
    )
    frames["categories"] = pd.DataFrame(
        {"id": range(1, 13), "categoryname": [f"category {i}" for i in range(1, 13)]}
    )
    graph = build_framed_graph(
        frames, {**_KEYS, "products": ["id"], "categories": ["id"]}
    )

    infer_links_by_name(graph, frames)

    assert not [
        edge
        for edge in graph.edges
        if edge.dst_table in {"products", "categories"}
    ]


def test_a_same_named_column_whose_values_never_meet_the_key_is_ignored(frames):
    """``regionid`` is a key; an unrelated ``customerid`` must not borrow it."""
    frames["regions"] = pd.DataFrame(
        {
            "regionid": range(1, 13),
            "customerid": [900 + i for i in range(12)],
        }
    )
    graph = build_framed_graph(frames, {**_KEYS, "regions": ["regionid"]})

    infer_links_by_name(graph, frames)

    assert not [edge for edge in graph.edges if edge.dst_table == "regions"]


def test_a_composite_parent_key_is_never_a_destination(frames):
    """A two-column key cannot be spelled by one foreign key column."""
    graph = build_framed_graph(
        frames, {**_KEYS, "invoicelines": ["invoiceid", "invoicelineid"]}
    )

    infer_links_by_name(graph, frames)

    assert not [edge for edge in graph.edges if edge.dst_table == "invoicelines"]


def test_declared_edges_are_not_duplicated(frames):
    """Running the fallback over a graph the catalog already wired adds nothing."""
    graph = build_framed_graph(frames, _KEYS)
    graph.link("invoices", "customerid", "customers")

    added = infer_links_by_name(graph, frames)

    assert added == 1
    assert len([e for e in graph.edges if e.dst_table == "customers"]) == 1


def test_a_table_whose_values_are_all_missing_is_not_linked(frames):
    """An all-null foreign key carries no attribution and must be left alone."""
    frames["invoices"] = frames["invoices"].assign(customerid=None)
    graph = build_framed_graph(frames, _KEYS)

    infer_links_by_name(graph, frames)

    assert not [edge for edge in graph.edges if edge.dst_table == "customers"]


def test_one_foreign_key_reaches_one_destination(frames):
    """A live and an archived copy of a dimension are both plausible parents."""
    frames["customers_archive"] = frames["customers"].copy()
    graph = build_framed_graph(frames, {**_KEYS, "customers_archive": ["customerid"]})

    infer_links_by_name(graph, frames)

    assert len([e for e in graph.edges if e.fkey == "customerid"]) == 1
