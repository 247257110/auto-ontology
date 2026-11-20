# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""A Term that spans several tables is what a *concept* dimension needs.

Compilation allocates one Term per table, so "客户地域" — four joined tables and
no single owner — could not exist at all before this. Every read filters on a
Term having at least one represented table (see ``_represented``), so a Term
created without links would be invisible to the glossary and to retrieval.

These tests use the same ``world`` fixture as the rest of the DAL suite and skip
when ``POSTGRES_*`` is unset.
"""

from __future__ import annotations

import os

import pytest

from auto_ontology.dal import terms as t

# The fixture lives with the rest of the DAL Term tests. Importing it *as the
# same name* puts the fixture object in this module's globals, which is how
# pytest discovers fixtures, and is the form ruff accepts without a noqa.
from auto_ontology.dal.tests.test_terms import world as world  # noqa: F401


pytestmark = pytest.mark.skipif(
    not os.environ.get("POSTGRES_USER"), reason="POSTGRES_* not set"
)


@pytest.fixture
def concept(world) -> str:
    """The Term name, scoped to the throwaway catalog this test created.

    The fixture's teardown deletes terms by name prefix, so an unprefixed name
    would survive into the next run and make the create fail on a duplicate.
    """
    return f"{world.prefix}-客户地域"


def test_a_term_can_represent_several_tables(world, concept) -> None:
    world.table("customers", columns=("id",))
    world.table("cities", columns=("id",))
    world.table("stateprovinces", columns=("id",))

    row = t.create_term(
        name=concept,
        description="按客户所在州省统计的销售区域",
        synonyms=["地域", "地区"],
        table_ids=[
            world.tables["customers"],
            world.tables["cities"],
            world.tables["stateprovinces"],
        ],
    )

    assert row["name"] == concept
    assert row["table_count"] == 3

    # Retrievable at all: every read of the visible Term set requires a table
    # (see `_represented`), which is exactly what a concept Term has several of.
    names = [term["name"] for term in t.fetch_all_terms()]
    assert concept in names


def test_the_created_term_carries_its_synonyms_into_the_glossary(
    world, concept
) -> None:
    world.table("customers", columns=("id",))
    t.create_term(
        name=concept,
        description="d",
        synonyms=["地域", "地区"],
        table_ids=[world.tables["customers"]],
    )

    rows = {row["name"]: row for row in t.fetch_all_terms()}
    assert rows[concept]["synonyms"] == ["地域", "地区"]


def test_a_duplicate_name_is_refused_not_merged(world, concept) -> None:
    """Two Terms with one name cannot both be embedded."""
    world.table("customers", columns=("id",))
    t.create_term(
        name=concept, description="first", table_ids=[world.tables["customers"]]
    )

    with pytest.raises(t.TermNameConflict):
        t.create_term(
            name=concept, description="second", table_ids=[world.tables["customers"]]
        )


def test_the_existing_term_survives_a_failed_recreate(world, concept) -> None:
    world.table("customers", columns=("id",))
    tables = [world.tables["customers"]]
    t.create_term(name=concept, description="first", table_ids=tables)

    with pytest.raises(t.TermNameConflict):
        t.create_term(name=concept, description="second", table_ids=tables)

    rows = {row["name"]: row for row in t.fetch_all_terms()}
    assert rows[concept]["description"] == "first"


def test_an_unknown_table_id_is_refused(world) -> None:
    """A link that silently does not happen leaves the Term invisible."""
    world.table("customers", columns=("id",))

    with pytest.raises(ValueError, match="Unknown table id"):
        t.create_term(
            name="t-unknown-table-case",
            description="d",
            table_ids=[world.tables["customers"], "00000000-0000-0000-0000-0"],
        )


def test_a_blank_name_is_refused(world) -> None:
    world.table("customers", columns=("id",))

    with pytest.raises(ValueError, match="cannot be blank"):
        t.create_term(
            name="   ", description="d", table_ids=[world.tables["customers"]]
        )


def test_a_term_with_no_table_is_refused(world, concept) -> None:
    """It would be created, succeed, and then be invisible to every read."""
    with pytest.raises(ValueError, match="at least one table"):
        t.create_term(name=concept, description="d", table_ids=[])


def test_certification_flags_are_stored(world, concept) -> None:
    world.table("customers", columns=("id",))
    t.create_term(
        name=concept,
        description="d",
        table_ids=[world.tables["customers"]],
        name_certified=True,
        description_certified=True,
    )

    rows = {row["name"]: row for row in t.fetch_all_terms()}
    assert rows[concept]["name_certified"] is True
    assert rows[concept]["description_certified"] is True
