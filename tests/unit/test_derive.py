"""Derivation logic (docs/design/0003 §2-§6).

The properties tested here are the ones that make the derived layer trustworthy:
purity (no clock, no network), determinism, correct absorption of an entity that
gains an MBID, and honest field/tag provenance.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from metaedit.archive.derive import (
    RawObservation,
    derive_aliases,
    derive_all,
    derive_entity_tag_counts,
    derive_similarities,
    derive_tag_edges,
)

BASE = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def obs(
    body: dict[str, object],
    *,
    minutes: int = 0,
    request_id: int = 1,
    response_id: str | None = None,
    method: str = "artist.getinfo",
) -> RawObservation:
    return RawObservation(
        request_id=request_id,
        requested_at=BASE + timedelta(minutes=minutes),
        response_id=response_id or f"{request_id:064x}",
        body=body,
        method=method,
    )


def artist_body(
    *,
    name: str = "Cher",
    mbid: str | None = "mbid-cher",
    tags: list[dict[str, object]] | None = None,
    summary: str | None = "Cher is an American singer.",
) -> dict[str, object]:
    payload: dict[str, object] = {"name": name, "url": "https://www.last.fm/music/Cher"}
    if mbid:
        payload["mbid"] = mbid
    if tags is not None:
        payload["tags"] = {"tag": tags}
    if summary is not None:
        payload["bio"] = {"summary": summary, "published": "Thu, 13 Mar 2008"}
    payload["stats"] = {"listeners": "196440", "plays": "1599101"}
    return {"artist": payload}


# --------------------------------------------------------------------- purity


def test_derivation_is_deterministic_across_runs() -> None:
    """Same raw rows in, identical derived rows out."""
    observations = [
        obs(artist_body(), minutes=0, request_id=1),
        obs(artist_body(summary="Updated."), minutes=5, request_id=2),
    ]
    first = derive_all(observations)
    second = derive_all(observations)
    assert [a.as_columns() for a in first.artists] == [a.as_columns() for a in second.artists]


def test_derivation_ignores_input_order() -> None:
    """Ordering must come from raw timestamps, not from list position."""
    observations = [
        obs(artist_body(), minutes=0, request_id=1),
        obs(artist_body(summary="Newer."), minutes=5, request_id=2),
        obs(artist_body(summary="Oldest."), minutes=-5, request_id=3),
    ]
    forwards = derive_all(observations).artists[0]
    backwards = derive_all(list(reversed(observations))).artists[0]
    assert forwards.as_columns() == backwards.as_columns()
    assert forwards.overview == "Newer."


def test_derivation_uses_only_raw_timestamps() -> None:
    """No ``now()`` may leak into a derived column.

    A wall-clock value here would make the store unreproducible while looking
    perfectly healthy, which is why this is asserted rather than assumed.
    """
    observations = [
        obs(artist_body(), minutes=0, request_id=1),
        obs(artist_body(), minutes=30, request_id=2),
    ]
    entity = derive_all(observations).artists[0]
    assert entity.first_seen_at == BASE
    assert entity.last_seen_at == BASE + timedelta(minutes=30)


def test_ids_follow_a_stable_identity_order() -> None:
    """Iteration order determines primary keys, so it must be sorted."""
    observations = [
        obs(artist_body(name="Zebra", mbid=None), request_id=1),
        obs(artist_body(name="Apple", mbid=None), request_id=2),
        obs(artist_body(name="Mango", mbid=None), request_id=3),
    ]
    entities = derive_all(observations).artists
    identities = [entity.identity for entity in entities]
    assert identities == sorted(identities), "ids are assigned in identity order"
    assert [e.name for e in entities] == ["Apple", "Mango", "Zebra"]


# ----------------------------------------------------------------- grouping


def test_observations_of_one_entity_become_one_row() -> None:
    observations = [
        obs(artist_body(), minutes=0, request_id=1),
        obs(artist_body(), minutes=10, request_id=2),
        obs(artist_body(), minutes=20, request_id=3),
    ]
    result = derive_all(observations)
    assert len(result.artists) == 1
    assert result.artists[0].first_seen_at == BASE
    assert result.artists[0].last_seen_at == BASE + timedelta(minutes=20)


def test_an_entity_that_gains_an_mbid_resolves_to_one_row() -> None:
    """The regression the design document calls out explicitly.

    Earlier observations have no MBID, so the entity is first known by its name.
    A later observation carries an MBID. Naively that is two rows for one artist.
    """
    observations = [
        obs(artist_body(mbid=None), minutes=0, request_id=1),
        obs(artist_body(mbid=None), minutes=10, request_id=2),
        obs(artist_body(mbid="mbid-cher"), minutes=20, request_id=3),
    ]
    result = derive_all(observations)
    assert len(result.artists) == 1, "an entity that gains an MBID must not duplicate"
    entity = result.artists[0]
    assert entity.identity == "mbid:mbid-cher"
    assert entity.first_seen_at == BASE, "the earlier history is absorbed, not lost"
    assert entity.last_seen_at == BASE + timedelta(minutes=20)


def test_absorption_does_not_merge_different_artists() -> None:
    observations = [
        obs(artist_body(name="Cher", mbid=None), request_id=1),
        obs(artist_body(name="Madonna", mbid=None), request_id=2),
    ]
    result = derive_all(observations)
    assert {entity.name for entity in result.artists} == {"Cher", "Madonna"}


def test_an_unnamed_payload_is_skipped_not_merged() -> None:
    """A payload with nothing to key on must not become one shared entity."""
    observations = [
        obs({"artist": {"url": "x"}}, request_id=1),
        obs({"artist": {"url": "y"}}, request_id=2),
        obs(artist_body(), request_id=3),
    ]
    result = derive_all(observations)
    assert len(result.artists) == 1
    assert result.artists[0].name == "Cher"


def test_error_bodies_are_not_entities() -> None:
    observations = [
        obs({"error": 6, "message": "Invalid parameters"}, request_id=1),
        obs(artist_body(), request_id=2),
    ]
    result = derive_all(observations)
    assert len(result.artists) == 1


def test_album_and_track_payloads_do_not_become_artists() -> None:
    observations = [
        obs(artist_body(), request_id=1),
        obs({"album": {"name": "Believe", "artist": "Cher"}}, request_id=2, method="album.getinfo"),
        obs({"track": {"name": "Believe", "artist": {"name": "Cher"}}}, request_id=3),
    ]
    result = derive_all(observations)
    assert len(result.artists) == 1
    assert len(result.albums) == 1
    assert len(result.tracks) == 1


# ----------------------------------------------------------- field selection


def test_newest_observation_supplies_the_fields() -> None:
    observations = [
        obs(artist_body(summary="Old."), minutes=0, request_id=1),
        obs(artist_body(summary="New."), minutes=10, request_id=2),
    ]
    assert derive_all(observations).artists[0].overview == "New."


def test_a_removed_field_really_is_cleared() -> None:
    """Last.fm does remove data, and the raw layer keeps the previous value.

    Honouring the removal is the point: the derived layer reports what is
    currently true, and the archive can still show what it used to be.
    """
    observations = [
        obs(artist_body(summary="Present."), minutes=0, request_id=1),
        obs(artist_body(summary=None), minutes=10, request_id=2),
    ]
    entity = derive_all(observations).artists[0]
    assert entity.overview is None, "a null in the newest observation clears the value"


def test_same_second_observations_resolve_by_request_id() -> None:
    """Ties must break deterministically, or two runs could disagree."""
    observations = [
        obs(artist_body(summary="From request 1"), minutes=0, request_id=1),
        obs(artist_body(summary="From request 2"), minutes=0, request_id=2),
    ]
    entity = derive_all(observations).artists[0]
    assert entity.overview == "From request 2"
    assert entity.last_request_id == 2


def test_latest_response_id_points_at_the_chosen_observation() -> None:
    """Provenance: every derived row names the raw body it came from."""
    observations = [
        obs(artist_body(summary="Old."), minutes=0, request_id=1, response_id="a" * 64),
        obs(artist_body(summary="New."), minutes=10, request_id=2, response_id="b" * 64),
    ]
    entity = derive_all(observations).artists[0]
    assert entity.latest_response_id == "b" * 64


def test_overview_keeps_lastfm_markup_verbatim() -> None:
    """Sanitising is the mapper's job; the archive stores what Last.fm sent."""
    observations = [obs(artist_body(summary="Cher is <b>an American</b> singer."))]
    assert "<b>" in (derive_all(observations).artists[0].overview or "")


