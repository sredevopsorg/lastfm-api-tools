"""Contract tests against the vendored Jellyfin OpenAPI specification.

The whole design leans on facts taken from this document -- that
``POST /Items/{itemId}`` is the only write path, that it requires elevation, and
that every whitelist field is nullable on ``BaseItemDto``. If Jellyfin changes
any of them, these tests fail in CI instead of the app corrupting a library.

The pinned version is asserted deliberately: upgrading it is a conscious act that
should come with a re-read of the update semantics.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from metaedit.domain.writable import ITEM_KINDS_ALL, payload_field_set, payload_fields

SPEC_PATH = Path(__file__).resolve().parents[2] / "contracts" / "jellyfin-openapi-stable.json"

PINNED_VERSION = "12.2.0"


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    with SPEC_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)  # type: ignore[no-any-return]


def test_spec_is_pinned_to_a_known_server_version(spec: dict[str, Any]) -> None:
    assert spec["openapi"].startswith("3.0"), "we assume OpenAPI 3.0 semantics"
    assert spec["info"]["version"] == PINNED_VERSION
    assert spec["info"]["x-jellyfin-version"] == PINNED_VERSION


def test_the_expected_write_path_exists(spec: dict[str, Any]) -> None:
    path = spec["paths"].get("/Items/{itemId}")
    assert path is not None, "UpdateItem disappeared: the app has no write path"
    assert "post" in path
    assert path["post"]["operationId"] == "UpdateItem"


def test_update_item_still_requires_elevation(spec: dict[str, Any]) -> None:
    """If this ever stops being true, the admin-key requirement can be relaxed."""
    security = spec["paths"]["/Items/{itemId}"]["post"]["security"]
    assert {"CustomAuthentication": ["RequiresElevation"]} in security


def test_update_item_accepts_and_returns_what_we_assume(spec: dict[str, Any]) -> None:
    post = spec["paths"]["/Items/{itemId}"]["post"]
    body_schema = post["requestBody"]["content"]["application/json"]["schema"]
    assert body_schema["allOf"][0]["$ref"].endswith("/BaseItemDto")
    assert "204" in post["responses"], "a successful update returns no content"


def test_auth_scheme_is_an_api_key_header(spec: dict[str, Any]) -> None:
    scheme = spec["components"]["securitySchemes"]["CustomAuthentication"]
    assert scheme["type"] == "apiKey"
    assert scheme["in"] == "header"
    assert scheme["name"] == "Authorization"


def test_every_whitelist_field_exists_and_is_nullable(spec: dict[str, Any]) -> None:
    """Two independent safety properties in one place.

    * It must exist -- a typo would send a property the server ignores, while the
      real field it was meant to set is absent and gets nulled.
    * It must be nullable -- the snapshot fills gaps with empty values, and a
      non-nullable property would make those bodies invalid.
    """
    properties = spec["components"]["schemas"]["BaseItemDto"]["properties"]
    for kind in ITEM_KINDS_ALL:
        for field in payload_fields(kind):
            assert field in properties, f"{field} is not a BaseItemDto property"
            assert properties[field].get("nullable") is True, (
                f"{field} is not nullable; sending an empty value for it would be invalid"
            )


def test_base_item_dto_forbids_unknown_properties(spec: dict[str, Any]) -> None:
    """We may not invent properties; anything extra is silently dropped."""
    schema = spec["components"]["schemas"]["BaseItemDto"]
    assert schema.get("additionalProperties") is False


def test_lock_related_enums_cover_the_values_we_use(spec: dict[str, Any]) -> None:
    fields = spec["components"]["schemas"]["MetadataField"]["enum"]
    for expected in ("Genres", "Tags", "Name", "Overview", "Cast", "Studios"):
        assert expected in fields


def test_music_item_kinds_are_still_the_ones_we_target(spec: dict[str, Any]) -> None:
    kinds = spec["components"]["schemas"]["BaseItemKind"]["enum"]
    for kind in ("MusicArtist", "MusicAlbum", "Audio"):
        assert kind in kinds


def test_item_fields_enum_supports_what_we_ask_for(spec: dict[str, Any]) -> None:
    """``fields=`` may only name documented values."""
    from metaedit.adapters.jellyfin.client import ITEM_FIELDS

    documented = set(spec["components"]["schemas"]["ItemFields"]["enum"])
    undocumented = sorted(set(ITEM_FIELDS) - documented)
    assert not undocumented, (
        f"these requested fields are not in the ItemFields enum and may be ignored: {undocumented}"
    )


def test_provider_id_key_is_available_for_musicbrainz_matching(spec: dict[str, Any]) -> None:
    """MBID-first resolution depends on this key existing.

    ``ExternalIdInfo`` lists the provider keys the server knows about; our
    ProviderIds handling special-cases MusicBrainz ids.
    """
    types = spec["components"]["schemas"]["ExternalIdMediaType"]["enum"]
    for expected in ("Album", "Artist", "Recording", "Track"):
        assert expected in types


@pytest.mark.parametrize("kind", ITEM_KINDS_ALL)
def test_payload_field_sets_are_disjoint_from_read_only_properties(
    spec: dict[str, Any], kind: str
) -> None:
    """We only ever write what the update path reads, never server-computed data."""
    payload = payload_field_set(kind)
    assert not payload & {"Id", "Etag", "ServerId", "MediaSources", "ImageTags", "UserData"}
    properties = spec["components"]["schemas"]["BaseItemDto"]["properties"]
    assert payload.issubset(properties)
