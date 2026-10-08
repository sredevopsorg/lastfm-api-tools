"""Item id validation, and the widening it prevents.

The behaviour under test is not "does the regex match a GUID". It is: a facet filter must
never answer with *more* than it was asked for. Jellyfin drops an unparseable id list
entirely and replies with the unfiltered library, so every case here is a shape that would
otherwise turn a narrowing into a widening.
"""

from __future__ import annotations

import pytest

from metaedit.domain.errors import ValidationError
from metaedit.domain.identifiers import (
    MAX_FACET_IDS,
    clean_item_ids,
    is_item_id,
)

BARE = "bbc3e56260455521dfda7effa56e14f2"
OTHER = "55032a1fe7e44ec8b3723943cf7db850"
DASHED = "bbc3e562-6045-5521-dfda-7effa56e14f2"


@pytest.mark.parametrize(
    "value",
    [
        BARE,
        BARE.upper(),
        DASHED,
        DASHED.upper(),
        f"  {BARE}  ",
    ],
)
def test_shapes_jellyfin_parses_are_accepted(value: str) -> None:
    # Every one of these was verified to work as a filter value on a live 12.2.0 server,
    # including the dashed and uppercase forms. Rejecting them would refuse an id the
    # server would have honoured.
    assert is_item_id(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "abc",
        "0" * 40,
        "0" * 31,
        f"{BARE}|{OTHER}",
        f"{BARE},{OTHER}",
        "not-an-id",
        f"{BARE} " + "x",
        "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz",
    ],
)
def test_shapes_jellyfin_discards_are_refused(value: str) -> None:
    # `<32 hex>|<32 hex>` and a 40-character string both returned the *whole* library when
    # probed live. They are the reason this module exists.
    assert not is_item_id(value)


def test_a_malformed_id_is_refused_rather_than_dropped() -> None:
    # Dropping it would narrow a union, which is a quieter wrong answer than a 422: the
    # caller would get results and believe they were the ones asked for.
    with pytest.raises(ValidationError) as caught:
        clean_item_ids([BARE, "abc"], field="artist_ids")

    message = caught.value.message
    assert "artist_ids" in message
    assert "abc" in message
    # The reason, not just the rule: without it the operator cannot tell why a "filter"
    # widened a selection.
    assert "unfiltered library" in message
    assert caught.value.http_status == 422


def test_the_error_names_the_field_so_a_form_can_say_which_box() -> None:
    with pytest.raises(ValidationError):
        clean_item_ids(["nonsense"], field="album_ids")


def test_a_hostile_value_cannot_amplify_the_error_text() -> None:
    with pytest.raises(ValidationError) as caught:
        clean_item_ids(["x" * 5000], field="artist_ids")

    assert len(caught.value.message) < 400


def test_order_is_preserved_and_duplicates_collapse() -> None:
    # Order matters because the parameters are joined into one comma-separated value and a
    # test asserting on the request should read predictably.
    assert clean_item_ids([OTHER, BARE, OTHER], field="artist_ids") == [OTHER, BARE]


def test_blank_entries_mean_no_filter_rather_than_an_error() -> None:
    # What a cleared form submits. Refusing it would make "clear the picker" a 422.
    assert clean_item_ids(["", "  ", BARE, ""], field="artist_ids") == [BARE]
    assert clean_item_ids(None, field="artist_ids") == []
    assert clean_item_ids([], field="artist_ids") == []


def test_a_list_long_enough_to_be_a_mistake_is_refused() -> None:
    ids = [f"{index:032x}" for index in range(MAX_FACET_IDS + 1)]
    with pytest.raises(ValidationError) as caught:
        clean_item_ids(ids, field="artist_ids")
    assert str(MAX_FACET_IDS) in caught.value.message


def test_exactly_at_the_cap_is_allowed() -> None:
    ids = [f"{index:032x}" for index in range(MAX_FACET_IDS)]
    assert len(clean_item_ids(ids, field="artist_ids")) == MAX_FACET_IDS
