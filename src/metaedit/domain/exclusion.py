"""Exclusion patterns: the items an operator never wants in a selection.

A pattern is a shell-style glob (``*``, ``?``) matched case-insensitively. It is
**literal**, with no implicit wrapping: ``live`` matches exactly ``live``, and ``*live*``
is what matches a substring. That is worth stating plainly because the friendlier-looking
alternative -- treating a bare word as a substring -- is how ``Rock`` came to swallow
``Rockabilly`` elsewhere in this project (ADR 0015), and the same class of mistake here
would quietly drop items nobody meant to drop.

`fnmatch.translate` escapes everything except the wildcards, so an operator-typed pattern
cannot become a catastrophic-backtracking regex. That is the reason this is glob and not
regular expression: the input is typed by hand into a box, and a regex box is a denial-of-
service surface pointed at the operator's own server.

Jellyfin has no parameter for this. ``searchTerm`` is a substring match against the item's
name and nothing else, so it cannot express "not", cannot reach the album artist, and
cannot be negated. Every exclusion therefore costs a read of the items, which is why
callers must report the scan rather than present the result as the whole truth.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Sequence

from metaedit.adapters.lastfm.canonical import normalize_tag
from metaedit.domain.errors import ValidationError

# Bounds on operator input. A pattern list is a narrowing, so a long one is a mistake; the
# length cap also keeps the work per item predictable, which is what makes it safe to run
# this over a library of thousands of items.
MAX_PATTERNS = 50
MAX_PATTERN_LENGTH = 200

# The fields a pattern is matched against, in the order they are reported. Kept as a
# documented constant because two boundary adapters read it -- one for the API's item
# summary, one for a Jellyfin DTO -- and two lists that drift apart would mean an exclusion
# that works on one screen and not another. ``tests/unit/test_exclusion.py`` asserts they
# agree.
LABEL_SOURCES = ("name", "album", "album artist")


def compile_patterns(raw: Iterable[str] | str | None) -> tuple[str, ...]:
    """Normalise operator patterns for matching, refusing the ones that cannot be meant.

    A bare ``str`` is taken as one pattern rather than iterated. ``str`` is an
    ``Iterable[str]``, so the obvious implementation turns ``"android"`` into seven
    single-character patterns -- and any one of them being ``*`` makes the list match
    everything. That was not hypothetical: it is how an exclusion-only scan that matched
    nothing was found, because every item had been excluded by a stray ``*``.

    Normalisation is the same ``normalize_tag`` the rest of the project uses, applied to
    *both* sides of every comparison -- so case folding and Unicode folding (``ß``/``ss``)
    behave identically here and in the genre blacklist, rather than each doing its own.

    A pattern that is only whitespace is dropped rather than refused: an empty line in a
    textarea is not an instruction.
    """
    if isinstance(raw, str):
        raw = (raw,)
    compiled: list[str] = []
    for value in raw or ():
        candidate = normalize_tag(value)
        if not candidate:
            continue
        if len(candidate) > MAX_PATTERN_LENGTH:
            raise ValidationError(
                f"exclusion pattern {value.strip()[:40]!r} is longer than "
                f"{MAX_PATTERN_LENGTH} characters."
            )
        if candidate not in compiled:
            compiled.append(candidate)
    if len(compiled) > MAX_PATTERNS:
        raise ValidationError(
            f"{len(compiled)} exclusion patterns were given; at most {MAX_PATTERNS} are "
            "applied at once. Combine them with a wildcard instead of adding more."
        )
    return tuple(compiled)


def matched_pattern(value: str | None, patterns: Sequence[str]) -> str | None:
    """The first pattern that excludes ``value``, or ``None``.

    Returns the pattern rather than a bool because the report has to say *which* rule
    dropped an item: "excluded" with no reason is indistinguishable from a bug.
    """
    if not value or not patterns:
        return None
    folded = normalize_tag(value)
    if not folded:
        return None
    for pattern in patterns:
        if fnmatch.fnmatchcase(folded, pattern):
            return pattern
    return None


def first_match(labels: Sequence[str], patterns: Sequence[str]) -> tuple[str, str] | None:
    """The first ``(label, pattern)`` pair that excludes an item, or ``None``."""
    for label in labels:
        pattern = matched_pattern(label, patterns)
        if pattern is not None:
            return label, pattern
    return None


def is_excluded(labels: Sequence[str], patterns: Sequence[str]) -> bool:
    """Whether any label matches any pattern.

    ``any`` on both sides, deliberately. The labels are alternative readings of the same
    item -- its name, the album it is on, who it is by -- and a pattern naming any one of
    them is a statement about the item. Requiring all of them to match would make
    ``Various Artists`` (which appears only in the album artist) match nothing.
    """
    return first_match(labels, patterns) is not None


def exclusion_reason(labels: Sequence[str], patterns: Sequence[str]) -> str | None:
    """Why an item is excluded, phrased for the report. ``None`` when it is not."""
    found = first_match(labels, patterns)
    if found is None:
        return None
    label, pattern = found
    return f"matched exclusion pattern {pattern!r} on {label!r}"
