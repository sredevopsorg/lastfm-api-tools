# 0014. `PremiereDate` is a wire string everywhere outside comparison

- **Status:** Accepted
- **Date:** 2026-10-08

## Context

`PremiereDate` is the one writable field whose JSON representation and useful
Python representation differ. Jellyfin sends it as an ISO-8601 string with
sub-second precision (`1992-01-01T03:00:00.0000000Z`); the natural type to compare
and display it as is `datetime`. `NormalizedItem.fields` therefore held a
`datetime`, and `to_payload` converted it back to a string — but **only for fields
present in `changes`**. A field carried through from current state was emitted
verbatim.

That one asymmetry produced three separate `TypeError: Object of type datetime is
not JSON serializable` failures, each on a different boundary:

1. **The snapshot flush.** `snapshot_fields` copied `fields` into a JSONB column.
   JSON has no datetime. Every apply of an item with a `PremiereDate` failed,
   *after* the change set had been reviewed and confirmed and *before* anything
   was written. Measured on a live library this is 5,916 of 6,583 items — 470 of
   502 albums and 5,437 of 5,442 songs. Artists mostly have no `PremiereDate`
   (9 of 639), which is why it was first reported as one artist rather than as
   everything.
2. **The write payload.** `to_payload` emitted the unconverted value for
   unchanged fields, so the httpx `json=` encoder raised the same error while
   building the request.
3. **The error body** (fixed earlier, v0.0.4). `as_state()["fields"]` exposed the
   datetime to `JSONResponse`, whose plain `json.dumps` rejected it.

Fixing (1) and (2) separately is what made (2) visible only after (1) was fixed.
The pattern is a representation choice leaking across three boundaries, so the
fix belongs at the representation, not at the three destinations.

## Decision

`_normalize` — the single door into `fields` — returns the **wire** form for every
field, including `PremiereDate`. `fields` never holds a value that cannot be
JSON-encoded. `to_payload` and `snapshot_fields` still call `_to_wire`, which is
now idempotent rather than load-bearing, so neither can be the only place a
conversion happens.

Comparison against a local date is no longer done on `fields`. Where a date
comparison is needed, parse with `_as_datetime`.

## Consequences

- No path can put a non-serialisable value into a JSONB column or an HTTP body.
  The two failure modes that reached users are structurally impossible, not
  merely fixed.
- `GET /items/{id}/state` returns `PremiereDate` as an ISO string rather than a
  datetime. That is a public response change; the previous shape was a latent 500
  in any handler that echoed it, so the new shape is the one that always worked.
- Reading a snapshot back is unchanged: `from_snapshot_row` already parsed the
  stored string.
- A test asserting the *old* representation as a premise had to be retired and
  retargeted (`test_error_body_serialization.py`). The conversion it justified —
  `to_jsonable` — is still required, because `BaseItemDto` still parses
  `PremiereDate` into a datetime and that value can reach an error detail.

## Known limitation, measured not assumed

**Jellyfin discards the time component of `PremiereDate` on every write.** Verified
on 12.2.0: writing `1987-01-01T03:00:00+00:00` back to an item that already held
exactly that value returns `1987-01-01T00:00:00+00:00`. The server normalises to
midnight in its own configured timezone, and does so even for a no-op write.

This means **editing any metadata field on an item whose `PremiereDate` has a
non-midnight time will silently round that date to midnight.** The snapshot
faithfully records the pre-write value, so a revert restores what we captured —
and the revert will itself be rounded on the way in. Six artists in the reference
library are affected; the time component there is a timezone offset (UTC−3 to
UTC−5), so the date itself is unchanged and only the stored time differs.

We do not work around this:

- **Not by omitting `PremiereDate` from the payload.** The endpoint is a full
  overwrite (ADR 0003); omitting the key nulls the field.
- **Not by refusing the write.** The field is not one this application edits, and
  refusing every edit to a dated item would make most of the library uneditable.

It is recorded here because it is a real, silent, user-visible change caused by
using this tool, and the alternative to documenting it is a user discovering it
from their own library.