def test_track_duration_is_milliseconds() -> None:
    body = {"track": {"name": "Believe", "duration": "240000", "artist": {"name": "Cher"}}}
    assert derive_all([obs(body)]).tracks[0].duration_ms == 240000


def test_album_release_date_yields_a_year() -> None:
    body = {"album": {"name": "Believe", "artist": "Cher", "releasedate": "6 Apr 1999, 00:00"}}
    assert derive_all([obs(body)]).albums[0].production_year == 1999


@pytest.mark.parametrize("value", [None, "", "unknown", "00:00"])
def test_unparseable_release_dates_do_not_invent_a_year(value: str | None) -> None:
    body = {"album": {"name": "Believe", "artist": "Cher", "releasedate": value}}
    assert derive_all([obs(body)]).albums[0].production_year is None


def test_album_getinfo_leaves_overview_unset() -> None:
    """Album getInfo carries no wiki; inventing one would be a lie."""
    body = {"album": {"name": "Believe", "artist": "Cher"}}
    assert derive_all([obs(body)]).albums[0].overview is None


def test_track_wiki_supplies_the_overview() -> None:
    body = {
        "track": {
            "name": "Believe",
            "artist": {"name": "Cher"},
            "wiki": {"summary": "A hit single.", "published": "Sun, 27 Jul 2008"},
        }
    }
    track = derive_all([obs(body)]).tracks[0]
    assert track.overview == "A hit single."
    assert track.wiki_published == "Sun, 27 Jul 2008"


