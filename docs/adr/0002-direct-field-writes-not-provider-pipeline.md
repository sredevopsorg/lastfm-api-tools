# 0002. Write fields directly; do not use Jellyfin's metadata provider pipeline

- **Status:** Accepted
- **Date:** 2026-10-06

## Context

Jellyfin fetches metadata through ordered, per-library providers. Metadata can be
pushed to an item either by configuring a provider and calling
`POST /Items/{itemId}/Refresh`, or by writing the fields ourselves with
`POST /Items/{itemId}`.

## Decision

Write fields directly with `UpdateItem`. Do not register, configure, or rely on a
Jellyfin metadata provider.

## Consequences

- The change set is deterministic and reviewable before anything is written: we
  know exactly which fields will change and to what values.
- We do not depend on per-library provider configuration
  (`MetadataFetcherOrder`, `DisabledMetadataFetchers`), which is invisible to the
  API consumer and easy to get wrong.
- Hand-curated values are never silently overwritten by a background refresh,
  because no background provider is involved.
- `POST /Items/{itemId}/Refresh` remains available but is exposed as a separate,
  clearly labelled action rather than as the mechanism that applies our edits.
- Image/artwork handling stays out of scope; the provider pipeline is where
  artwork fetching normally lives.

## Alternatives rejected

- **A provider plus `Refresh` with `replaceAllMetadata=true`.** Rejected: it
  replaces far more than requested, depends on server-side ordering we do not
  control, and produces a diff we cannot show the user in advance.
