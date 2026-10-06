# Architecture Decision Records

Short, immutable records of the decisions that shaped this project. Each entry
states context, the decision, its consequences, and the alternatives rejected.
A record is superseded by a new one (status change), never rewritten.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-custom-app-over-jellyfin-plugin.md) | Custom app calling the Jellyfin REST API, not a Jellyfin plugin | Accepted |
| [0002](0002-direct-field-writes-not-provider-pipeline.md) | Write fields directly; do not use Jellyfin's metadata provider pipeline | Accepted |
| [0003](0003-updateitem-is-a-full-overwrite.md) | Treat `UpdateItem` as a full overwrite with a fixed write whitelist | Accepted |
| [0004](0004-snapshot-before-every-write.md) | Snapshot the writable fields before every write; revert replays it | Accepted |
| [0005](0005-related-artists-in-our-db-only.md) | Related artists live in our DB only, never written to Jellyfin | Accepted |
| [0006](0006-styles-map-to-tags.md) | Map Last.fm styles onto `BaseItemDto.Tags`; genres onto `Genres` | Accepted |
| [0007](0007-optimistic-concurrency-on-etag.md) | Optimistic concurrency on `Etag`/`DateLastSaved` | Accepted |
| [0008](0008-persistent-lastfm-archive.md) | Persist every Last.fm request and response in a permanent archive | Accepted |
| [0009](0009-raw-archive-is-source-of-truth.md) | The raw archive is the source of truth; derived data is rebuildable | Accepted |
| [0010](0010-content-addressed-responses.md) | Content-address response bodies; log every observation separately | Accepted |
| [0011](0011-measure-dont-silently-prune.md) | Measure the ToS cap; never silently delete archive data | Accepted |
| [0012](0012-read-only-lastfm.md) | Read-only Last.fm usage: API key only, no user auth flow | Accepted |
| [0013](0013-single-container-and-postgres-18.md) | Single container serving the SPA; Postgres 18 pinned as `18-trixie` | Accepted |