def test_track_album_reference_is_captured() -> None:
    body = {
        "track": {
            "name": "Believe",
            "artist": {"name": "Cher", "mbid": "m-cher"},
            "album": {"title": "Believe", "mbid": "m-album", "position": "1"},
        }
    }
    track = derive_all([obs(body)]).tracks[0]
    assert track.album_name == "Believe"
    assert track.album_mbid == "m-album"
    assert track.album_position == 1
    assert track.artist_mbid == "m-cher"


def test_album_tracklist_durations_are_normalised_to_milliseconds() -> None:
    """Album tracklist durations are seconds; track.getInfo uses milliseconds."""
    body = {
        "album": {
            "name": "Believe",
            "artist": "Cher",
            "tracks": {"track": [{"name": "Believe", "duration": 239, "rank": "1"}]},
        }
    }
    tracklist = derive_all([obs(body)]).albums[0].tracklist
    assert tracklist[0]["duration_ms"] == 239000
    assert tracklist[0]["rank"] == 1


def test_images_are_flattened_into_a_size_map() -> None:
    body = {
        "artist": {
            "name": "Cher",
            "image": [
                {"size": "small", "#text": "https://img/s.png"},
                {"size": "mega", "#text": "https://img/m.png"},
            ],
        }
    }
    assert derive_all([obs(body)]).artists[0].images == {
        "small": "https://img/s.png",
        "mega": "https://img/m.png",
    }


# ---------------------------------------------------------------- tag edges


def test_tag_edges_preserve_rank_and_count() -> None:
    body = artist_body(tags=[{"name": "pop", "count": "100"}, {"name": "dance", "count": "50"}])
    edges = derive_tag_edges("artist", derive_all([obs(body)]).artists)
    assert [(edge.tag_name, edge.rank, edge.count) for edge in edges] == [
        ("pop", 0, 100),
        ("dance", 1, 50),
    ]


def test_tag_count_is_never_fabricated() -> None:
    """Only artist.getTopTags supplies counts; elsewhere it must be null."""
    body = artist_body(tags=[{"name": "pop"}])
    edges = derive_tag_edges("artist", derive_all([obs(body)]).artists)
    assert edges[0].count is None


def test_tag_edges_deduplicate_case_insensitively() -> None:
    body = artist_body(tags=[{"name": "Pop"}, {"name": "pop"}, {"name": "POP"}])
    edges = derive_tag_edges("artist", derive_all([obs(body)]).artists)
    assert len(edges) == 1


def test_tag_edges_carry_identity_not_a_primary_key() -> None:
    """Ids are assigned at insert time, so the edge cannot depend on them."""
    body = artist_body(tags=[{"name": "pop"}])
    entity = derive_all([obs(body)]).artists[0]
    edges = derive_tag_edges("artist", [entity])
    assert edges[0].entity_identity == entity.identity
    assert edges[0].observed_at == entity.last_seen_at


def test_tag_edges_use_the_newest_tag_set() -> None:
    observations = [
        obs(artist_body(tags=[{"name": "old"}]), minutes=0, request_id=1),
        obs(artist_body(tags=[{"name": "new"}]), minutes=10, request_id=2),
    ]
    entities = derive_all(observations).artists
    edges = derive_tag_edges("artist", entities)
    assert [edge.tag_name for edge in edges] == ["new"], "edges are a snapshot, not a log"


# ------------------------------------------------------------- tag aggregate


def test_entity_tag_counts_distinct_entities() -> None:
    """One artist observed twice with the same tag counts once."""
    observations = [
        obs(artist_body(name="Cher", mbid=None, tags=[{"name": "pop"}]), request_id=1),
        obs(artist_body(name="Cher", mbid=None, tags=[{"name": "pop"}]), minutes=5, request_id=2),
        obs(
            artist_body(name="Madonna", mbid=None, tags=[{"name": "pop"}]), minutes=6, request_id=3
        ),
    ]
    result = derive_all(observations)
    counts = derive_entity_tag_counts(result.tag_edges)
    assert counts == [("pop", "pop", 2)], "two distinct artists carry 'pop'"


def test_entity_tag_counts_are_sorted_by_normalised_name() -> None:
    observations = [
        obs(artist_body(name="A", mbid=None, tags=[{"name": "zebra"}]), request_id=1),
        obs(artist_body(name="B", mbid=None, tags=[{"name": "apple"}]), request_id=2),
    ]
    counts = derive_entity_tag_counts(derive_all(observations).tag_edges)
    assert [norm for _, norm, _ in counts] == ["apple", "zebra"]


def test_entity_tag_counts_are_empty_without_tags() -> None:
    assert derive_entity_tag_counts([]) == []


# ----------------------------------------------------------------- similarity


