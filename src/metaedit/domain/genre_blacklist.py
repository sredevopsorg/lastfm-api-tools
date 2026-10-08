"""The operator's genre blacklist: parsing it, and matching against it.

Pure: no I/O, no database, no clock. The service layer reads and writes rows; this
module decides what those rows *mean*.

Three rules carry the feature, and each exists because the obvious implementation is
destructive or surprising:

1. **Matching is exact and case-insensitive -- never substring.** A blacklist entry
   ``"rock"`` must not swallow ``"gothic rock"``, ``"progressive rock"`` or
   ``"rockabilly"``. A substring matcher would drop most of a rock library, silently, on
   the next reindex.

2. **Matching happens against the values that reach ``Genres``, which are the *pieces* of
   a multi-valued tag.** This is not a detail: ``TagPolicy.classify`` runs every Last.fm
   tag through ``SPLIT_PATTERN``, which splits on ``,``, ``;``, ``|`` and `` / ``. So
   Last.fm's ``"Rock, Reggae"`` is proposed as two genres, ``Rock`` and ``Reggae``, and
   blacklisting ``rock`` correctly drops the first -- verified against the policy rather
   than assumed. Matching the un-split string would silently blacklist nothing.

   The consequence to be honest about: a *stored* genre containing a comma is likewise
   decomposed by the next write that touches ``Genres``. That is pre-existing behaviour
   of the mapping layer (see ``tests/unit/test_tags.py``), not something this module can
   prevent, and :func:`matches` documents it at the point it matters.

3. **The input format is comma-separated for convenience, but a comma is also a legitimate
   character in a stored genre value.** Both are true at once, so the ambiguity is
   resolved by *refusing to guess*: a line containing a comma is reported as a conflict
   carrying both readings, and the caller decides. Silently choosing is how one intended
   genre becomes two blacklisted fragments.

Normalisation delegates to ``normalize_tag``, the same function the archive's
``tag_name_norm`` columns use. A second normaliser is how the blacklist and the tag graph
would come to disagree about whether two strings are the same tag, and the disagreement
would be invisible until a genre was dropped that nobody blacklisted.
"""

from __future__ import annotations

from dataclasses import dataclass

from metaedit.adapters.lastfm.canonical import normalize_tag
from metaedit.domain.errors import MetaeditError

# The character the input format uses as a separator, and the one that is also a
# legitimate part of a genre value.
SEPARATOR = ","


def normalise(value: str | None) -> str:
    """The comparison key for a genre value: NFC, casefolded, whitespace collapsed.

    Thin alias over ``normalize_tag`` rather than a reimplementation, so blacklist
    matching and archive tag identity cannot drift apart.
    """
    return normalize_tag(value)


def matches(candidate: str | None, entries: frozenset[str] | set[str]) -> bool:
    """Whether ``candidate`` is blacklisted by ``entries``.

    ``entries`` may hold *either* normalised or raw values; both sides are normalised
    here. Callers pass normalised sets, and normalisation is idempotent, so accepting raw
    input costs nothing and removes a way to get it wrong.

    Exact equality only. ``"rock"`` matches ``"Rock"``; it does not match
    ``"Gothic Rock"`` or ``"rockabilly"``.

    This compares **one value**. Callers deciding what to write should pass the pieces a
    multi-valued tag decomposes into, because that is what ``TagPolicy`` proposes and what
    ends up in ``Genres`` -- see the module docstring, rule 2. Passing the raw stored
    string ``"Rock, Reggae"`` asks "is this whole string blacklisted", which is a
    different and usually unintended question; use :func:`matches_any_piece` for the
    write-path question.
    """
    key = normalise(candidate)
    if not key:
        return False
    return any(normalise(entry) == key for entry in entries if entry)


def matches_any_piece(value: str | None, entries: frozenset[str] | set[str]) -> bool:
    """Whether any component of a multi-valued ``value`` is blacklisted.

    The write-path question. ``SPLIT_PATTERN`` decomposes ``"Rock, Reggae"`` into
    ``Rock`` and ``Reggae`` on the way into ``Genres``, so an operator who blacklisted
    ``rock`` means the first of those -- and this returns True for it, while
    :func:`matches` on the whole string returns False.

    Splits with ``TagPolicy``'s own pattern rather than a private copy, so the blacklist
    and the mapping cannot disagree about where a genre value ends.
    """
    if not value:
        return False
    from metaedit.domain.tags import SPLIT_PATTERN

    return any(
        matches(piece.strip(), entries) for piece in SPLIT_PATTERN.split(value) if piece.strip()
    )


@dataclass(frozen=True, slots=True)
class Entry:
    """One parsed blacklist entry.

    ``value`` is what the operator typed, kept for display; ``norm`` is the comparison
    key. They differ whenever case or whitespace differ, and showing the typed form
    while comparing the normalised one is what lets the UI say "you blacklisted ``Rock``"
    and still match ``rock``.
    """

    value: str
    norm: str

    @property
    def has_separator(self) -> bool:
        return SEPARATOR in self.value


