"""Tag policy: turning a flat Last.fm tag list into Jellyfin genres and styles.

Last.fm has no genre/style distinction — it exposes one flat tag set, each tag with
an optional popularity count (ADR 0006). Jellyfin has both a ``Genres`` array and a
``Tags`` array. So "genre" and "style" are *our* mapping concepts over one list, and
this module is where that mapping is decided. It is pure: no I/O, no clock, no
configuration lookup beyond the policy passed in.

The policy is deliberately data-driven rather than hard-coded, because the honest
thing about a community tag set is that it is noisy: ``seen live``, ``bought at
concert``, ``1001 albums`` and the artist's own name all appear as tags, and the
right split differs per library. Everything is tunable, and retuning is a reindex
rather than a re-crawl, because the raw archive keeps the original tag lists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from metaedit.adapters.lastfm.canonical import normalize_tag

# Tags that carry no genre information. Kept short and obvious on purpose: a long
# clever list would encode one person's library and be wrong for the next.
DEFAULT_BLACKLIST: frozenset[str] = frozenset(
    {
        # Scrobbling artefacts and personal bookkeeping.
        "seen live",
        "live",
        "concerts",
        "gigs",
        "i own this",
        "own it",
        "owned",
        "albums i own",
        "want to see live",
        "saw live",
        "gig",
        # Subjective / non-genre.
        "favorites",
        "favourites",
        "favorite",
        "favourite",
        "love",
        "loved",
        "awesome",
        "amazing",
        "good",
        "great",
        "best",
        "beautiful",
        "chill",
        "epic",
        # Format and technical noise.
        "mp3",
        "flac",
        "vinyl",
        "cd",
        "digital",
        "music",
        "albums",
        "songs",
        # Catch-all buckets Last.fm uses when nobody tagged properly.
        "seen live 2024",
        "all",
        "other",
        "unknown",
        "various",
        "misc",
        "to listen",
        "listen",
    }
)

# Characters that separate several tags crammed into one value. Some file taggers
# write "Rock; Alternative" or "Rock / Alternative" as a single tag, and a slashed
# genre is common ("R&B/Soul"), so splitting is applied to *tag values* and never to
# an entity name -- "AC/DC" and "Godspeed You! Black Emperor" must survive intact.
SPLIT_PATTERN = re.compile(r"\s*[;,|]\s*|\s+/\s+")

_YEAR_PATTERN = re.compile(r"^(19|20)\d{2}$")
_URL_PATTERN = re.compile(r"^https?://", re.IGNORECASE)

Mode = Literal["merge", "replace", "fill_if_empty"]


@dataclass(frozen=True, slots=True)
class TagInput:
    """One Last.fm tag, as it arrived.

    ``count`` is ``None`` where Last.fm supplied no popularity (every method except
    ``artist.getTopTags``), which is not the same as a count of zero.
    """

    name: str
    count: int | None = None


@dataclass(frozen=True, slots=True)
class DroppedTag:
    name: str
    reason: str


@dataclass(frozen=True, slots=True)
class TagOutcome:
    """The tags that survive, and what happened to the rest.

    ``dropped`` exists so the UI can explain a result rather than just present it:
    "3 tags dropped by blacklist" is the difference between a policy an operator can
    debug and one they have to guess at.
    """

    genres: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    dropped: list[DroppedTag] = field(default_factory=list)
    # Tags that ranked but fell outside the two limits.
    overflow: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        parts = [f"{len(self.genres)} genres", f"{len(self.tags)} tags"]
        if self.dropped:
            parts.append(f"{len(self.dropped)} dropped")
        if self.overflow:
            parts.append(f"{len(self.overflow)} over limit")
        return ", ".join(parts)


@dataclass(frozen=True, slots=True)
class TagPolicy:
    """How to turn a tag list into genres and tags."""

    genre_limit: int = 5
    style_limit: int = 10
    # Tags below this popularity are ignored. Zero means "keep everything ranked",
    # so the behaviour is opt-in rather than a surprising default.
    min_count: int = 0
    blacklist: frozenset[str] = DEFAULT_BLACKLIST
    extra_blacklist: frozenset[str] = frozenset()
    max_tags_per_item: int = 30
    max_tag_length: int = 100
    mode: Mode = "merge"
    # Names that must never become genres: tagging an artist with itself is common
    # and carries no information.
    exclude_names: tuple[str, ...] = ()

    def effective_blacklist(self) -> frozenset[str]:
        return self.blacklist | self.extra_blacklist

    def normalise(self, raw: str) -> str:
        """Canonical form, whitespace collapsed, case folded for comparison."""
        return normalize_tag(raw)

    def split(self, raw: str) -> list[str]:
        """Split a possibly multi-valued tag string into display values."""
        parts = [part.strip() for part in SPLIT_PATTERN.split(raw)]
        return [part for part in parts if part]

    def rejection_reason(self, display: str) -> str | None:
        """Why this tag must not be used, or ``None`` if it is acceptable.

        Returns a reason rather than a bool so the report can say *why*. Empty
        values are not a policy decision and are filtered before this is consulted,
        so they are deliberately not reported as rejections -- a report full of
        "dropped: empty" is noise that hides the real decisions.
        """
        if len(display) > self.max_tag_length:
            return f"longer than {self.max_tag_length} characters"
        if _URL_PATTERN.match(display):
            return "a URL"
        if _YEAR_PATTERN.match(display.strip()):
            return "a bare year"
        norm = self.normalise(display)
        if norm in self.effective_blacklist():
            return "on the blacklist"
        for name in self.exclude_names:
            if norm and norm == self.normalise(name):
                return "the item's own name"
        return None

    def rank(self, tags: list[TagInput]) -> list[TagInput]:
        """Order tags by popularity, falling back to the order Last.fm gave them.

        ``artist.getTopTags`` supplies counts and ``*getInfo`` does not, so ranking
        cannot simply sort by count -- that would reorder the count-less lists
        arbitrarily. Ties keep their source order, which is itself a ranking signal.
        """
        counted = [tag for tag in tags if tag.count is not None]
        uncounted = [tag for tag in tags if tag.count is None]
        if counted and uncounted:
            # Both kinds present: trust the counts for the counted ones and place
            # the uncounted afterwards in their original order.
            counted.sort(key=lambda tag: tag.count or 0, reverse=True)
            return [*counted, *uncounted]
        if counted:
            counted.sort(key=lambda tag: tag.count or 0, reverse=True)
            return counted
        return uncounted

    def classify(self, tags: list[TagInput]) -> TagOutcome:
        """Filter, rank and split tags into genres and styles. No merging.

        Merging is deliberately *not* done here. Each field has its own mode
        (``Genres`` might replace while ``Tags`` merges), so merging is a field-level
        decision and belongs to the caller. Doing it here made a field-level
        ``replace`` override silently ineffective, because the merge had already
        happened using the classifier's own mode.
        """
        accepted: list[str] = []
        dropped: list[DroppedTag] = []
        seen: set[str] = set()

        for tag in self.rank(tags):
            for piece in self.split(tag.name):
                norm = self.normalise(piece)
                if not norm:
                    continue
                if norm in seen:
                    dropped.append(DroppedTag(name=piece, reason="duplicate"))
                    continue
                reason = self.rejection_reason(piece)
                if reason:
                    dropped.append(DroppedTag(name=piece, reason=reason))
                    continue
                if tag.count is not None and tag.count < self.min_count:
                    dropped.append(
                        DroppedTag(name=piece, reason=f"count {tag.count} below {self.min_count}")
                    )
                    continue
                seen.add(norm)
                accepted.append(piece)

        genres = accepted[: self.genre_limit]
        remainder = accepted[self.genre_limit :]
        styles = remainder[: self.style_limit]
        overflow = remainder[self.style_limit :]

        styles = styles[: max(self.max_tags_per_item - len(genres), 0)]
        genres = genres[: self.max_tags_per_item]
        return TagOutcome(genres=genres, tags=styles, dropped=dropped, overflow=overflow)

    def merge_into(self, proposed: list[str], existing: list[str] | None) -> list[str]:
        """Union proposed onto existing, existing first, case-insensitively."""
        return _merge(proposed, existing or [], self)

    def apply(
        self,
        tags: list[TagInput],
        *,
        existing_genres: list[str] | None = None,
        existing_tags: list[str] | None = None,
    ) -> TagOutcome:
        """Classify and merge in one step, for callers with a single mode.

        The limits bound how many *Last.fm* tags are promoted, not the final list
        length: in ``merge`` mode existing values are unioned on top, so an item that
        already had six curated genres can end up with more than ``genre_limit``.
        That is deliberate -- a limit must never silently drop a curated genre -- so
        callers needing a hard cap should use ``max_tags_per_item`` or ``replace``.
        """
        outcome = self.classify(tags)
        genres = outcome.genres
        styles = outcome.tags
        if self.mode == "merge":
            genres = self.merge_into(genres, existing_genres)
            styles = self.merge_into(styles, existing_tags)
        styles = styles[: max(self.max_tags_per_item - len(genres), 0)]
        genres = genres[: self.max_tags_per_item]
        return TagOutcome(
            genres=genres,
            tags=styles,
            dropped=outcome.dropped,
            overflow=outcome.overflow,
        )


def _merge(proposed: list[str], existing: list[str], policy: TagPolicy) -> list[str]:
    """Union, existing first, case-insensitively deduplicated.

    Existing values come first so a curated list keeps its order: this tool should
    not reshuffle what an operator already arranged, and Jellyfin's genre order is
    what clients display.
    """
    merged: list[str] = []
    seen: set[str] = set()
    # Both lists are already strings: Jellyfin's arrays are normalised by
    # domain.snapshot before they reach here, and proposed values come from the
    # policy above. No runtime guard is needed, and one would silently hide a
    # normalisation regression rather than catch it.
    for value in [*existing, *proposed]:
        for piece in policy.split(value):
            norm = policy.normalise(piece)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            merged.append(piece)
    return merged


def tags_from_payload(payload: Any) -> list[TagInput]:
    """Read a tag list from a stored Last.fm payload.

    Accepts both shapes that reach the derived layer: ``toptags`` from
    ``artist.getTopTags`` (with counts) and the embedded tag collection on a
    ``*getInfo`` response (without).
    """
    entries = _entries(payload)
    result: list[TagInput] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        count = entry.get("count")
        result.append(
            TagInput(
                name=name.strip(),
                count=int(count)
                if isinstance(count, (int, str)) and str(count).isdigit()
                else None,
            )
        )
    return result


def _entries(payload: Any) -> list[Any]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    # {"tag": [...]} is the wire shape; tolerate a bare tag object too.
    if "tag" not in payload:
        return [payload]
    raw: Any = payload["tag"]
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return raw
    return []
