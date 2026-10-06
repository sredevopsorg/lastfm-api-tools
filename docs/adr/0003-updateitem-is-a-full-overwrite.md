# 0003. Treat `UpdateItem` as a full overwrite with a fixed write whitelist

- **Status:** Accepted
- **Date:** 2026-10-06

## Context

`POST /Items/{itemId}` is the only metadata write path in the Jellyfin API. Its
implementation assigns every field unconditionally, for example
`item.Name = request.Name`, `item.Genres = request.Genres`,
`item.ProviderIds = request.ProviderIds`. It is not a JSON Merge Patch: a field
omitted from the body is written as null or empty.

Some fields also have surprising side effects. Tags written to a `MusicAlbum`
are unioned onto and subtracted from every child track. Setting `LockData` on a
folder cascades recursively to all children. `ArtistItems`/`AlbumArtists` are the
write source for the internal `Artists`/`AlbumArtists` arrays, so an incomplete
list drops artist links.

## Decision

Define a single explicit `WRITABLE_FIELDS` whitelist and always send a body that
contains **every** key in it. The body is built from a fresh read of the item,
with accepted changes layered on top; every rejected key carries its current
value verbatim.

Invariant, asserted in the domain layer and in a contract test:

```
set(payload) == set(WRITABLE_FIELDS)
```

`ArtistItems`, `AlbumArtists` and `Album` are excluded from the whitelist for v1.

## Consequences

- An apply can never null out a field the user did not touch.
- Every apply needs a read first, so an apply is at least two round trips.
- Adding a field to the editor means adding it to one place, the whitelist.
- Round-tripping read-only properties is unavoidable; the contract test asserts
  the body still validates against the vendored OpenAPI schema.

## Alternatives rejected

- **Send only the changed fields (a partial body).** Rejected outright: it
  silently destroys every other field.
- **Round-trip the entire ~155-property `BaseItemDto`.** Rejected: it would push
  server-computed values (counts, image tags, media sources) back at the server,
  and the blast radius of a mistake is the whole item.
