"""Response models for the API.

These exist so the API contract is real rather than implied. The endpoints that
returned ``dict[str, Any]`` produced an OpenAPI document with empty response schemas,
which meant the frontend had to hand-write matching TypeScript interfaces -- and a
hand-written duplicate of a contract drifts from it silently.

Defining the shapes here makes three things true at once: the endpoints validate what
they return, ``/openapi.json`` documents it, and the SPA's types are *generated* from
that document (``apps/web/src/api/schema.gen.ts``) rather than copied.

The shapes are also the UI's vocabulary: a diff is a list of proposed field changes
with a reason each, because the operator has to decide per field.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ------------------------------------------------------------------ confidence


class ConfidenceComponentView(BaseModel):
    """One weighted component of a match score, with its contribution."""

    name: str
    # None means the component could not be compared at all, which is deliberately
    # different from a zero score.
    value: float | None
    applied_weight: float
    detail: str


class ConfidenceView(BaseModel):
    total: float
    verdict: Literal["auto", "review", "reject"]
    accepts_by_default: bool
    # True when both sides carry a MusicBrainz id and they disagree: conclusive
    # evidence of a different entity, whatever the names say.
    mbid_conflict: bool
    components: list[ConfidenceComponentView]
    notes: list[str]


# ------------------------------------------------------------------ candidates


class CandidateTagView(BaseModel):
    name: str
    # None where Last.fm supplied no count, which is not the same as a count of zero.
    count: int | None


class CandidateView(BaseModel):
    entity_kind: Literal["artist", "album", "track"]
    entity_id: int
    identity: str
    name: str
    mbid: str | None
    artist: str | None
    year: int | None
    duration_ms: int | None
    url: str | None
    listeners: int | None
    playcount: int | None
    tags: list[CandidateTagView]
    has_overview: bool = False
    last_seen_at: str | None
    latest_response_id: str | None
    matched_on: Literal["mbid", "name"]
    confidence: ConfidenceView


class CandidatesResponse(BaseModel):
    item_id: str
    kind: str
    name: str
    etag: str | None
    candidates: list[CandidateView]
    count: int
    note: str


# ------------------------------------------------------------------------ diff


class FieldChangeView(BaseModel):
    """One proposed change to one writable field."""

    field: str
    current: Any
    proposed: Any
    mode: str
    # Why this field is being changed, in the operator's terms.
    reason: str
    source: str
    selected: bool
    # False when the value already matches, so ticking it would be a no-op write.
    changes_anything: bool
    # Set when Jellyfin has the field locked; nothing may be written to it.
    withheld_reason: str | None
    provenance: dict[str, Any]


class CandidateSummary(BaseModel):
    name: str
    mbid: str | None
    artist: str | None
    url: str | None
    year: int | None
    response_id: str | None


class DiffResponse(BaseModel):
    item_id: str
    kind: str
    name: str
    etag: str | None
    locked_fields: list[str]
    confidence: ConfidenceView
    candidate: CandidateSummary
    changes: list[FieldChangeView]
    withheld: list[FieldChangeView]
    # What a caller may write without reviewing further. Empty for anything that needs
    # review, so an unreviewed apply writes nothing.
    default_selection: list[str]


class ApplyResponse(BaseModel):
    item_id: str
    kind: str
    applied: list[str]
    unchanged: list[str]
    snapshot_id: int | None
    etag_before: str | None
    etag_after: str | None
    duration_ms: int
    # The state re-read from Jellyfin after the write, so the response reports what the
    # server holds rather than what we sent.
    state: dict[str, Any] | None
    idempotency_key: str | None


class SnapshotSummary(BaseModel):
    id: int
    kind: str
    name: str | None
    source_op: str
    created_at: str | None
    etag: str | None
    field_count: int


class SnapshotListResponse(BaseModel):
    item_id: str
    snapshots: list[SnapshotSummary]
    count: int


# --------------------------------------------------------------------- archive


class ArchiveTagView(BaseModel):
    name: str
    rank: int
    count: int | None


class ArchiveSimilarityView(BaseModel):
    name: str
    mbid: str | None
    match: float | None
    rank: int


class ArchiveEntityBase(BaseModel):
    """Fields every archived entity has, whichever table it came from."""

    model_config = ConfigDict(populate_by_name=True)

    id: int
    identity: str
    kind: str
    name: str
    mbid: str | None
    url: str | None
    listeners: int | None
    playcount: int | None
    overview: str | None
    first_seen_at: str | None
    last_seen_at: str | None
    latest_response_id: str
    # Kind-specific columns, so one response model can serve all three tables without
    # pretending they share a schema.
    extra: dict[str, Any]


class ArchiveEntitySummary(ArchiveEntityBase):
    """A list row: tags are bare names, because the list has no room for counts."""

    tags: list[str]


class ArchiveEntityDetail(ArchiveEntityBase):
    """One entity, with tags and similarity expanded.

    Deliberately *not* a subclass of the summary. Its ``tags`` are objects with rank and
    count where the summary's are strings, and narrowing a field's type in a subclass is
    not a legal override -- mypy rejected it, which is the correct answer rather than an
    inconvenience. Two shapes that differ this much are two models.
    """

    tags: list[ArchiveTagView]
    similar: list[ArchiveSimilarityView]


class ArchiveEntityListResponse(BaseModel):
    """A page of archived entities.

    The field names match what the endpoint has always returned. Writing them from
    memory instead of reading the code produced a model that rejected every real
    response -- which is precisely why these shapes are worth declaring.
    """

    items: list[ArchiveEntitySummary]
    total: int
    page: int
    page_size: int
    sort: str = "name"
    order: str = "asc"
    # How many pages `total` and `page_size` imply, so a client does not have to do the
    # arithmetic that goes wrong when the last page is partial.
    pages: int = 1


class ArchiveAliasView(BaseModel):
    """A recorded Last.fm autocorrect.

    ``requested`` is the normalised name as asked, ``canonical_name`` what Last.fm
    answered with, so a known misspelling can skip a request next time.
    """

    requested: str
    canonical_name: str | None
    canonical_artist_id: int | None
    kind: str | None


class ArchiveAliasListResponse(BaseModel):
    aliases: list[ArchiveAliasView]
    total: int


class ArchiveTagCountView(BaseModel):
    name: str
    norm: str | None
    # How many distinct entities carry this tag, which is what makes it a candidate for
    # a controlled genre vocabulary.
    entity_count: int


class ArchiveTagListResponse(BaseModel):
    tags: list[ArchiveTagCountView]
    total: int


class DerivedCounts(BaseModel):
    counts: dict[str, int]
    total: int
    newest_observation_at: str | None


class PartitionUsage(BaseModel):
    name: str
    rows: int


class ArchiveStatView(BaseModel):
    name: str
    value: Any


class ReadMetricsView(BaseModel):
    hits: int
    misses: int
    decisions: int
    hit_ratio: float | None
    # "process" because the counters are in memory and reset on restart.
    scope: str
    note: str


class ArchiveStatsResponse(BaseModel):
    payload_bytes: int
    cap_bytes: int
    warn_bytes: int
    used_ratio: float
    headroom_bytes: int
    state: Literal["ok", "warning", "cap_reached"]
    response_rows: int
    observations: int
    request_rows: int
    oldest_request_at: str | None
    # Requests not accounted for by an observation: a healthy archive has none.
    stray_archive_reads: int
    reads: ReadMetricsView
    derived: DerivedCounts
    partitions: list[PartitionUsage]


class ReindexCounts(BaseModel):
    artists: int
    albums: int
    tracks: int
    tag_edges: int
    similarities: int
    aliases: int
    entity_tags: int
    observations: int
    response_bodies: int
    # A non-zero count here is the cue to look at which method produced a shape the
    # derivation could not read.
    unexpected_shapes: int
    expected_no_envelope: int


class ReindexResponse(BaseModel):
    dry_run: bool
    counts: ReindexCounts
    duration_ms: int
    # Per-table id changes, so a rebuild's effect is visible before it is accepted.
    changes: dict[str, Any]


# ------------------------------------------------------------------ library


class LibraryListView(BaseModel):
    libraries: list[Any]
    count: int


# ----------------------------------------------------------------- bulk events


class BulkItemEvent(BaseModel):
    type: Literal["item"] = "item"
    index: int
    item_id: str
    name: str
    kind: str
    skipped_reason: str | None
    applicable: bool
    diff: DiffResponse


class BulkAppliedEvent(BaseModel):
    type: Literal["applied"] = "applied"
    index: int
    item_id: str
    name: str
    applied_fields: list[str]
    snapshot_id: int | None


class BulkFailedEvent(BaseModel):
    type: Literal["failed"] = "failed"
    index: int | None
    item_id: str
    name: str | None
    error: str
    error_code: str | None


class BulkRevertedEvent(BaseModel):
    type: Literal["reverted"] = "reverted"
    item_id: str
    restored_fields: list[str]


class BulkSummaryEvent(BaseModel):
    type: Literal["summary"] = "summary"
    job_id: str | None
    batch_id: str | None
    items: int | None
    applicable: int | None
    skipped: int | None
    applied: int | None
    failed: int | None
    reverted: int | None
    failures: list[dict[str, Any]]
    batch_revert: str | None
    # How many items the selection *dropped* on the way in -- by an exclusion pattern, in
    # practice. Declared here because the response model silently discards an undeclared
    # key, which is how `removing` went missing from the job list once already.
    excluded: int | None = None
    # Only present when the library had to be read to answer the selection. `truncated`
    # means the read stopped at the cap, so fewer items were considered than exist.
    scanned: int | None = None
    truncated: bool | None = None


class BulkErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str


class BulkJobSummary(BaseModel):
    job_id: str
    batch_id: str
    items: int
    applicable: int
    skipped: int
    created_at: str
    applied: bool
    # Zero for a job whose selection dropped nothing, which is most of them. A default
    # rather than a required field so the list endpoint keeps describing jobs that were
    # reviewed before this existed.
    excluded: int = 0


class RemovalJobSummary(BulkJobSummary):
    """A reviewed removal, which additionally knows *what* it would remove.

    A subclass rather than a reuse of ``BulkJobSummary``: the extra field is what makes the
    list useful -- "which genre was this batch about" is the first thing an operator asks
    when deciding whether to apply it. Returning the service's raw dict instead would have
    had the field silently dropped by the parent response model, which is exactly the class
    of drift these models exist to prevent.
    """

    removing: str | None = None


class RemovalJobListResponse(BaseModel):
    jobs: list[RemovalJobSummary]
    count: int


class GenreVocabularyResponse(BaseModel):
    """The genre values the library actually uses."""

    genres: list[str]
    count: int
    note: str


class BulkJobListResponse(BaseModel):
    jobs: list[BulkJobSummary]
    count: int


# ------------------------------------------------------------ removal events


class RemovalOutcomeView(BaseModel):
    """What removing one target from one field of one item would do.

    Mirrors ``domain.genre_removal.RemovalOutcome.as_dict()``. Declared rather than left as
    a bare dict because the UI renders ``removed`` and ``emptied`` by name: a shape the
    document does not describe is one the SPA has to guess at, and a guess cannot be
    checked against the server.
    """

    field: str
    target: str
    before: list[str]
    after: list[str]
    removed: list[str]
    changed: bool
    emptied: bool


class RemovalItemEvent(BulkItemEvent):
    """One item in a removal diff, carrying the per-field outcomes.

    Extends ``BulkItemEvent`` rather than replacing it, so the shared fields -- and the
    ``diff`` the review UI already knows how to render -- keep one definition.
    """

    removals: list[RemovalOutcomeView] = Field(default_factory=list)


class EmptiedField(BaseModel):
    """A field a removal would leave empty, named so the UI can warn about it."""

    item_id: str
    name: str
    field: str


class RemovalSummaryEvent(BulkSummaryEvent):
    """The closing frame of a removal run, which additionally knows the target."""

    removing: str | None = None
    emptied: list[EmptiedField] = Field(default_factory=list)


# A discriminated union over the frames a removal stream can emit. Separate from
# ``BulkStreamEvent`` because the item frame carries ``removals`` and the summary carries
# ``emptied``; reusing the bulk union would document both streams as lacking the fields
# that are the entire point of the removal one.
RemovalStreamEvent = Annotated[
    RemovalItemEvent
    | BulkAppliedEvent
    | BulkFailedEvent
    | BulkRevertedEvent
    | RemovalSummaryEvent
    | BulkErrorEvent,
    Field(discriminator="type"),
]


# --------------------------------------------------------------- stream events

# A discriminated union so one schema documents every frame a bulk stream can emit.
# Without a type that an endpoint references, FastAPI omits these from the document
# entirely -- the event models existed but were invisible to the SPA, which is why its
# bulk progress handler had nothing to type against.
BulkStreamEvent = Annotated[
    BulkItemEvent
    | BulkAppliedEvent
    | BulkFailedEvent
    | BulkRevertedEvent
    | BulkSummaryEvent
    | BulkErrorEvent,
    Field(discriminator="type"),
]


# -------------------------------------------------------------------- harvesting


class SearchAlternativeView(BaseModel):
    """A Last.fm search hit, offered when the derived query missed.

    A miss that says only "not found" leaves the operator with nothing to do; these give
    them something to pick.
    """

    name: str
    artist: str | None
    url: str | None
    listeners: int | None


class HarvestItemResponse(BaseModel):
    """The outcome of fetching Last.fm data for one item."""

    item_id: str
    name: str
    kind: str
    # What we asked Last.fm for, so a wrong lookup is diagnosable rather than mysterious.
    derived_query: dict[str, str | None]
    found: bool
    methods: list[str]
    # Content ids of the stored bodies: every written value can be traced to one.
    response_ids: list[str]
    from_archive: int
    error: str | None
    error_code: str | None
    alternatives: list[SearchAlternativeView]
    # Present on the single-item endpoint, which rebuilds the derived layer inline.
    derived: dict[str, int] | None = None


class HarvestBatchResponse(BaseModel):
    """The batch summary frame.

    Per-item frames reuse ``HarvestItemResponse`` with the ``item_id``/``total`` that the
    stream adds, so one model describes both the single and the streamed shape.
    """

    items: int
    found: int
    missing: int


# ---------------------------------------------------------------- harvest events


class HarvestSearchAlternative(BaseModel):
    name: str
    artist: str | None
    url: str | None
    listeners: int | None


class HarvestItemEvent(BaseModel):
    """One harvested item, as a stream frame.

    Carries the same fields as ``HarvestItemResponse`` plus the stream's position, so the
    batch event and the single-item response cannot describe different things.
    """

    type: Literal["item"] = "item"
    index: int
    total: int
    item_id: str
    name: str
    kind: str
    derived_query: dict[str, str | None]
    found: bool
    methods: list[str]
    response_ids: list[str]
    from_archive: int
    error: str | None
    error_code: str | None
    alternatives: list[HarvestSearchAlternative]


class HarvestSummaryEvent(BaseModel):
    type: Literal["summary"] = "summary"
    items: int
    found: int
    missing: int


class HarvestReindexedEvent(BaseModel):
    """The derived layer was rebuilt, which is what makes the fetched data usable."""

    type: Literal["reindexed"] = "reindexed"
    artists: int
    albums: int
    tracks: int
    tag_edges: int
    similarities: int
    aliases: int
    entity_tags: int
    observations: int
    response_bodies: int
    unexpected_shapes: int
    expected_no_envelope: int


class HarvestErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str


HarvestStreamEvent = Annotated[
    HarvestItemEvent | HarvestSummaryEvent | HarvestReindexedEvent | HarvestErrorEvent,
    Field(discriminator="type"),
]
