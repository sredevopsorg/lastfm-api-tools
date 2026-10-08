"""The initial migration must produce a working archive schema.

These tests need a real Postgres 18: partitioning, JSONB and the derived-table
invariants are exactly the things a mock would not verify.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from tests.conftest import requires_postgres

from metaedit.db.partitions import ensure_partitions_sync, partition_name

pytestmark = requires_postgres


def _engine(url: str):  # type: ignore[no-untyped-def]
    return create_engine(url, pool_pre_ping=True)


def _tables(conn) -> set[str]:  # type: ignore[no-untyped-def]
    rows = conn.execute(text("select tablename from pg_tables where schemaname = 'public'"))
    return {row[0] for row in rows}


EXPECTED_TABLES = {
    "alembic_version",
    "snapshot",
    "audit_log",
    "lastfm_request",
    "lastfm_response",
    "lastfm_artist",
    "lastfm_album",
    "lastfm_track",
    "lastfm_tag_edge",
    "lastfm_similarity",
    "lastfm_artist_alias",
    "lastfm_entity_tag",
    "archive_stat",
    # Operator settings (0003). Listed explicitly, like every other table, because the
    # assertion is an equality rather than a superset: a table appearing here without
    # being added to this set is meant to fail the suite.
    "genre_blacklist",
}


def test_migration_creates_every_table(database_url: str) -> None:
    engine = _engine(database_url)
    with engine.connect() as conn:
        assert _tables(conn) == EXPECTED_TABLES
        assert conn.execute(text("select version()")).scalar_one().startswith("PostgreSQL 18")
    engine.dispose()


def test_lastfm_request_is_declaratively_partitioned(database_url: str) -> None:
    engine = _engine(database_url)
    with engine.connect() as conn:
        kind = conn.execute(
            text(
                "select c.relkind from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                "where n.nspname = 'public' and c.relname = 'lastfm_request'"
            )
        ).scalar_one()
    engine.dispose()
    assert kind == "p", "the append-only request log must be partitioned"


def test_partitions_are_created_and_accept_writes(database_url: str) -> None:
    engine = _engine(database_url)
    with engine.begin() as conn:
        created = ensure_partitions_sync(conn, months_ahead=2)
    assert len(created) == 3

    response_id = "a" * 64
    with engine.begin() as conn:
        conn.execute(
            text(
                "insert into lastfm_response (id, body, body_bytes, is_error) "
                "values (:id, '{}'::jsonb, 2, false)"
            ),
            {"id": response_id},
        )
        conn.execute(
            text(
                "insert into lastfm_request "
                "(method, params, params_hash, requested_at, http_status, response_id) "
                "values ('artist.getinfo', '{}'::jsonb, :h, now(), 200, :rid)"
            ),
            {"h": "b" * 64, "rid": response_id},
        )

    with engine.connect() as conn:
        count = conn.execute(text("select count(*) from lastfm_request")).scalar_one()
        routed = conn.execute(
            text("select tableoid::regclass::text from lastfm_request")
        ).scalar_one()
    engine.dispose()

    assert count == 1
    assert routed == partition_name(datetime.now(UTC).date()), "row must land in the current month"


def test_request_row_cannot_reference_a_missing_response(database_url: str) -> None:
    """RESTRICT protects the raw archive from accidental orphaning."""
    engine = _engine(database_url)
    with engine.begin() as conn:
        ensure_partitions_sync(conn, months_ahead=1)
    with pytest.raises(Exception) as excinfo, engine.begin() as conn:
        conn.execute(
            text(
                "insert into lastfm_request "
                "(method, params, params_hash, requested_at, response_id) "
                "values ('artist.getinfo', '{}'::jsonb, :h, now(), :rid)"
            ),
            {"h": "c" * 64, "rid": "d" * 64},
        )
    engine.dispose()
    assert "foreign key" in str(excinfo.value).lower()


def test_content_addressed_responses_are_unique(database_url: str) -> None:
    engine = _engine(database_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "insert into lastfm_response (id, body, body_bytes, observation_count) "
                "values (:id, '{\"artist\": {}}'::jsonb, 16, 1)"
            ),
            {"id": "e" * 64},
        )
    with pytest.raises(Exception) as excinfo, engine.begin() as conn:
        conn.execute(
            text(
                "insert into lastfm_response (id, body, body_bytes) "
                "values (:id, '{\"artist\": {}}'::jsonb, 16)"
            ),
            {"id": "e" * 64},
        )
    engine.dispose()
    assert "duplicate key" in str(excinfo.value).lower()


def test_derived_entity_identity_is_unique(database_url: str) -> None:
    engine = _engine(database_url)
    now = datetime.now(UTC)
    insert = text(
        "insert into lastfm_artist "
        "(identity, name, name_norm, first_seen_at, last_seen_at, latest_response_id) "
        "values (:identity, 'Radiohead', 'radiohead', :now, :now, :rid)"
    )
    with engine.begin() as conn:
        conn.execute(insert, {"identity": "name:radiohead", "now": now, "rid": "f" * 64})
    with pytest.raises(Exception) as excinfo, engine.begin() as conn:
        conn.execute(insert, {"identity": "name:radiohead", "now": now, "rid": "f" * 64})
    engine.dispose()
    assert "duplicate key" in str(excinfo.value).lower()
