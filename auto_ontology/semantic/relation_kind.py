# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Whether a relation is a fact source or a reference over one.

Two kinds of relation answer a question badly even though nothing about them is
wrong:

* **Views** (``website.customers``) are pre-joined wide relations meant for
  display. Analysing through one silently picks someone else's join and filter.
* **Archives** (``customers_archive``) hold *closed* versions of rows
  (``validto`` in the past). Joining one duplicates every entity that has more
  than one version, so a customer count doubles without anything failing.

Neither is a reason to hide the relation — someone asking about change history
needs the archive. It is a reason to rank it below a base table, which is what
``semantic_search``'s reference penalty does with the label this module assigns.

**Views are authoritative; archives are a convention.** ``table_type`` comes
from the connector's own catalog, so a view is known for certain.
"``_archive``" is a naming pattern Wide World Importers and its documentation
use; there is no catalog flag for "purged history", and inventing one would mean
guessing from data at ingest time. A table named ``..._archive`` that is really
a fact source (see the trade-off note in ``semantic_search``) is therefore
mislabelled on purpose rather than by accident.
"""

from __future__ import annotations

import re

from auto_ontology.catalog.constants import TableTypes

#: A base table.
KIND_TABLE = "table"
#: A view or materialized view: a stored query, not a source of rows.
KIND_VIEW = "view"
#: A base table whose name marks it as purged or versioned history.
KIND_ARCHIVE = "archive"

#: Suffixes that name a history relation. Kept to the forms that actually occur
#: in practice plus the obvious neighbours, so a column called ``archived_by``
#: is not mistaken for one — this is matched against a *table* name only.
_ARCHIVE_SUFFIX = re.compile(
    r"(_archive|_archived|_history|_hist|_backup|_bak|_old|_pruned)$",
    re.IGNORECASE,
)

_VIEW_TYPES = frozenset({TableTypes.VIEW, TableTypes.MATERIALIZED_VIEW})


def classify_relation(
    table_name: str | None,
    table_type: str | None = None,
) -> str:
    """Classify one relation as ``table``, ``view`` or ``archive``.

    *table_type* wins when it says view: that is catalog metadata, not a guess.
    Otherwise an archive-named base table is ``archive``, and everything else —
    including a name we were given nothing about — is an ordinary ``table``.
    Defaulting to ``table`` matters: a wrong label here downranks a legitimate
    fact source.
    """
    if (table_type or "").strip().lower() in _VIEW_TYPES:
        return KIND_VIEW
    if table_name and _ARCHIVE_SUFFIX.search(table_name.strip()):
        return KIND_ARCHIVE
    return KIND_TABLE


def classify_name(
    name: str | None,
    table_type: str | None = None,
) -> str:
    """Classify a relation *or* a name derived from one.

    ``classify_relation`` on its own only recognises a physical ``_archive``
    suffix. A Term compiled from an archive table is called "Archived City", so
    the naming heuristic in :func:`infer_from_text` has to run too. Catalog
    ``table_type`` still wins over both: a view is a view whatever it is called.
    """
    kind = classify_relation(name, table_type)
    if kind != KIND_TABLE:
        return kind
    return infer_from_text(name)


def is_reference_relation(kind: str | None) -> bool:
    """Whether *kind* is a reference over facts rather than a fact source."""
    return kind in {KIND_VIEW, KIND_ARCHIVE}


def _token_marks_archive(token: str) -> bool:
    """Whether one name token marks a history relation.

    Two forms: a suffix on the table's own name (``customers_archive``), and a
    leading word on a *display* name. The second exists because compilation
    names a term after the table's business meaning, not its physical name —
    Wide World Importers' archive tables produce terms called "Archived City".
    The cost is that a legitimately named "Archive Settings" term is demoted
    too; the penalty is soft and configurable for exactly that reason.
    """
    return bool(_ARCHIVE_SUFFIX.search(token)) or token.lower().startswith("archiv")


def infer_from_text(*values: str | None) -> str:
    """Best-effort classification from whatever names a stored row carries.

    Rows embedded before this module existed have no ``relation_kind`` in their
    metadata, and re-embedding a whole deployment is not a precondition for
    ranking correctly. The names that *are* in those rows still say it: a
    ColumnAttribute carries its physical ``table_name``, and terms derived from
    an archive table carry the word in the term name.

    Only archives are inferable this way. A view leaves no trace in a free-text
    name, so nothing is claimed for it — an unlabelled view keeps its current
    rank rather than being demoted on a guess. Term rows are the ones that most
    need the flag embedded (their names are display names), so a reindex after
    deploying this is what makes term-level demotion take effect.
    """
    for value in values:
        if not value:
            continue
        # A term name may embed the qualified table as a suffix; split on the
        # separators compile and qualify_table use.
        for token in re.split(r"[\s(),.:/]+", str(value)):
            if token and _token_marks_archive(token):
                return KIND_ARCHIVE
    return KIND_TABLE


__all__ = [
    "KIND_ARCHIVE",
    "KIND_TABLE",
    "KIND_VIEW",
    "classify_name",
    "classify_relation",
    "infer_from_text",
    "is_reference_relation",
]