def similar_body(peers: list[dict[str, object]]) -> dict[str, object]:
    """``artist.getsimilar`` as it arrives.

    The owning artist is an attribute on the container and the peers are repeated
    children. Flattened to JSON both occupy the key ``"artist"``, and converters
    disagree on which survives, so this fixture deliberately exercises the case the
    parser must survive: the attribute lost, only the peer list present. The owner
    is supplied through the request params instead, which is where it really lives.
    """
    return {"similarartists": {"artist": peers}}


def test_similarities_attach_to_the_known_artist() -> None:
    observations = [
        obs(artist_body(name="Cher", mbid=None), request_id=1),
        obs(
            similar_body([{"name": "Madonna", "match": "1", "mbid": "m-mad"}]),
            minutes=5,
            request_id=2,
            method="artist.getsimilar",
        ),
    ]
    result = derive_all(observations, similarity_owners={2: "Cher"})
    assert len(result.similarities) == 1
    similarity = result.similarities[0]
    assert similarity.artist_identity == "name:Cher"
    assert similarity.peer_name == "Madonna"
    assert similarity.peer_mbid == "m-mad"
    assert similarity.match == 1.0
    assert similarity.rank == 0


def test_similarities_are_ignored_for_an_unknown_owner() -> None:
    """We only link peers to artists we actually know about."""
    observations = [
        obs(similar_body([{"name": "Someone"}]), request_id=1, method="artist.getsimilar")
    ]
    assert derive_similarities([], observations, owners={1: "Nobody"}) == []


def test_similarities_are_skipped_when_the_owner_cannot_be_determined() -> None:
    """No owner in the params and only the peer list in the body: nothing to attach."""
    observations = [
        obs(artist_body(name="Cher", mbid=None), request_id=1),
        obs(
            similar_body([{"name": "Madonna"}]), minutes=5, request_id=2, method="artist.getsimilar"
        ),
    ]
    assert derive_all(observations).similarities == []


def test_owner_is_read_from_the_container_attribute_when_it_is_a_string() -> None:
    """Some converters keep the attribute and drop the peers; we must not crash."""
    body = {"similarartists": {"artist": "Cher"}}
    observations = [obs(artist_body(name="Cher", mbid=None), request_id=1), obs(body, request_id=2)]
    assert derive_all(observations).similarities == []


def test_similarity_ranks_follow_the_source_order() -> None:
    observations = [
        obs(artist_body(name="Cher", mbid=None), request_id=1),
        obs(
            similar_body([{"name": "A"}, {"name": "B"}, {"name": "C"}]),
            minutes=5,
            request_id=2,
            method="artist.getsimilar",
        ),
    ]
    result = derive_all(observations, similarity_owners={2: "Cher"})
    assert [(row.peer_name, row.rank) for row in result.similarities] == [
        ("A", 0),
        ("B", 1),
        ("C", 2),
    ]


def test_newest_similarity_observation_wins_per_peer() -> None:
    observations = [
        obs(artist_body(name="Cher", mbid=None), request_id=1),
        obs(
            similar_body([{"name": "Madonna", "match": "0.2"}]),
            minutes=5,
            request_id=2,
            method="artist.getsimilar",
        ),
        obs(
            similar_body([{"name": "Madonna", "match": "0.9"}]),
            minutes=10,
            request_id=3,
            method="artist.getsimilar",
        ),
    ]
    result = derive_all(observations, similarity_owners={2: "Cher", 3: "Cher"})
    assert len(result.similarities) == 1
    assert result.similarities[0].match == 0.9


# -------------------------------------------------------------------- aliases


def test_aliases_record_autocorrect_corrections() -> None:
    aliases = derive_aliases([("cher ", "mbid:cher", BASE)])
    assert len(aliases) == 1
    assert aliases[0].requested_name_norm == "cher"
    assert aliases[0].canonical_identity == "mbid:cher"


def test_aliases_skip_requests_that_were_already_correct() -> None:
    """The database layer only supplies genuine corrections; none means none."""
    assert derive_aliases([]) == []


def test_newest_alias_wins_for_a_spelling() -> None:
    aliases = derive_aliases(
        [
            ("cher", "mbid:old", BASE),
            ("cher", "mbid:new", BASE + timedelta(days=1)),
        ]
    )
    assert len(aliases) == 1
    assert aliases[0].canonical_identity == "mbid:new"


def test_aliases_reject_a_missing_canonical_identity() -> None:
    assert derive_aliases([("cher", "", BASE)]) == []
    assert derive_aliases([("", "mbid:cher", BASE)]) == []


def test_aliases_are_sorted_deterministically() -> None:
    aliases = derive_aliases([("z", "mbid:z", BASE), ("a", "mbid:a", BASE)])
    assert [alias.requested_name_norm for alias in aliases] == ["a", "z"]


# ------------------------------------------------------- silent-loss detection


