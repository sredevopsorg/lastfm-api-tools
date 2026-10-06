"""Entity identity keys for the derived layer.

`docs/design/0003-derivation-and-reindex.md` §2 fixes these rules. Two things here
are easy to get wrong and both have bitten this project's design already:

* **Composite keys need escaping.** Last.fm names and tags are user-generated
  text, so a control character *can* appear in one. Without escaping, an artist
  literally named ``"A\\x1fB"`` produces the same key as the pair ``("A", "B")``
  and merges two unrelated entities.
* **Identity must be a pure function of the raw rows.** It is the uniqueness key
  of a derived table, so it cannot depend on insertion order or on the clock.

An entity is therefore identified by its MBID when Last.fm supplied one, and by
an escaped name key otherwise. ``candidate_keys`` exists because an entity that
gains an MBID later is first known by a name key, and the later MBID-keyed group
has to be able to recognise and absorb it.
"""

from __future__ import annotations

from typing import Literal

from metaedit.adapters.lastfm.canonical import normalize_name

EntityKind = Literal["artist", "album", "track"]

# ASCII unit separator: the field delimiter for composite keys. It is escaped out
# of the components so it can only ever be a delimiter.
FIELD_SEPARATOR = "\x1f"

MBID_PREFIX = "mbid:"
NAME_PREFIX = "name:"
ALBUM_PREFIX = "album:"
TRACK_PREFIX = "track:"


def escape(component: str | None) -> str:
    """Percent-encode the delimiter and the escape character itself.

    ``%`` is encoded first, so the mapping is injective: distinct component pairs
    always produce distinct keys, and no component can forge a delimiter.
    """
    if not component:
        return ""
    return component.replace("%", "%25").replace(FIELD_SEPARATOR, "%1F")


def _join(prefix: str, *parts: str | None) -> str:
    return prefix + FIELD_SEPARATOR.join(escape(part) for part in parts)


def artist_identity(*, mbid: str | None, name: str | None) -> str:
    """``""`` when there is nothing to identify by.

    A degenerate key is refused rather than returned: ``"name:"`` is a key that a
    row could occupy, so every unnamed entity in the archive would collide into one
    derived row. Callers skip an empty identity instead.
    """
    if mbid:
        return MBID_PREFIX + escape(mbid)
    normalized = normalize_name(name)
    if not normalized:
        return ""
    return _join(NAME_PREFIX, normalized)


def album_identity(*, mbid: str | None, artist: str | None, name: str | None) -> str:
    if mbid:
        return MBID_PREFIX + escape(mbid)
    normalized = normalize_name(name)
    if not normalized:
        return ""
    return _join(ALBUM_PREFIX, normalize_name(artist), normalized)


def track_identity(*, mbid: str | None, artist: str | None, name: str | None) -> str:
    if mbid:
        return MBID_PREFIX + escape(mbid)
    normalized = normalize_name(name)
    if not normalized:
        return ""
    return _join(TRACK_PREFIX, normalize_name(artist), normalized)


def key_for(kind: EntityKind, **kwargs: str | None) -> str:
    """Dispatch on entity kind, so callers do not duplicate the branching."""
    if kind == "artist":
        return artist_identity(mbid=kwargs.get("mbid"), name=kwargs.get("name"))
    if kind == "album":
        return album_identity(
            mbid=kwargs.get("mbid"), artist=kwargs.get("artist"), name=kwargs.get("name")
        )
    return track_identity(
        mbid=kwargs.get("mbid"), artist=kwargs.get("artist"), name=kwargs.get("name")
    )


def candidate_keys(
    kind: EntityKind, *, mbid: str | None, artist: str | None, name: str | None
) -> set[str]:
    """Every key this entity could currently be known by.

    Used to absorb a name-keyed row into the MBID-keyed row that supersedes it, so
    an entity that gains an MBID later resolves to one row instead of two.
    """
    keys = {key_for(kind, mbid=mbid, artist=artist, name=name)}
    if mbid:
        # It may already exist under its name from before the MBID appeared.
        keys.add(key_for(kind, mbid=None, artist=artist, name=name))
    return {key for key in keys if key}


def is_mbid_key(identity: str) -> bool:
    return identity.startswith(MBID_PREFIX)


def unescape(component: str) -> str:
    """Inverse of ``escape``. Only used for display and tests."""
    return component.replace("%1F", FIELD_SEPARATOR).replace("%25", "%")
