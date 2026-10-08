"""Removing one genre from a set of items.

Pure: no I/O, no database, no Jellyfin client. The service layer finds which items to
look at and performs the write; this module decides what the new value of a field is.

The whole feature turns on one question: **which stored values does the operator's input
match?** Getting it wrong in one direction deletes nothing and looks like a bug in the
filter; getting it wrong in the other deletes genres nobody named. So the rule is:

    a stored value matches when it equals the target, case-insensitively, after
    Unicode NFC normalisation and whitespace collapsing -- never as a substring,
    and never by splitting the value.

Measured on the live library, that distinction is worth real data. ``Genres=Rock``
returns 67 albums; ``Genres=Rock, Reggae`` returns 1. Those are different genres, and a
matcher that treated one as containing the other would delete 67 albums' worth of
curation for a request that named a genre occurring once.

The multi-value question is real, though: a stored genre is *sometimes* a packed list
(``"Thrash Metal, Speed Metal, Heavy Metal, Hard Rock"``, ``"Reggae; Ska"``) because
file taggers write them that way. So there are two operations, kept separate and never
conflated:

* :func:`remove_value` removes values that **equal** the target. The safe default.
* :func:`split_packed_values` decomposes a packed value and removes matching components,
  optionally dropping the container. Explicit, because it is the destructive reading.

Neither is applied implicitly. ``TagPolicy``'s ``SPLIT_PATTERN`` already decomposes
packed values on the *write* path (which is how a stored ``"Rock, Reggae"`` becomes two
genres on the next merge); this module is about acting on a stored value on purpose.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from metaedit.adapters.lastfm.canonical import normalize_tag
from metaedit.domain.tags import SPLIT_PATTERN

# Which fields a removal may act on. Both are string arrays on the Jellyfin item, and
# both are written to by this application: ``Genres`` from Last.fm's top tags and
# ``Tags`` from the remainder (ADR 0006).
RemovalField = Literal["Genres", "Tags"]

REMOVAL_FIELDS: tuple[RemovalField, ...] = ("Genres", "Tags")


def normalise(value: str | None) -> str:
    """The comparison key: NFC, casefolded, whitespace collapsed.

    The same function the archive uses for ``tag_name_norm``, so "the same genre" means
    one thing across the whole application.
    """
    return normalize_tag(value)


def matches(candidate: str | None, target: str | None) -> bool:
    """Whether a stored value *is* the target, case-insensitively.

    Exact equality. ``"Rock, Reggae"`` does not match ``"Rock"``, and ``"Gothic Rock"``
    does not either. This is the only definition of "matches" used by
    :func:`remove_value`, and it is why the feature cannot delete a genre the operator
    did not name.
    """
    key = normalise(target)
    if not key:
        return False
    return normalise(candidate) == key


@dataclass(frozen=True, slots=True)
class RemovalOutcome:
    """What removing a target from one field would do.

    ``removed`` and ``kept`` are the full new list and its complement, so a caller can
    report exactly which spellings disappeared -- which is the difference between an
    operator trusting the tool and re-checking every item by hand.
    """

    field: str
    target: str
    before: list[str]
    after: list[str]
    removed: list[str]

    @property
    def changed(self) -> bool:
        return bool(self.removed)

    @property
    def emptied(self) -> bool:
        """The field would become empty.

        Surfaced rather than hidden: clearing ``Genres`` entirely is a legitimate edit
        and is revertible, but it is also the outcome most likely to be unintended, so
        the UI can say so before the write.
        """
        return bool(self.before) and not self.after

    def as_dict(self) -> dict[str, object]:
        return {
            "field": self.field,
            "target": self.target,
            "before": list(self.before),
            "after": list(self.after),
            "removed": list(self.removed),
            "changed": self.changed,
            "emptied": self.emptied,
        }


def remove_value(values: list[str] | None, *, target: str) -> tuple[list[str], list[str]]:
    """Remove every value equal to ``target``. Returns ``(kept, removed)``.

    Order is preserved so a curated genre list keeps the arrangement the operator gave
    it -- Jellyfin's genre order is what clients display and what the album page reads.

    Duplicates are all removed; the caller is not left with a second copy that would
    look like the removal had failed.
    """
    kept: list[str] = []
    removed: list[str] = []
    # No `isinstance(value, str)` guard, deliberately. Jellyfin's arrays are normalised to
    # strings by `domain.snapshot` before they reach here, so the check would be dead code
    # -- mypy reports it as unreachable, which is the correct answer rather than an
    # inconvenience. A guard would also be worse than useless: it would silently keep a
    # non-string, hiding a normalisation regression instead of surfacing it. The declared
    # type is the guarantee, and the caller's obligation is to honour it.
    for value in values or []:
        if matches(value, target):
            removed.append(value)
        else:
            kept.append(value)
    return kept, removed


def plan_removal(values: list[str] | None, *, target: str, field: str) -> RemovalOutcome:
    """What removing ``target`` from one field of one item would do."""
    kept, removed = remove_value(values, target=target)
    return RemovalOutcome(
        field=field,
        target=target,
        before=list(values or []),
        after=kept,
        removed=removed,
    )


def plan_removals(
    item: Mapping[str, object], *, target: str, fields: tuple[str, ...]
) -> list[RemovalOutcome]:
    """One outcome per requested field, for one item.

    Takes a ``Mapping`` rather than a ``dict`` because callers pass whatever shape they
    have -- a ``NormalizedItem.fields`` dict or a literal in a test -- and ``dict`` is
    invariant in its value type, so a ``dict[str, list[str]]`` is not a
    ``dict[str, object]``. Accepting the read-only view is what lets both through.

    A field the item does not have is still reported, with an empty ``before`` and
    ``changed=False``, so the UI can show "not present" rather than omitting a row the
    operator asked about. An absent row is indistinguishable from a bug.
    """
    outcomes: list[RemovalOutcome] = []
    for field in fields:
        raw = item.get(field)
        values = [value for value in raw if isinstance(value, str)] if isinstance(raw, list) else []
        outcomes.append(plan_removal(values, target=target, field=field))
    return outcomes


# ------------------------------------------------------------- packed values


@dataclass(frozen=True, slots=True)
class SplitOutcome:
    """The result of decomposing one packed value."""

    original: str
    components: list[str]
    kept_components: list[str]
    removed_components: list[str]
    # The value to write in place of the original: the packed form when anything
    # survives (with the removed components dropped), or None when nothing does.
    replacement: str | None

    @property
    def changed(self) -> bool:
        return bool(self.removed_components)


def split_packed_values(value: str) -> list[str]:
    """Decompose a packed genre value with the policy's own pattern.

    ``"Thrash Metal, Speed Metal, Heavy Metal, Hard Rock"`` -> four components. Uses
    ``SPLIT_PATTERN`` rather than a copy so this cannot disagree with the mapping layer
    about where a genre value ends -- the same reasoning as the blacklist sharing
    ``normalize_tag``.
    """
    return [piece.strip() for piece in SPLIT_PATTERN.split(value) if piece.strip()]


def remove_component(value: str, *, target: str) -> SplitOutcome:
    """Remove matching components from a packed value.

    The destructive reading, kept explicit. ``"Rock, Reggae"`` with target ``"Reggae"``
    yields ``"Rock"`` -- note *four* characters, not a list of one, because the value is
    rebuilt as a string since that is what a packed value is.

    When several components match, all are dropped. When none survive, ``replacement`` is
    ``None``, and the caller decides whether that means "drop the value" or "leave it
    alone" -- this function does not guess.
    """
    components = split_packed_values(value)
    kept: list[str] = []
    removed: list[str] = []
    for component in components:
        if matches(component, target):
            removed.append(component)
        else:
            kept.append(component)

    replacement: str | None
    if not removed:
        replacement = value
    elif not kept:
        replacement = None
    else:
        # Rebuild with the same separator the value used. A comma is the common case and
        # the one the live library mixes with `;`, so the original's first separator is
        # reused rather than imposed -- rewriting `"Reggae; Ska"` as `"Reggae, Ska"` would
        # be an unrelated edit to a value the operator did not ask to restyle.
        separator = _first_separator(value)
        replacement = separator.join(kept)

    return SplitOutcome(
        original=value,
        components=components,
        kept_components=kept,
        removed_components=removed,
        replacement=replacement,
    )


def _first_separator(value: str) -> str:
    """The separator a packed value already uses, for rebuilding it.

    Defaults to ``, `` (comma-space) when none is found, which cannot happen for a value
    ``SPLIT_PATTERN`` split into more than one piece -- but returning a sensible separator
    beats raising on a case that is unreachable by construction.
    """
    for match in SPLIT_PATTERN.finditer(value):
        found = match.group(0)
        if found.strip() == ",":
            return ", "
        if found.strip() == ";":
            return "; "
        if found.strip() == "|":
            return " | "
        if found.strip() == "/":
            return " / "
    return ", "