def test_unusable_bodies_are_counted_not_silently_dropped() -> None:
    """The failure mode of a parsing mismatch is *absence*.

    An archived body can carry an ``{"artist": ...}`` envelope and still be
    unusable, because it has no name and no MBID to key on. That body yields no
    entity, and without a counter the only symptom is a smaller number than
    expected.
    """
    observations = [
        obs(artist_body(), request_id=1),
        obs({"artist": {"url": "https://www.last.fm/music/unknown"}}, minutes=5, request_id=2),
    ]
    result = derive_all(observations)
    assert len(result.artists) == 1, "the usable body still derives"
    assert result.skipped_unrecognised_shape == 1, "the unusable body is reported"
    assert result.unexpected_shapes == 1


def test_expected_envelope_free_methods_are_not_counted_as_unexpected() -> None:
    """Otherwise the signal drowns in legitimate non-entity responses."""
    observations = [
        obs(artist_body(), request_id=1),
        obs(
            {"similarartists": {"artist": [{"name": "Madonna"}]}},
            minutes=5,
            request_id=2,
            method="artist.getsimilar",
        ),
        obs(
            {"results": {"artistmatches": {"artist": []}}},
            minutes=6,
            request_id=3,
            method="artist.search",
        ),
    ]
    result = derive_all(observations)
    assert result.skipped_unrecognised_shape == 0, "these methods carry another shape"
    assert result.skipped_expected_no_envelope == 2


def test_a_healthy_archive_reports_no_unexpected_shapes() -> None:
    observations = [
        obs(artist_body(), request_id=1),
        obs({"album": {"name": "Believe", "artist": "Cher"}}, request_id=2),
        obs({"track": {"name": "Believe", "artist": {"name": "Cher"}}}, request_id=3),
    ]
    assert derive_all(observations).unexpected_shapes == 0


def test_the_counter_is_per_response_not_per_kind() -> None:
    """One body must not be counted once for each entity kind it fails."""
    observations = [obs({"album": {"url": "x"}}, request_id=1)]
    result = derive_all(observations)
    assert result.skipped_unrecognised_shape == 1


def test_repeated_identical_unusable_bodies_count_once() -> None:
    """Content addressing means one stored body, so one problem, not five."""
    body = {"artist": {"url": "x"}}
    observations = [
        obs(body, minutes=offset, request_id=offset + 1, response_id="a" * 64)
        for offset in range(5)
    ]
    assert derive_all(observations).skipped_unrecognised_shape == 1


# ---------------------------------------------------- tag popularity (getTopTags)

ARTIST_BODY = {
    "artist": {
        "name": "Radiohead",
        "mbid": "a74b1b7f-71a5-4011-9441-d0b5e4122711",
        # No counts: artist.getinfo genuinely omits them.
        "tags": {"tag": [{"name": "rock"}, {"name": "alternative"}]},
    }
}

TOPTAGS_BODY = {
    "toptags": {
        "@attr": {"artist": "Radiohead"},
        "tag": [
            {"name": "rock", "count": 100},
            {"name": "alternative", "count": 54},
        ],
    }
}


def test_toptags_counts_reach_the_derived_edges() -> None:
    """The regression this exists for.

    ``artist.getTopTags`` is envelope-free, so the entity derivation skips it and every
    ``count`` used to come out null -- which silently disabled ``TagPolicy.min_count``
    and reduced genre ranking to list order everywhere.
    """
    result = derive_all(
        [
            obs(ARTIST_BODY, request_id=1),
            obs(TOPTAGS_BODY, request_id=2, method="artist.gettoptags"),
        ]
    )
    counts = {edge.tag_name_norm: edge.count for edge in result.tag_edges}
    assert counts == {"rock": 100, "alternative": 54}


def test_without_a_toptags_observation_counts_stay_null() -> None:
    """Absent data is absent, not zero: a missing count must not filter anything out."""
    result = derive_all([obs(ARTIST_BODY)])
    assert result.tag_edges
    assert all(edge.count is None for edge in result.tag_edges)


def test_an_entity_with_no_tags_adopts_the_toptags_list() -> None:
    """An artist we hold popularity data for should not appear tagless."""
    bare = {"artist": {"name": "Radiohead", "mbid": "mbid-1"}}
    result = derive_all(
        [
            obs(bare, request_id=1),
            obs(TOPTAGS_BODY, request_id=2, method="artist.gettoptags"),
        ]
    )
    assert {edge.tag_name_norm for edge in result.tag_edges} == {"rock", "alternative"}
    assert {edge.count for edge in result.tag_edges} == {100, 54}


def test_toptags_does_not_replace_the_entity_tag_list() -> None:
    """Counts are merged into the entity's own tags, not substituted for them.

    The two responses are different views of the same artist, and the envelope is the
    canonical tag set, so a tag only TopTags knows about must not silently appear
    alongside one the envelope does carry.
    """
    entity = {
        "artist": {
            "name": "Radiohead",
            "mbid": "mbid-1",
            "tags": {"tag": [{"name": "rock"}]},
        }
    }
    top = {
        "toptags": {
            "@attr": {"artist": "Radiohead"},
            "tag": [{"name": "rock", "count": 100}, {"name": "unrelated", "count": 9}],
        }
    }
    result = derive_all(
        [obs(entity, request_id=1), obs(top, request_id=2, method="artist.gettoptags")]
    )
    assert {edge.tag_name_norm for edge in result.tag_edges} == {"rock"}
    assert result.tag_edges[0].count == 100


