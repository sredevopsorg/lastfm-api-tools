"""Canonicalisation: request identity and response content addressing.

These functions decide what counts as "the same request" and "the same body", so
their insensitivity and their sensitivity both matter. A false duplicate serves
the wrong data; a false distinct wastes storage and requests.
"""

from __future__ import annotations

import pytest

from metaedit.adapters.lastfm.canonical import (
    api_key_fingerprint,
    body_bytes,
    canonical_body,
    canonical_params,
    content_id,
    is_error_body,
    params_hash,
)

ARTIST_BODY = {
    "artist": {
        "name": "Cher",
        "mbid": "bfcc6d75-a6a5-4bc6-8282-47aec8531818",
        "tags": {"tag": [{"name": "pop", "count": "100"}]},
    }
}


def test_params_hash_ignores_key_order() -> None:
    assert params_hash("artist.getinfo", {"artist": "Cher", "mbid": "x"}) == params_hash(
        "artist.getinfo", {"mbid": "x", "artist": "Cher"}
    )


def test_params_hash_ignores_transport_and_credentials() -> None:
    """api_key and format must not change the identity of the data."""
    baseline = params_hash("artist.getinfo", {"artist": "Cher"})
    assert (
        params_hash(
            "artist.getinfo",
            {"artist": "Cher", "api_key": "secret", "format": "json", "callback": "cb"},
        )
        == baseline
    )


def test_params_hash_ignores_method_case() -> None:
    assert params_hash("artist.getinfo", {}) == params_hash("artist.getInfo", {})


def test_params_hash_normalises_whitespace_and_unicode() -> None:
    padded = params_hash("artist.getinfo", {"artist": "  Cher   Cher  "})
    collapsed = params_hash("artist.getinfo", {"artist": "Cher Cher"})
    assert padded == collapsed

    # NFC vs NFD must fold together, or the same name hashes two ways.
    import unicodedata

    nfd = unicodedata.normalize("NFD", "Sigur Rós")
    nfc = unicodedata.normalize("NFC", "Sigur Rós")
    assert params_hash("artist.getinfo", {"artist": nfd}) == params_hash(
        "artist.getinfo", {"artist": nfc}
    )


def test_params_hash_is_case_sensitive_about_names() -> None:
    """Last.fm echoes a canonical spelling; folding case would merge two requests."""
    assert params_hash("artist.getinfo", {"artist": "cher"}) != params_hash(
        "artist.getinfo", {"artist": "Cher"}
    )


@pytest.mark.parametrize(
    "changed",
    [
        {"artist": "Cher ", "album": "Believe"},
        {"artist": "Cher", "album": "Believe Deluxe"},
        {"artist": "Cher", "album": "Believe", "mbid": "different"},
        {"artist": "Cher", "album": "Believe", "autocorrect": "0"},
    ],
)
def test_params_hash_is_sensitive_to_what_it_must_be(changed: dict[str, str]) -> None:
    baseline = params_hash(
        "album.getinfo", {"artist": "Cher", "album": "Believe", "autocorrect": "1"}
    )
    assert params_hash("album.getinfo", changed) != baseline


def test_params_hash_drops_empty_values() -> None:
    assert params_hash(
        "artist.getinfo", {"artist": "Cher", "mbid": None, "lang": ""}
    ) == params_hash("artist.getinfo", {"artist": "Cher"})


def test_canonical_params_keeps_payload_relevant_keys() -> None:
    canonical = canonical_params("artist.getinfo", {"artist": " Cher ", "limit": 20})
    assert canonical["method"] == "artist.getinfo"
    assert canonical["artist"] == "Cher"
    assert canonical["limit"] == 20


def test_content_id_is_stable_across_key_order() -> None:
    assert content_id({"a": 1, "b": 2}) == content_id({"b": 2, "a": 1})


def test_content_id_distinguishes_different_bodies() -> None:
    assert content_id(ARTIST_BODY) != content_id({**ARTIST_BODY, "extra": 1})


def test_content_id_is_a_sha256_hex_digest() -> None:
    value = content_id(ARTIST_BODY)
    assert len(value) == 64
    assert all(char in "0123456789abcdef" for char in value)


def test_content_id_is_stable_across_calls() -> None:
    """The address must not drift: it is a primary key."""
    assert content_id(ARTIST_BODY) == content_id(ARTIST_BODY)


def test_content_id_is_unicode_stable() -> None:
    """Canonical JSON must not escape non-ASCII differently between runs."""
    body = {"artist": {"name": "Sigur Rós", "bio": "ísland"}}
    assert content_id(body) == content_id({"artist": {"bio": "ísland", "name": "Sigur Rós"}})


def test_body_bytes_counts_the_canonical_form() -> None:
    assert body_bytes({"a": 1}) == len(canonical_body({"a": 1}).encode())
    assert body_bytes({"a": 1}) > 0


def test_api_key_fingerprint_is_short_and_not_the_key() -> None:
    fingerprint = api_key_fingerprint("0123456789abcdef")
    assert len(fingerprint) == 16
    assert "0123456789abcdef" not in fingerprint
    assert api_key_fingerprint("") == ""


def test_api_key_fingerprint_differs_per_key() -> None:
    assert api_key_fingerprint("key-one") != api_key_fingerprint("key-two")


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"error": 6, "message": "not found"}, True),
        ({"error": 29}, True),
        ({"artist": {}}, False),
        ({"error": "6"}, False),
    ],
)
def test_is_error_body_matches_lastfm_envelope(body: dict[str, object], expected: bool) -> None:
    assert is_error_body(body) is expected
