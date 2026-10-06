# 0012. Read-only Last.fm usage: API key only, no user auth flow

- **Status:** Accepted
- **Date:** 2026-10-06

## Context

The Last.fm API offers `artist.addTags`, `album.addTags` and `track.addTags`,
which require the full authentication protocol (`auth.getToken`,
`auth.getSession`, `api_sig` request signing with a shared secret, and a user
session key). The read methods we need require only an API key.

## Decision

Use Last.fm read-only. Data flows one way: Last.fm to Jellyfin. No user
authentication, no session keys, no request signing.

## Consequences

- The API surface is a couple of GET methods; there is no token storage, no
  session lifecycle, no signing implementation to get wrong, and no user
  credentials in the system at all.
- The user's Last.fm tagging history is never modified. Last.fm tags are
  community data, so personal edits there would be both surprising and rude.
- Configuration is one secret, `LASTFM_API_KEY`, which is created
  per-application at `last.fm/api/account/create`.
- Read methods are also subject to attribution and 100 MB storage terms, which
  ADR 0011 and the UI's "Source: Last.fm" links address.

## Alternatives rejected

- **Write tags back to Last.fm.** Rejected: it would multiply the auth surface for
  a feature nobody asked for, and it pollutes shared community metadata.
