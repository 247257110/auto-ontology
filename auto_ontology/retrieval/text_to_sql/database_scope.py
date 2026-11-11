# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Which database a chat turn is allowed to read.

Three scopes live here, and conflating them is what makes a question about
customer revenue come back with ``retail_churn`` columns:

* **Explicit scope** — the caller named a database (``target_db`` on the chat
  request). It always wins: every search is filtered to it and the candidate
  vote never runs.
* **Default scope** — nobody named one, and the deployment wants SQL questions
  to land on one database instead of on whichever database wins the vote. A
  vote is the wrong instrument for this: two libraries that model the same
  business (a demo warehouse plus a purpose-built churn library) produce almost
  the same hits, and the tie-breakers are hit count and then database *name*.
* **Prediction scope** — a forecast belongs to the library it was trained and
  scored against, not to whichever database has more columns. Left to the vote,
  a churn question lands on the warehouse (more hits, no churn signal) and then
  dies resolving a table that only exists in the other library.

Both pins are deliberately **soft**: a question that names a *different*
configured database is left to the vote, so "retail_churn 里 2024 年有多少客户"
still lands where it says without asking the caller for a parameter the chat UI
does not expose.

Configured with ``CHAT_DEFAULT_TARGET_DB`` and ``CHAT_PREDICTION_TARGET_DB``.
Leaving either unset keeps the old behaviour for that branch: no pin, the vote
decides.
"""

from __future__ import annotations

import logging

import os

from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)

# Env knobs, read per call so a restart is the only thing needed to change them.
ENV_DEFAULT_TARGET_DB = "CHAT_DEFAULT_TARGET_DB"
ENV_PREDICTION_TARGET_DB = "CHAT_PREDICTION_TARGET_DB"

# ``path_state["question_type"]`` value routed to the PQL branch.
_PREDICTION = "prediction"

# Scope decisions, also used as the log reason.
SCOPE_REQUESTED = "requested"
SCOPE_DEFAULT = "default"
SCOPE_PREDICTION_DEFAULT = "prediction-default"
SCOPE_NONE = "none"
SKIP_PREDICTION = "prediction-skipped:no-prediction-database"
SKIP_NAMES_OTHER_DB = "skipped:question-names-other-database"
SKIP_UNKNOWN_DB = "skipped:not-a-configured-database"


def _env_target_db(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def configured_default_target_db() -> str | None:
    """The deployment's default database, or ``None`` when unset."""
    return _env_target_db(ENV_DEFAULT_TARGET_DB)


def configured_prediction_target_db() -> str | None:
    """The database prediction turns are scoped to, or ``None`` when unset."""
    return _env_target_db(ENV_PREDICTION_TARGET_DB)


def _database_names(connectors: Iterable[Any]) -> list[str]:
    """The ``database_name`` of every connector, in configured order."""
    names: list[str] = []
    for connector in connectors or ():
        name = getattr(connector, "database_name", None)
        if name and str(name).strip():
            names.append(str(name).strip())
    return names


def _canonical(name: str, names: list[str]) -> str | None:
    """The configured spelling of *name*, matched exactly then case-insensitively."""
    if name in names:
        return name
    folded = [
        candidate for candidate in names if candidate.casefold() == name.casefold()
    ]
    return folded[0] if len(folded) == 1 else None


def _other_database_named(
    question: str, names: list[str], *, excluding: str
) -> str | None:
    """The first configured database other than *excluding* that the question names."""
    folded_question = (question or "").casefold()
    if not folded_question:
        return None
    for name in names:
        if name.casefold() == excluding.casefold():
            continue
        if name.casefold() in folded_question:
            return name
    return None


def _pin(
    *,
    setting: str,
    raw: str,
    reason: str,
    question: str,
    names: list[str],
) -> tuple[str | None, str]:
    """Resolve one configured pin, or fall back to the vote with a reason."""
    canonical = _canonical(raw, names)
    if canonical is None:
        # A pin that no connector serves would either be silently ignored by
        # the search (wrong answer, no explanation) or raise downstream.
        # Neither is acceptable in a request path, so fall back to the vote.
        logger.warning(
            "Ignoring %s=%r: no configured connector serves that database. "
            "Available: %s",
            setting,
            raw,
            ", ".join(sorted(names)) or "(none)",
        )
        return None, SKIP_UNKNOWN_DB

    if _other_database_named(question, names, excluding=canonical) is not None:
        return None, SKIP_NAMES_OTHER_DB

    return canonical, reason


def resolve_effective_target_db(
    *,
    requested: str | None,
    question_type: str | None,
    question: str,
    connectors: Iterable[Any],
    default: str | None,
    prediction_default: str | None = None,
) -> tuple[str | None, str]:
    """Decide the database this turn is scoped to.

    Returns ``(target_db, reason)`` where *reason* is one of the ``SCOPE_*`` /
    ``SKIP_*`` constants — the caller logs it, so an operator can tell "the
    question was pinned" from "the vote ran" in one grep. ``None`` means the
    caller lets the candidate vote decide.

    *requested* is trusted as-is: it was already validated against the catalog
    by the API layer, and an explicit caller instruction beats a deployment
    default.
    """
    if requested:
        return requested, SCOPE_REQUESTED

    # Normalised here as well as in the env reader: a caller that passes the raw
    # value must not turn a blank setting into a "not a configured database"
    # warning on every request.
    default = (default or "").strip()
    prediction_default = (prediction_default or "").strip()
    names = _database_names(connectors)

    if question_type == _PREDICTION:
        if not prediction_default:
            return None, SKIP_PREDICTION
        return _pin(
            setting=ENV_PREDICTION_TARGET_DB,
            raw=prediction_default,
            reason=SCOPE_PREDICTION_DEFAULT,
            question=question,
            names=names,
        )

    if not default:
        return None, SCOPE_NONE

    return _pin(
        setting=ENV_DEFAULT_TARGET_DB,
        raw=default,
        reason=SCOPE_DEFAULT,
        question=question,
        names=names,
    )
