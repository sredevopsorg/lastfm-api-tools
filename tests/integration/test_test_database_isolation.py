"""Cloning the test database from a template must still isolate every test.

The suite used to replay ``alembic upgrade head`` per test; now it copies a
migrated template. That is a large saving, but it is only worth having if the
property the suite depended on survives: a test can never see another test's
rows, and every database is at the migration head. Both are properties of the
provisioning code, so they are asserted here rather than assumed.

These tests deliberately go through :mod:`tests.support.db` directly instead of
the ``database_url`` fixture, because a fixture that hands out one database per
test cannot be asked for two.
"""

from __future__ import annotations

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from tests.conftest import requires_postgres
from tests.support import db

pytestmark = requires_postgres

MARKER = "isolation-marker"

# The one table a clone-test writes to. Nothing else in the suite reads it here,
# so a leaked row can only have come from the database we wrote it to.
INSERT_RESPONSE = (
    "insert into lastfm_response (id, body, body_bytes, is_error) "
    "values (:id, '{}'::jsonb, 2, false)"
)


def _engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True)


def _count(url: str, table: str) -> int:
    engine = _engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(f"select count(*) from {table}")).scalar_one()
    finally:
        engine.dispose()


def _head_revision() -> str:
    """The migration head, read from the scripts on disk rather than the database."""
    config = Config(str(db.REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(db.REPO_ROOT / "migrations"))
    return ScriptDirectory.from_config(config).get_current_head()


def test_two_databases_cloned_from_one_template_do_not_share_rows(
    _admin_engine: Engine, migrated_template: str
) -> None:
    names = [db.new_database_name(), db.new_database_name()]
    for name in names:
        db.create_database(_admin_engine, name, template=migrated_template)
    first, second = names
    try:
        engine = _engine(db.url_for(first))
        with engine.begin() as conn:
            conn.execute(text(INSERT_RESPONSE), {"id": MARKER})
        engine.dispose()

        assert _count(db.url_for(first), "lastfm_response") == 1
        assert _count(db.url_for(second), "lastfm_response") == 0
    finally:
        for name in names:
            db.drop_database(_admin_engine, name)


def test_a_clone_is_at_the_migration_head(_admin_engine: Engine, migrated_template: str) -> None:
    """``alembic_version`` is copied, not applied, so it is worth checking."""
    name = db.new_database_name()
    db.create_database(_admin_engine, name, template=migrated_template)
    try:
        engine = _engine(db.url_for(name))
        with engine.connect() as conn:
            applied = conn.execute(text("select version_num from alembic_version")).scalar_one()
        engine.dispose()
        assert applied == _head_revision()
    finally:
        db.drop_database(_admin_engine, name)