@dataclass(frozen=True, slots=True)
class Conflict:
    """An input line that contains a comma, and therefore cannot be read unambiguously.

    Carries everything the caller needs to present a choice rather than a complaint:
    the literal value the whole line *would* match if kept intact, and the fragments it
    would become if split. Neither reading is chosen here -- that is the point.
    """

    raw: str
    whole: str
    fragments: list[str]

    @property
    def message(self) -> str:
        return (
            f"{self.raw!r} contains a comma, which is also a legal character in a genre "
            f"value. As one genre it matches {self.whole!r}; split it would match "
            f"{self.fragments!r}. Put each genre on its own line to choose."
        )


@dataclass(frozen=True, slots=True)
class ParseResult:
    """What a block of text means, including what could not be read."""

    entries: list[Entry]
    conflicts: list[Conflict]
    # Entries that were empty once stripped, so a caller can tell "nothing was typed"
    # from "everything typed was whitespace".
    blanks: int = 0

    @property
    def values(self) -> list[str]:
        return [entry.value for entry in self.entries]

    @property
    def normalised(self) -> frozenset[str]:
        return frozenset(entry.norm for entry in self.entries)

    @property
    def clean(self) -> bool:
        """True when nothing needs the operator's attention."""
        return not self.conflicts

    def summary(self) -> dict[str, object]:
        return {
            "entries": self.values,
            "count": len(self.entries),
            "conflicts": [
                {"raw": c.raw, "whole": c.whole, "fragments": c.fragments, "message": c.message}
                for c in self.conflicts
            ],
            "blanks": self.blanks,
        }


def parse(raw: str, *, allow_commas: bool = False) -> ParseResult:
    """Read operator input into entries, reporting anything ambiguous.

    One entry per line. That is the unambiguous format, and the one the UI offers.

    ``allow_commas`` additionally splits each line on commas, for a caller that has
    already asked the operator what a comma means (see :func:`conflicts`). It is off by
    default so the *library* of this function is safe: a casual caller gets conflicts
    rather than a silent split.

    Deduplication is by normalised form and keeps first-seen order and spelling, so
    typing ``Rock`` and ``rock`` yields one entry named ``Rock``.
    """
    entries: list[Entry] = []
    conflicts: list[Conflict] = []
    seen: set[str] = set()
    blanks = 0

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            # Blank lines are layout, not content. Counting them would make a trailing
            # newline look like a rejected entry.
            blanks += 1
            continue

        if SEPARATOR in stripped:
            candidates = [part.strip() for part in stripped.split(SEPARATOR)]
            fragments = [part for part in candidates if part]
            if not allow_commas:
                conflicts.append(Conflict(raw=stripped, whole=stripped, fragments=fragments))
                continue
            for fragment in fragments:
                _add(fragment, entries, seen)
            continue

        _add(stripped, entries, seen)

    return ParseResult(entries=entries, conflicts=conflicts, blanks=blanks)


def _add(value: str, entries: list[Entry], seen: set[str]) -> None:
    norm = normalise(value)
    if not norm or norm in seen:
        return
    seen.add(norm)
    entries.append(Entry(value=value, norm=norm))


def conflicts(values: list[str], known: frozenset[str] | set[str] = frozenset()) -> list[Conflict]:
    """Which of ``values`` contain a comma, and what each reading would match.

    ``known`` is the live genre vocabulary, used to make ``whole`` informative rather
    than merely literal: when the operator's line is an exact genre value that exists in
    the library, ``whole`` is that value, which is the reading they almost certainly
    meant.
    """
    found: list[Conflict] = []
    for value in values:
        stripped = value.strip()
        if not stripped or SEPARATOR not in stripped:
            continue
        fragments = [part.strip() for part in stripped.split(SEPARATOR) if part.strip()]
        whole = stripped
        norm = normalise(stripped)
        for candidate in known:
            if normalise(candidate) == norm:
                whole = candidate
                break
        found.append(Conflict(raw=stripped, whole=whole, fragments=fragments))
    return found


class BlacklistConflictError(MetaeditError):
    """Input that cannot be read without guessing.

    A ``MetaeditError`` rather than a bare ``ValueError`` so the HTTP layer maps it to a
    422 with a message written for a person, and so the refusal carries a code the SPA
    can act on instead of parsing prose. The alternative -- accepting the line and
    picking a reading -- is how ``"Rock, Reggae"`` becomes two deleted genres.
    """

    code = "ambiguous_blacklist_entry"
    http_status = 422

    def __init__(self, conflicts_: list[Conflict]) -> None:
        self.conflicts = conflicts_
        detail = "; ".join(conflict.message for conflict in conflicts_)
        super().__init__(
            f"Refusing to guess how to read {len(conflicts_)} entr"
            f"{'y' if len(conflicts_) == 1 else 'ies'}: {detail}"
        )


def parse_strict(raw: str) -> ParseResult:
    """Parse, raising on any ambiguity. For callers with no way to ask the operator."""
    result = parse(raw)
    if result.conflicts:
        raise BlacklistConflictError(result.conflicts)
    return result
