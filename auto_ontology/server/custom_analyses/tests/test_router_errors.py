# SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unresolvable SQL reaches the caller as 422, not as a 500.

`CustomAnalysisSqlError` subclasses `SqlParseError`, but the validator raises the
*base* class. Catching only the subclass let the single most common failure of
these endpoints — "SQL doesn't reference any table known to the catalog" — escape
as an unhandled 500, from the endpoint whose entire job is to report it. The
frontend's Validate button had no message to show and printed "Internal Server
Error".

These tests go through the route handlers rather than the service, because the
handlers are where the wrong class was caught.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import HTTPException

from auto_ontology.server.datasources import router as datasources_router
from auto_ontology.server.sql_utils import SqlParseError


class _Body:
    def __init__(self, sql: str, name: str = "an analysis") -> None:
        self.sql = sql
        self.name = name
        self.description = ""


@pytest.fixture()
def unresolvable() -> SqlParseError:
    return SqlParseError(
        "SQL doesn't reference any table known to the catalog; ingest the schema "
        "first or check the query"
    )


def test_validate_reports_unresolvable_sql_as_422(unresolvable: SqlParseError) -> None:
    with patch.object(
        datasources_router.custom_analyses_dal,
        "validate_custom_analysis_sql",
        side_effect=unresolvable,
    ):
        with pytest.raises(HTTPException) as caught:
            datasources_router.validate_custom_analysis_sql(
                _Body("SELECT nope FROM nothing")
            )

    # The message the UI can show is the point of the 422.
    assert caught.value.status_code == 422
    assert "known to the catalog" in str(caught.value.detail)


def test_create_reports_unresolvable_sql_as_422(unresolvable: SqlParseError) -> None:
    with patch.object(
        datasources_router.custom_analyses_dal,
        "create_custom_analysis",
        side_effect=unresolvable,
    ):
        with pytest.raises(HTTPException) as caught:
            datasources_router.create_custom_analysis(_Body("SELECT nope FROM nothing"))

    assert caught.value.status_code == 422


def test_update_reports_unresolvable_sql_as_422(unresolvable: SqlParseError) -> None:
    with patch.object(
        datasources_router.custom_analyses_dal,
        "update_custom_analysis",
        side_effect=unresolvable,
    ):
        with pytest.raises(HTTPException) as caught:
            datasources_router.update_custom_analysis("analysis-1", _Body("SELECT 1"))

    assert caught.value.status_code == 422


def test_the_subclass_is_still_reported_as_422(unresolvable: SqlParseError) -> None:
    """Narrowing the catch to the base class must not lose the subclass."""
    from auto_ontology.dal.custom_analyses import CustomAnalysisSqlError

    assert issubclass(CustomAnalysisSqlError, SqlParseError)

    with patch.object(
        datasources_router.custom_analyses_dal,
        "create_custom_analysis",
        side_effect=CustomAnalysisSqlError("duplicate snippet"),
    ):
        with pytest.raises(HTTPException) as caught:
            datasources_router.create_custom_analysis(_Body("SELECT 1"))

    assert caught.value.status_code == 422


def test_a_name_conflict_is_still_409() -> None:
    """Widening one branch must not swallow the branch beside it."""
    from auto_ontology.dal.custom_analyses import CustomAnalysisNameConflict

    with patch.object(
        datasources_router.custom_analyses_dal,
        "create_custom_analysis",
        side_effect=CustomAnalysisNameConflict("taken"),
    ):
        with pytest.raises(HTTPException) as caught:
            datasources_router.create_custom_analysis(_Body("SELECT 1"))

    assert caught.value.status_code == 409