def test_a_later_toptags_fetch_supersedes_an_earlier_one() -> None:
    """Deterministic, and the freshest numbers win."""
    stale = {"toptags": {"@attr": {"artist": "Radiohead"}, "tag": [{"name": "rock", "count": 10}]}}
    fresh = {"toptags": {"@attr": {"artist": "Radiohead"}, "tag": [{"name": "rock", "count": 100}]}}
    result = derive_all(
        [
            obs(ARTIST_BODY, request_id=1),
            obs(stale, request_id=2, minutes=1, method="artist.gettoptags"),
            obs(fresh, request_id=3, minutes=2, method="artist.gettoptags"),
        ]
    )
    rock = next(edge for edge in result.tag_edges if edge.tag_name_norm == "rock")
    assert rock.count == 100
    # `alternative` is in the envelope but ranked by neither TopTags response, so it
    # has no popularity of its own -- absent, not zero.
    alternative = next(e for e in result.tag_edges if e.tag_name_norm == "alternative")
    assert alternative.count is None


def test_toptags_ordering_does_not_depend_on_input_order() -> None:
    """The derived layer must be reproducible whatever order observations arrive in."""
    first = obs(ARTIST_BODY, request_id=1)
    stale = obs(
        {"toptags": {"@attr": {"artist": "Radiohead"}, "tag": [{"name": "rock", "count": 10}]}},
        request_id=2,
        minutes=1,
        method="artist.gettoptags",
    )
    fresh = obs(
        {"toptags": {"@attr": {"artist": "Radiohead"}, "tag": [{"name": "rock", "count": 100}]}},
        request_id=3,
        minutes=2,
        method="artist.gettoptags",
    )
    forward = derive_all([first, stale, fresh])
    backward = derive_all([fresh, stale, first])
    assert [(e.tag_name_norm, e.count) for e in forward.tag_edges] == [
        (e.tag_name_norm, e.count) for e in backward.tag_edges
    ]


def test_a_name_only_toptags_response_still_attributes() -> None:
    """The container attribute is the key, since autocorrect means the params can name
    the request while the attribute names what was actually served."""
    body = {
        "toptags": {"tag": [{"name": "rock", "count": 7}]},
    }
    # No @attr at all, so there is no owner to attribute to.
    result = derive_all(
        [obs(ARTIST_BODY, request_id=1), obs(body, request_id=2, method="artist.gettoptags")]
    )
    assert all(edge.count is None for edge in result.tag_edges)


def test_a_numeric_string_count_is_coerced() -> None:
    """Last.fm's XML-derived responses send counts as strings, so tolerance is required.

    The JSON format sends integers, but the same API serves both, and refusing a
    string would silently reduce those responses to "no popularity data".
    """
    body = {
        "toptags": {
            "@attr": {"artist": "Radiohead"},
            "tag": [{"name": "rock", "count": "100"}],
        }
    }
    result = derive_all(
        [obs(ARTIST_BODY, request_id=1), obs(body, request_id=2, method="artist.gettoptags")]
    )
    rock = next(edge for edge in result.tag_edges if edge.tag_name_norm == "rock")
    assert rock.count == 100


def test_an_unparseable_count_becomes_absent_not_zero() -> None:
    """A count that cannot be read is unknown, and unknown must not filter anything."""
    body = {
        "toptags": {
            "@attr": {"artist": "Radiohead"},
            "tag": [{"name": "rock", "count": "1,000"}],
        }
    }
    result = derive_all(
        [obs(ARTIST_BODY, request_id=1), obs(body, request_id=2, method="artist.gettoptags")]
    )
    rock = next(edge for edge in result.tag_edges if edge.tag_name_norm == "rock")
    assert rock.count is None


def test_album_and_track_edges_never_gain_counts() -> None:
    """Only artists have a popularity source, so nothing is fabricated for the others."""
    album = {
        "album": {
            "name": "OK Computer",
            "artist": "Radiohead",
            "tags": {"tag": [{"name": "alternative"}]},
        }
    }
    result = derive_all(
        [
            obs(album, request_id=1, method="album.getinfo"),
            obs(TOPTAGS_BODY, request_id=2, method="artist.gettoptags"),
        ]
    )
    assert result.tag_edges
    assert all(edge.count is None for edge in result.tag_edges)


