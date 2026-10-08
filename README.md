# metaedit

Browse a Jellyfin music library, read artist / album / track metadata from the
Last.fm API, keep a persistent local archive of every Last.fm response, review a
field-level diff, and apply the accepted changes back to Jellyfin.

It is a self-hosted tool for one person with a music library and a taste for tidier
metadata — not a sync service and not a plugin.

<p align="center">
  <em>Library → candidates → diff → apply → undo.</em>
</p>

## What it does

- **Browse and search your library.** Artists, albums or songs, with a filter for
  items that are missing metadata.
- **Fetch metadata from Last.fm.** Genres and styles, tag popularity, album
  overviews, and similar artists — every response stored locally as it arrives.
- **Review a field-by-field diff before anything changes.** Nothing is written until
  you tick the fields you want, and a match that needs review writes *nothing* by
  default rather than guessing.
- **Undo any change.** Every write is snapshotted first, and one click restores the
  previous values.
- **Edit in bulk.** Review a whole selection, apply it, watch per-item progress, and
  revert the batch as a single unit.
- **Explore the archive offline.** Search everything ever fetched from Last.fm, with
  tag popularity and similar artists, without touching the network.

Four properties hold on every write, and each is tested:

1. **The payload is always the complete writable field set.** `POST /Items/{id}` is a
   full overwrite, so a field omitted from the body is nulled. Anything you did not
   select is carried through at its current value instead.
2. **A snapshot is written before the item is.** A failed write leaves a harmless
   orphaned snapshot; the reverse order would leave an unrecoverable edit.
3. **A stale `Etag` is refused** with 409 and nothing is written, so a reviewed change
   cannot silently undo an edit made in Jellyfin meanwhile.
4. **Only planned fields may be selected.** Naming any other field is a 422, because
   on a full-overwrite API a field the caller can name but we did not plan is one they
   could destroy.

Two more things are deliberate, not missing:

- **Last.fm is read-only.** One API key, no user auth, no session signing. Nothing is
  ever written back to Last.fm.
- **Related artists stay in our database.** Jellyfin has no field for them, so they are
  stored and displayed but never written to your library.

## Quickstart

You need Docker with Compose, a Jellyfin server, and a Last.fm API key.

1. **A Jellyfin administrator API key** — Dashboard → API Keys. Metadata writes are
   gated on the `RequiresElevation` policy, so a non-admin key can browse but not save;
   `GET /api/info` tells you which one you have.
2. **A Last.fm API key** — from <https://www.last.fm/api/account/create>.

```bash
git clone https://github.com/sredevopsorg/metaedit.git
cd metaedit

cp .env.example .env
# edit .env: JELLYFIN_URL, JELLYFIN_API_KEY, LASTFM_API_KEY, POSTGRES_PASSWORD

docker compose up -d --build
```

Then open <http://127.0.0.1:8080>.

The stack runs Postgres, applies migrations as a one-shot `migrate` service, and only
then starts the app. The first visit is the Library screen: search for something, open
an item, fetch its Last.fm candidates, and review the diff.

> **It binds to `127.0.0.1` on purpose.** The app holds an administrator Jellyfin key
> and has no authentication of its own. Put an authenticating proxy in front of it
> before exposing it anywhere.

Postgres 18 is required: the archive uses declarative monthly partitioning. Jellyfin
10.9 or newer is required for the item-update semantics the write path depends on.

## Documentation

| Doc | For |
|---|---|
| [Development](docs/development.md) | Running from a checkout, checks, tests, the API contract |
| [API](docs/api.md) | Every endpoint, and the rules the write path follows |
| [Operations](docs/operations.md) | The archive, the CLI, retention, live-verified server behaviour |
| [Architecture](docs/architecture.md) | Status, the UI, layering, known deviations |
| [ADRs](docs/adr/README.md) | Why the design is what it is |
| [Design notes](docs/design/README.md) | Implementation contracts fixed ahead of a phase |

## Attribution

Metadata originates from Last.fm. The UI links every Last.fm-derived value back to the
corresponding artist, album or track page on <https://www.last.fm>, as the
[API Terms of Service](https://www.last.fm/api/tos) require. This tool is for personal,
non-commercial use.
