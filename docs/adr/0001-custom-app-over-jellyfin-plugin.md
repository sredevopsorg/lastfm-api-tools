# 0001. Custom app calling the Jellyfin REST API, not a Jellyfin plugin

- **Status:** Accepted
- **Date:** 2026-10-06

## Context

The goal is to edit Jellyfin music metadata (genres, styles, overview, provider
ids) from Last.fm data. Jellyfin is extensible in two ways: a C# server plugin
shipped in a plugin repository, or an external client that speaks the REST API.

## Decision

Build a standalone application that talks to the Jellyfin HTTP API. Do not build
a Jellyfin plugin.

## Consequences

- Releases are independent of the Jellyfin server version; only the HTTP contract
  must hold, and that contract is pinned by a vendored OpenAPI spec plus a
  version assertion in the contract test suite.
- The whole stack is Python and TypeScript, testable without a running Jellyfin.
- We inherit HTTP's limits: `POST /Items/{itemId}` requires an elevated API key,
  and there is no way to subscribe to library change events.
- Anything a plugin could do that the HTTP API cannot (for example, a first-class
  related-artists field) is out of reach. ADR 0005 records how we handle that.

## Alternatives rejected

- **Jellyfin plugin.** Would have first-class access to the item model and could
  populate structured fields directly. Rejected: a much larger codebase, a
  mandatory plugin repository and CI, a C#/Jellyfin-internals upgrade burden, and
  slower iteration for a single-operator tool.
- **Doing nothing / hand-editing in the Jellyfin UI.** Rejected: no Last.fm data
  at all, which is the point of the tool.