def test_counts_reach_the_entity_not_only_the_edges() -> None:
    """The half-fix this test exists to prevent.

    Applying counts only to ``lastfm_tag_edge`` looked correct -- the counts were in the
    database -- but the candidate the mapping layer builds reads ``entity.tags``, so
    ``TagPolicy.min_count`` still filtered nothing and genre ranking was still list
    order. The entity is the thing every consumer actually reads.
    """
    result = derive_all(
        [
            obs(ARTIST_BODY, request_id=1),
            obs(TOPTAGS_BODY, request_id=2, method="artist.gettoptags"),
        ]
    )
    artist = next(entity for entity in result.artists)
    counts = {tag["name"]: tag["count"] for tag in artist.tags}
    assert counts == {"rock": 100, "alternative": 54}


def test_entity_counts_and_edge_counts_agree() -> None:
    """Two copies of the same number must not be able to disagree."""
    result = derive_all(
        [
            obs(ARTIST_BODY, request_id=1),
            obs(TOPTAGS_BODY, request_id=2, method="artist.gettoptags"),
        ]
    )
    artist = next(entity for entity in result.artists)
    from_entity = {tag["name"]: tag["count"] for tag in artist.tags}
    from_edges = {edge.tag_name: edge.count for edge in result.tag_edges}
    assert from_entity == from_edges


def test_an_existing_count_is_not_overwritten() -> None:
    """The envelope's own count wins where it has one, so a re-derive is stable."""
    from metaedit.archive.derive import DerivedEntity, apply_top_tag_counts

    entity = DerivedEntity(
        identity="artist:x",
        kind="artist",
        name="X",
        name_norm="x",
        tags=[{"name": "rock", "count": 7}],
    )
    apply_top_tag_counts(entity, {"rock": 100})
    assert entity.tags[0]["count"] == 7


def test_adopted_tags_are_ordered_by_popularity() -> None:
    """When nothing else supplies an order, popularity is the useful one."""
    from metaedit.archive.derive import DerivedEntity, apply_top_tag_counts

    entity = DerivedEntity(identity="artist:x", kind="artist", name="X", name_norm="x", tags=[])
    apply_top_tag_counts(entity, {"quiet": 3, "loud": 90, "medium": 40})
    assert [tag["name"] for tag in entity.tags] == ["loud", "medium", "quiet"]


def test_applying_no_counts_changes_nothing() -> None:
    from metaedit.archive.derive import DerivedEntity, apply_top_tag_counts

    entity = DerivedEntity(
        identity="artist:x",
        kind="artist",
        name="X",
        name_norm="x",
        tags=[{"name": "rock", "count": None}],
    )
    apply_top_tag_counts(entity, {})
    assert entity.tags == [{"name": "rock", "count": None}]


# ------------------------------------------- tag popularity across all three kinds

ALBUM_BODY = {
    "album": {
        "name": "Californication",
        "artist": "Red Hot Chili Peppers",
        "tags": {"tag": [{"name": "rock"}, {"name": "alternative"}]},
    }
}

ALBUM_TOPTAGS = {
    "toptags": {
        "@attr": {"artist": "Red Hot Chili Peppers", "album": "Californication"},
        "tag": [
            {"name": "alternative rock", "count": 100},
            {"name": "rock", "count": 89},
        ],
    }
}

TRACK_BODY = {
    "track": {
        "name": "Californication",
        "artist": {"name": "Red Hot Chili Peppers"},
        "tags": {"tag": [{"name": "rock"}]},
    }
}

TRACK_TOPTAGS = {
    "toptags": {
        "@attr": {"artist": "Red Hot Chili Peppers", "track": "Californication"},
        "tag": [{"name": "rock", "count": 100}],
    }
}


def test_album_tag_counts_reach_the_album_entity() -> None:
    """The counts source for albums, which `album.getInfo` does not provide.

    `album.getInfo` returns tags without counts, so without `album.getTopTags` every
    album tag edge had a null count and album ranking was list order.
    """
    result = derive_all(
        [
            obs(ALBUM_BODY, request_id=1, method="album.getinfo"),
            obs(ALBUM_TOPTAGS, request_id=2, method="album.gettoptags"),
        ]
    )
    album = next(entity for entity in result.albums)
    counts = {tag["name"]: tag["count"] for tag in album.tags}
    # `alternative` is in the envelope but absent from TopTags: unknown, not zero.
    assert counts == {"rock": 89, "alternative": None}


def test_track_tag_counts_reach_the_track_entity() -> None:
    result = derive_all(
        [
            obs(TRACK_BODY, request_id=1, method="track.getinfo"),
            obs(TRACK_TOPTAGS, request_id=2, method="track.gettoptags"),
        ]
    )
    track = next(entity for entity in result.tracks)
    assert {tag["name"]: tag["count"] for tag in track.tags} == {"rock": 100}


