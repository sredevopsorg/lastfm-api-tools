"""The browse query's sort vocabulary.

``SORT_BY_JELLYFIN_KEY`` is an allow-list, and this is what keeps it one. The failure it
prevents is specific and was observed on a live server: Jellyfin **accepts an
unrecognised ``sortBy`` value and silently ignores it**, returning the default order. So
a pass-through implementation would let the UI say "sorted by year" over a list sorted by
name, and no layer would report a problem.

Every key asserted here was verified to change the returned order on a live Jellyfin
12.2.0 (641 artists / 502 albums); the probe output is in ``docs/development.md``.
"""

from __future__ import annotations

import pytest

from metaedit.domain.browse import (
    DEFAULT_ORDER,
    DEFAULT_SORT,
    ORDERLESS_SORT_KEYS,
    SORT_BY_JELLYFIN_KEY,
    jellyfin_sort_by,
)

# Jellyfin's own sort field names. A browse key that maps to something else is either a
# typo or a field the server will ignore, and both are the bug this list prevents.
JELLYFIN_SORT_KEYS = {"Name", "SortName", "DateCreated", "ProductionYear", "Random"}


def test_every_browse_key_maps_to_a_real_jellyfin_sort_key() -> None:
    for browse_key, jellyfin_key in SORT_BY_JELLYFIN_KEY.items():
        assert jellyfin_key in JELLYFIN_SORT_KEYS, f"{browse_key} -> unknown {jellyfin_key}"


def test_the_defaults_are_themselves_valid_keys() -> None:
    """A default that is not in the map would KeyError on every unpaged browse."""
    assert DEFAULT_SORT in SORT_BY_JELLYFIN_KEY
    assert DEFAULT_ORDER in ("asc", "desc")


@pytest.mark.parametrize("sort", sorted(SORT_BY_JELLYFIN_KEY))
def test_every_key_produces_a_single_element_sort_chain(sort: str) -> None:
    keys, _ = jellyfin_sort_by(sort, "asc")
    assert keys == (SORT_BY_JELLYFIN_KEY[sort],)


def test_an_unknown_sort_key_raises_rather_than_falling_through() -> None:
    """The whole point. Jellyfin would ignore it; we must not."""
    with pytest.raises(KeyError):
        jellyfin_sort_by("release_year", "asc")


@pytest.mark.parametrize(
    ("order", "expected"),
    [("asc", "Ascending"), ("desc", "Descending")],
)
def test_order_maps_to_jellyfins_vocabulary(order: str, expected: str) -> None:
    _, sort_order = jellyfin_sort_by("sort_name", order)
    assert sort_order == expected


def test_random_is_orderless_and_says_so() -> None:
    """A shuffle cannot be reversed, and pretending otherwise would be a lie in the UI."""
    assert "random" in ORDERLESS_SORT_KEYS
    forward, _ = jellyfin_sort_by("random", "asc")
    backward, backward_order = jellyfin_sort_by("random", "desc")
    assert forward == backward
    assert backward_order == "Ascending"


def test_name_and_sort_name_are_different_questions() -> None:
    """`SortName` honours ForcedSortName ("The Cure" files under C); `Name` does not.

    Collapsing them would remove the ability to ask either question, which is why both
    are in the map rather than one being treated as a duplicate.
    """
    assert SORT_BY_JELLYFIN_KEY["name"] != SORT_BY_JELLYFIN_KEY["sort_name"]
