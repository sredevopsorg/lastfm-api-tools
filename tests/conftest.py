"""Shared fixtures.

Postgres-backed tests use a disposable database created on demand. Set
``METAEDIT_TEST_DATABASE_URL`` to point at the admin database of a running
Postgres 18 instance (compose exposes one); tests skip cleanly when absent.

Each test still gets a private database, but it is cloned from a migrated
template (``CREATE DATABASE ... TEMPLATE``) instead of replaying the migration
chain. See :mod:`tests.support.db` for the reasoning and the provisioning code.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from tests.support import db

POSTGRES_AVAILABLE = db.postgres_available()
requires_postgres = pytest.mark.skipif(
    not POSTGRES_AVAILABLE, reason="no reachable Postgres (set METAEDIT_TEST_DATABASE_URL)"
)


@pytest.fixture(scope="session")
def _admin_engine() -> Iterator[Engine]:
    engine = create_engine(db.admin_url(), isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def migrated_template(_admin_engine: Engine) -> Iterator[str]:
    """The migration chain, applied once, ready to be cloned per test.

    Session-scoped on purpose: a migration failure should abort the run before
    any test reports a result, rather than being attributed to one test.
    """
    name = db.create_migrated_template(_admin_engine)
    try:
        yield name
    finally:
        db.drop_database(_admin_engine, name)


@pytest.fixture
def database_url(_admin_engine: Engine, migrated_template: str) -> Iterator[str]:
    """A private, migrated, empty database."""
    name = db.new_database_name()
    db.create_database(_admin_engine, name, template=migrated_template)
    try:
        yield db.url_for(name)
    finally:
        db.drop_database(_admin_engine, name)


@pytest.fixture
def anyio_backend() -> str:  # pragma: no cover - configuration hook
    return "asyncio"