def test_an_album_and_an_artist_with_the_same_name_do_not_collide() -> None:
    """Attribution keys differ per kind, which is what stops a cross-kind match.

    An artist called "rock" and an album called "rock" must not share counts, and two
    albums with the same title by different artists must not either.
    """
    from metaedit.archive.derive import DerivedEntity, entity_tag_key

    artist = DerivedEntity(
        identity="artist:x", kind="artist", name="Californication", name_norm="californication"
    )
    # `entity_tag_key` folds the *raw* names, so those are what the fixture must set:
    # re-folding the stored norms would apply the tolerance twice and could not match a
    # response whose punctuation differs.
    album = DerivedEntity(
        identity="album:x",
        kind="album",
        name="Californication",
        name_norm="californication",
        artist_name="Red Hot Chili Peppers",
        artist_name_norm="red hot chili peppers",
    )
    other = DerivedEntity(
        identity="album:y",
        kind="album",
        name="Californication",
        name_norm="californication",
        artist_name="Someone Else",
        artist_name_norm="someone else",
    )
    # Case-preserving, like `normalize_name`: Last.fm echoes the canonical spelling, so
    # folding case here would merge entities the raw layer keeps distinct.
    assert entity_tag_key(artist) == ("Californication",)
    assert entity_tag_key(album) == ("Red Hot Chili Peppers", "Californication")
    assert entity_tag_key(album) != entity_tag_key(other)
    assert entity_tag_key(album) != entity_tag_key(artist)


def test_album_counts_do_not_leak_to_a_different_artist() -> None:
    """Two albums of the same name by different artists keep their own popularity."""
    mine = {
        "album": {
            "name": "Greatest Hits",
            "artist": "Artist One",
            "tags": {"tag": [{"name": "rock"}]},
        }
    }
    theirs = {
        "album": {
            "name": "Greatest Hits",
            "artist": "Artist Two",
            "tags": {"tag": [{"name": "rock"}]},
        }
    }
    top = {
        "toptags": {
            "@attr": {"artist": "Artist One", "album": "Greatest Hits"},
            "tag": [{"name": "rock", "count": 99}],
        }
    }
    result = derive_all(
        [
            obs(mine, request_id=1, method="album.getinfo"),
            obs(theirs, request_id=2, minutes=1, method="album.getinfo"),
            obs(top, request_id=3, minutes=2, method="album.gettoptags"),
        ]
    )
    got = {
        entity.artist_name: {tag["name"]: tag["count"] for tag in entity.tags}
        for entity in result.albums
    }
    assert got["Artist One"] == {"rock": 99}
    assert got["Artist Two"] == {"rock": None}, "counts must not cross artists"


def test_a_toptags_response_without_attributes_is_ignored() -> None:
    """No attribution means no safe target, so nothing is applied."""
    body = {"toptags": {"tag": [{"name": "rock", "count": 100}]}}
    result = derive_all(
        [
            obs(ALBUM_BODY, request_id=1, method="album.getinfo"),
            obs(body, request_id=2, method="album.gettoptags"),
        ]
    )
    album = next(entity for entity in result.albums)
    assert all(tag["count"] is None for tag in album.tags)


def test_typographic_variants_still_match_for_counts() -> None:
    """Last.fm is inconsistent with itself about punctuation.

    Live-verified against the real API: for one album, `album.getInfo` returned
    "…and Justice for All" (U+2026) while `album.getTopTags` returned "...and Justice for
    All" (three full stops). Keyed strictly, the counts were silently dropped and the
    album showed popularity for none of its tags -- 0/5 instead of 4/5.
    """
    from metaedit.archive.derive import typographic_match_key

    assert typographic_match_key("\u2026and Justice for All") == typographic_match_key(
        "...and Justice for All"
    )
    # The other variants NFKC does not fold on its own.
    assert typographic_match_key("\u2018quoted\u2019") == typographic_match_key("'quoted'")
    assert typographic_match_key("a\u2013b") == typographic_match_key("a-b")
    # Genuinely different names must stay different.
    assert typographic_match_key("OK Computer") != typographic_match_key("Kid A")


def test_typographic_folding_does_not_change_request_identity() -> None:
    """`normalize_name` defines request identity, so it must NOT fold typography.

    Changing it would reinterpret rows already in the archive -- a much larger blast radius
    than a missing tag count, which is why the tolerance lives only in the counts join.
    """
    from metaedit.adapters.lastfm.canonical import normalize_name

    assert normalize_name("\u2026and Justice for All") != normalize_name("...and Justice for All")


def test_counts_survive_an_ellipsis_mismatch_between_endpoints() -> None:
    """End to end through the derivation, with the punctuation differing on each side."""
    album = {
        "album": {
            "name": "\u2026and Justice for All",
            "artist": "Metallica",
            "tags": {"tag": [{"name": "thrash metal"}]},
        }
    }
    top = {
        "toptags": {
            "@attr": {"artist": "Metallica", "album": "...and Justice for All"},
            "tag": [{"name": "thrash metal", "count": 100}],
        }
    }
    result = derive_all(
        [
            obs(album, request_id=1, method="album.getinfo"),
            obs(top, request_id=2, method="album.gettoptags"),
        ]
    )
    derived = next(entity for entity in result.albums)
    assert {tag["name"]: tag["count"] for tag in derived.tags} == {"thrash metal": 100}
