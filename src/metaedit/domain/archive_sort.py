"""How the archive's derived entities are ordered, and why the tiebreaker is not optional.

``ORDER BY name`` is not a total order. Measured on a live archive: 8 album rows and 2
track rows share a name with another row --

    'Corazones'   -> Jorge González (id 8), Los Prisioneros (id 90)
    'RAMMSTEIN'   -> two distinct Rammstein albums

-- and Postgres is free to return tied rows in any order, including a different order per
query. With ``OFFSET`` paging that means a row can be returned on two pages while another
is never returned at all.

That is not a hypothesis. Paging the live album table one row at a time through the
endpoint's own query reproduced it exactly:

    109 rows fetched, 108 distinct
    duplicated across pages: [90]
    never appearing:         [8]

So every orderable column is paired with ``id`` as a final tiebreaker. ``id`` is the
derived row's primary key and is assigned deterministically by the reindex (never by a
sequence), so it is stable across rebuilds -- which matters, because a tiebreaker that
changed between reindexes would reintroduce the same bug with extra steps.
"""

from __future__ import annotations

from typing import Any, Literal

SortKey = Literal["name", "listeners", "playcount", "last_seen"]
SortOrder = Literal["asc", "desc"]

# Browse vocabulary -> the column to order by. Each is a real column on all three entity
# models, so one mapping serves artists, albums and tracks.
SORT_COLUMNS: dict[str, str] = {
    "name": "name",
    "listeners": "listeners",
    "playcount": "playcount",
    "last_seen": "last_seen_at",
}

DEFAULT_SORT: SortKey = "name"
DEFAULT_ORDER: SortOrder = "asc"

# Ordering by a nullable column needs NULLS LAST in both directions, or "descending by
# listeners" leads with every row that has no listener count. Measured: most track rows
# have none, so the default NULLS FIRST ordering on DESC would put the least informative
# rows on page one.
NULLABLE_SORT_KEYS = frozenset({"listeners", "playcount"})


def order_by_clauses(model: Any, sort: str, order: str) -> tuple[Any, ...]:
    """The ``ORDER BY`` list for a derived-entity query.

    Always ends with ``id``. The tiebreaker is what makes ``OFFSET`` paging correct, and
    putting it in one function means a new sort key cannot be added without it.

    ``model`` is typed ``Any`` because it is a SQLAlchemy declarative class, and the
    alternative is importing the models here -- which would point the domain at the
    persistence layer and invert the dependency this package is arranged around. The
    column lookup is a ``getattr`` on a string name for the same reason: the mapping is
    the vocabulary, and a static attribute access cannot be driven by a query parameter.
    """
    if sort not in SORT_COLUMNS:
        raise KeyError(sort)
    column_name = SORT_COLUMNS[sort]
    column = getattr(model, column_name)
    descending = order == "desc"

    primary = column.desc() if descending else column.asc()
    if sort in NULLABLE_SORT_KEYS:
        # Explicit in both directions; the default differs between ASC and DESC.
        primary = column.desc().nullslast() if descending else column.asc().nullslast()

    id_column = model.id
    return (primary, id_column.desc() if descending else id_column.asc())
