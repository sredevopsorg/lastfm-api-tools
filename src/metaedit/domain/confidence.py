"""Match confidence: is this Last.fm entity really the one I am looking at?

Every metadata bug this tool could cause starts here. Pulling the wrong artist's
tags onto an item is worse than pulling nothing, because the result looks
authoritative and the mistake is invisible afterwards.

So the score is deliberately conservative and *explainable*: each component is
reported separately, weights are renormalised when a component does not apply, and
one condition -- conflicting MusicBrainz ids -- overrides the arithmetic entirely
rather than being averaged away.

The thresholds are policy, not physics:
``auto`` at 0.85, ``review`` at 0.60, below which nothing is proposed at all.

**A consequence worth knowing:** when neither side has a MusicBrainz id, the MBID
component scores a neutral 0.5 rather than 1.0, so a perfect name match totals 0.833
and lands in ``review`` -- never ``auto``. That is deliberate (without ids we cannot
confirm identity, only infer it) but it means a library whose files carry no MBIDs
will require a human on every item, and bulk apply will never pre-select anything.
The neutral 0.5 is the tuning knob for that trade-off, and it is tested.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Literal

Verdict = Literal["auto", "review", "reject"]

AUTO_THRESHOLD = 0.85
REVIEW_THRESHOLD = 0.60

# Component weights, from the plan's §5.5. They sum to 1.00 for an album.
WEIGHT_NAME = 0.50
WEIGHT_MBID = 0.25
WEIGHT_ARTIST = 0.15
WEIGHT_YEAR = 0.10
# A track adds duration agreement; the total is renormalised.
WEIGHT_DURATION = 0.05

# A duration difference beyond this is not the same recording.
DURATION_TOLERANCE_MS = 5000

_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_for_comparison(value: str | None) -> str:
    """Lower-case, strip punctuation and accents, collapse whitespace.

    Accents are folded because Jellyfin's tag readers and Last.fm disagree about
    them constantly ("Björk" vs "Bjork"), and a diacritic difference is never the
    signal we are trying to measure.
    """
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = without_accents.casefold()
    stripped = _PUNCTUATION.sub(" ", lowered)
    return " ".join(stripped.split())


def name_similarity(left: str | None, right: str | None) -> float:
    """How alike two names are, from 0 to 1.

    Combines two measures because each covers a case the other misses:

    * a **token-set** score, which is order-insensitive, so "Beatles, The" and
      "The Beatles" match, and which tolerates extra words such as a featured
      artist or a remaster suffix;
    * :class:`difflib.SequenceMatcher`, which tolerates spelling differences and
      typos that a token comparison would treat as wholly different words.

    The maximum is taken rather than the average: either measure alone recognising a
    match is evidence of a match, and averaging would drag a genuine match below the
    review threshold when only one measure understood it.
    """
    a = normalize_for_comparison(left)
    b = normalize_for_comparison(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    left_tokens = set(a.split())
    right_tokens = set(b.split())
    if left_tokens and right_tokens:
        shared = left_tokens & right_tokens
        token_score = 2 * len(shared) / (len(left_tokens) + len(right_tokens))
    else:
        token_score = 0.0

    sequence_score = SequenceMatcher(None, a, b).ratio()
    return round(max(token_score, sequence_score), 4)


def mbid_score(*, item_mbid: str | None, candidate_mbid: str | None) -> tuple[float, bool]:
    """Agreement between MusicBrainz ids, and whether they actively conflict.

    Returns ``(score, conflict)``. A conflict is returned rather than folded into
    the number because it is a *disagreement about identity*, not a weak signal: two
    different MBIDs mean these are provably different entities, and no amount of
    name similarity should be able to average that away.
    """
    item = (item_mbid or "").strip().lower()
    candidate = (candidate_mbid or "").strip().lower()
    if item and candidate:
        if item == candidate:
            return 1.0, False
        return 0.0, True
    # Only one side is known: the ids neither confirm nor contradict. A neutral
    # score keeps an MBID-less library from being penalised for lacking data it
    # cannot have, while still leaving the name component to do the work.
    return 0.5, False


def comparable_name(left: str | None, right: str | None) -> float | None:
    """Name similarity, or ``None`` when there is no name on one side to compare.

    Absent data is not disagreement. Scoring a missing name as 0.0 would penalise a
    candidate for a field the response simply did not carry -- the same mistake that
    treating a missing year as a mismatch would make, and which this module avoids
    everywhere else. A truly nameless pair therefore reports "nothing comparable"
    instead of a confident-looking low number.
    """
    if not normalize_for_comparison(left) or not normalize_for_comparison(right):
        return None
    return name_similarity(left, right)


def artist_agreement(
    *, item_artists: list[str] | None, candidate_artist: str | None
) -> float | None:
    """Best name similarity between the candidate's artist and any credited artist.

    ``None`` when either side has nothing to compare, so the component is excluded
    from the weighted total rather than counted as a failure.
    """
    if not candidate_artist or not item_artists:
        return None
    best = max((name_similarity(name, candidate_artist) for name in item_artists), default=0.0)
    return best


def year_agreement(*, item_year: int | None, candidate_year: int | None) -> float | None:
    """How well the years line up. ``None`` when either is unknown.

    A one-year difference is treated as agreement, because release dates and
    reissues routinely disagree by a year between sources.
    """
    if item_year is None or candidate_year is None:
        return None
    difference = abs(item_year - candidate_year)
    if difference == 0:
        return 1.0
    if difference <= 1:
        return 0.8
    if difference <= 2:
        return 0.4
    return 0.0


def duration_agreement(
    *, item_duration_ms: int | None, candidate_duration_ms: int | None
) -> float | None:
    """Whether two track durations are the same recording.

    A remaster, a radio edit or a live version differs by more than a few seconds,
    so this is a genuinely discriminating signal for tracks.
    """
    if item_duration_ms is None or candidate_duration_ms is None:
        return None
    difference = abs(item_duration_ms - candidate_duration_ms)
    if difference <= DURATION_TOLERANCE_MS:
        return 1.0
    # Within half a minute is ambiguous rather than wrong.
    if difference <= 30_000:
        return 0.5
    return 0.0


@dataclass(frozen=True, slots=True)
class MatchContext:
    """What we know about the Jellyfin item we are trying to match."""

    kind: Literal["MusicArtist", "MusicAlbum", "Audio"]
    name: str
    mbid: str | None = None
    artists: list[str] = field(default_factory=list)
    year: int | None = None
    duration_ms: int | None = None


@dataclass(frozen=True, slots=True)
class CandidateIdentity:
    """What Last.fm says it found."""

    name: str
    mbid: str | None = None
    artist: str | None = None
    year: int | None = None
    duration_ms: int | None = None


@dataclass(frozen=True, slots=True)
class ConfidenceComponent:
    """One weighted input, reported so a user can see *why* the score is what it is."""

    name: str
    value: float | None
    weight: float
    applied_weight: float
    detail: str

    @property
    def contribution(self) -> float:
        if self.value is None or self.applied_weight == 0:
            return 0.0
        return self.value * self.applied_weight


@dataclass(frozen=True, slots=True)
class Confidence:
    total: float
    verdict: Verdict
    components: list[ConfidenceComponent]
    # True when the ids prove these are different entities. The caller must require
    # explicit confirmation rather than proposing changes.
    mbid_conflict: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def accepts_by_default(self) -> bool:
        """Whether the change set may be pre-selected without the user looking."""
        return self.verdict == "auto" and not self.mbid_conflict

    @property
    def explanation(self) -> str:
        parts = [
            f"{component.name}={component.value:.2f}"
            for component in self.components
            if component.value is not None
        ]
        return ", ".join(parts) if parts else "no comparable signals"


def score(context: MatchContext, candidate: CandidateIdentity) -> Confidence:
    """Score how confident we are that ``candidate`` is ``context``.

    Weights are renormalised over the components that actually apply, so an artist
    -- which has no year or duration to compare -- is judged on the signals it does
    have rather than being capped at a fraction of the scale.
    """
    notes: list[str] = []

    mb_value, conflict = mbid_score(item_mbid=context.mbid, candidate_mbid=candidate.mbid)
    if conflict:
        notes.append(
            "MusicBrainz ids disagree, so these are different entities regardless of "
            "how alike the names are"
        )

    components = [
        ConfidenceComponent(
            name="name",
            value=comparable_name(context.name, candidate.name),
            weight=WEIGHT_NAME,
            applied_weight=0.0,
            detail=f"{context.name!r} vs {candidate.name!r}",
        ),
        ConfidenceComponent(
            name="mbid",
            value=0.0 if conflict else mb_value,
            weight=WEIGHT_MBID,
            applied_weight=0.0,
            detail=_mbid_detail(context, candidate, conflict),
        ),
    ]

    if context.kind in {"MusicAlbum", "Audio"}:
        components.append(
            ConfidenceComponent(
                name="artist",
                value=artist_agreement(
                    item_artists=context.artists, candidate_artist=candidate.artist
                ),
                weight=WEIGHT_ARTIST,
                applied_weight=0.0,
                detail=f"item artists {context.artists} vs {candidate.artist!r}",
            )
        )
        components.append(
            ConfidenceComponent(
                name="year",
                value=year_agreement(item_year=context.year, candidate_year=candidate.year),
                weight=WEIGHT_YEAR,
                applied_weight=0.0,
                detail=f"item year {context.year} vs {candidate.year}",
            )
        )

    if context.kind == "Audio":
        components.append(
            ConfidenceComponent(
                name="duration",
                value=duration_agreement(
                    item_duration_ms=context.duration_ms,
                    candidate_duration_ms=candidate.duration_ms,
                ),
                weight=WEIGHT_DURATION,
                applied_weight=0.0,
                detail=f"item {context.duration_ms}ms vs {candidate.duration_ms}ms",
            )
        )

    # A neutral MBID score is not evidence: it means the comparison was
    # inconclusive. If nothing decisive was available -- no comparable name and no
    # pair of ids -- then the total would be a misleading half-confidence derived
    # from the neutral value alone, so it is refused outright instead.
    decisive = (
        bool(context.mbid and candidate.mbid)
        or comparable_name(context.name, candidate.name) is not None
    )
    if not decisive:
        return Confidence(
            total=0.0,
            verdict="reject",
            components=components,
            mbid_conflict=conflict,
            notes=[*notes, "no comparable signals between the item and the candidate"],
        )

    applied = [component for component in components if component.value is not None]
    total_weight = sum(component.weight for component in applied)
    if total_weight == 0:
        # Nothing comparable at all: refuse rather than guess.
        return Confidence(
            total=0.0,
            verdict="reject",
            components=components,
            mbid_conflict=conflict,
            notes=[*notes, "no comparable signals between the item and the candidate"],
        )

    renormalised: list[ConfidenceComponent] = []
    for component in components:
        if component.value is None:
            renormalised.append(component)
            continue
        renormalised.append(
            ConfidenceComponent(
                name=component.name,
                value=component.value,
                weight=component.weight,
                applied_weight=component.weight / total_weight,
                detail=component.detail,
            )
        )

    total = round(sum(component.contribution for component in renormalised), 4)

    if conflict:
        # A conflict cannot be averaged away: the ids are proof, not evidence.
        verdict: Verdict = "reject"
    elif total >= AUTO_THRESHOLD:
        verdict = "auto"
    elif total >= REVIEW_THRESHOLD:
        verdict = "review"
    else:
        verdict = "reject"

    excluded = [component.name for component in components if component.value is None]
    if excluded:
        notes.append(f"not compared: {', '.join(excluded)}")

    return Confidence(
        total=total,
        verdict=verdict,
        components=renormalised,
        mbid_conflict=conflict,
        notes=notes,
    )


def _mbid_detail(context: MatchContext, candidate: CandidateIdentity, conflict: bool) -> str:
    if conflict:
        return f"conflict: item {context.mbid!r} vs candidate {candidate.mbid!r}"
    if context.mbid and candidate.mbid:
        return "ids match"
    if candidate.mbid and not context.mbid:
        return "candidate has an id, the item does not"
    if context.mbid and not candidate.mbid:
        return "the item has an id, the candidate does not"
    return "neither side has an id"
