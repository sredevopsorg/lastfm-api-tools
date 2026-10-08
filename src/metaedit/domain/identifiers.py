"""Item ids that are safe to hand to Jellyfin as a facet filter.

This module exists for one measured reason, and it is not tidiness. Jellyfin's
``ArtistIds``/``AlbumIds`` parameters are **all-or-nothing**: an entry it cannot parse is
discarded, and if nothing in the list parses, the parameter is ignored entirely and the
server answers with the *unfiltered* library. Live-verified on 12.2.0:

    ArtistIds=<32 hex>            albums=2     songs=21     the filter was applied
    ArtistIds=<two valid, one junk>  albums=2  songs=21     the valid ids survived
    ArtistIds=abc                 albums=502   songs=5442   the filter vanished
    ArtistIds=<32 hex>|<32 hex>   albums=502   songs=5442   the filter vanished
    ArtistIds=<40 hex>            albums=502   songs=5442   the filter vanished

So a single typo turns "narrow to these three artists" into "the whole library" -- and in
this application that set is what feeds a write. A guard that returns a 422 is therefore
not defensive politeness; it is the difference between narrowing a selection and silently
widening one, and widening it is the failure nobody notices until the metadata is wrong.

The check is a shape check, not an existence check. Whether an id names a real item is
Jellyfin's answer to give, and a well-formed id that matches nothing correctly returns
nothing.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from metaedit.domain.errors import ValidationError

# The two spellings a Jellyfin item id arrives in. Both were verified to work as a filter
# value on 12.2.0, in either case, with surrounding whitespace tolerated by the server --
# so accepting only the bare lowercase form would reject ids the server would have honoured.
_BARE = r"[0-9a-fA-F]{32}"
_DASHED = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

ITEM_ID_PATTERN = re.compile(rf"\A(?:{_BARE}|{_DASHED})\Z")

# A facet is a narrowing, so a list long enough to need a URL of its own is a mistake
# rather than an intent. 200 is far past any hand-picked selection and still inside the
# request-line limits of every proxy this tool is likely to sit behind.
MAX_FACET_IDS = 200

# The reason is stated in the error text rather than only here, because the caller that
# hits it is looking at a 422 and needs to know why a "filter" did the opposite of filter.
WHY_IDS_MUST_BE_GUIDS = (
    "Jellyfin discards the whole parameter when no entry parses and answers with the "
    "unfiltered library, so an id of the wrong shape silently widens the selection "
    "instead of narrowing it"
)


def is_item_id(value: str) -> bool:
    """Whether ``value`` is a shape Jellyfin will parse as an item id."""
    return bool(ITEM_ID_PATTERN.match(value.strip()))


def clean_item_ids(values: Iterable[str] | None, *, field: str) -> list[str]:
    """Validate and de-duplicate a facet id list, preserving the order given.

    Raises rather than dropping: a dropped id *narrows* a union, so removing a bad one
    would quietly answer a different question than the caller asked. Refusing to answer is
    the only outcome that cannot be mistaken for success.

    Empty entries are ignored instead of refused -- an empty repeated query parameter is
    what a form submits when a picker is cleared, and it means "no filter", not "filter by
    the empty string".
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in values or ():
        candidate = raw.strip()
        if not candidate:
            continue
        if not is_item_id(candidate):
            # Truncated so a hostile caller cannot use the error text as an amplifier; the
            # value is echoed because "which one is wrong" is the whole question.
            shown = candidate if len(candidate) <= 40 else f"{candidate[:40]}…"
            raise ValidationError(
                f"{field} contains {shown!r}, which is not an item id "
                f"(expected 32 hex characters, or a dashed GUID). {WHY_IDS_MUST_BE_GUIDS}."
            )
        if candidate not in seen:
            seen.add(candidate)
            cleaned.append(candidate)

    if len(cleaned) > MAX_FACET_IDS:
        raise ValidationError(
            f"{field} names {len(cleaned)} ids; at most {MAX_FACET_IDS} can be filtered on "
            "at once. Narrow the selection instead of widening the list."
        )
    return cleaned
