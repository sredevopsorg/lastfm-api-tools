"""Shared fixtures.

Postgres-backed tests use a disposable database created on demand. Set
``METAEDIT_TEST_DATABASE_URL`` to point at the admin database of a running
Postgres 18 instance (compose exposes one); tests skip cleanly when absent.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_ADMIN_URL = "postgresql+psycopg://metaedit:metaedit@localhost:5432/metaedit"


def _admin_url() -> str:
    return os.environ.get("METAEDIT_TEST_DATABASE_URL", DEFAULT_ADMIN_URL)


def _postgres_available() -> bool:
    try:
        engine = create_engine(_admin_url(), pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        engine.dispose()
        return True
    except Exception:
        return False


POSTGRES_AVAILABLE = _postgres_available()
requires_postgres = pytest.mark.skipif(
    not POSTGRES_AVAILABLE, reason="no reachable Postgres (set METAEDIT_TEST_DATABASE_URL)"
)


@pytest.fixture(scope="session")
def _admin_engine() -> Iterator[Engine]:
    engine = create_engine(_admin_url(), isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    yield engine
    engine.dispose()


@pytest.fixture
def database_url(_admin_engine: Engine) -> Iterator[str]:
    """A freshly migrated, disposable database."""
    name = f"metaedit_test_{uuid.uuid4().hex[:12]}"
    with _admin_engine.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{name}"')

    admin = _admin_url()
    url = admin.rsplit("/", 1)[0] + f"/{name}"
    try:
        _run_alembic(url, "upgrade", "head")
        yield url
    finally:
        with _admin_engine.connect() as conn:
            conn.exec_driver_sql(
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = '{name}' AND pid <> pg_backend_pid()"
            )
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}"')


def _run_alembic(database_url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": database_url}
    result = subprocess.run(
        [str(REPO_ROOT / ".venv" / "bin" / "alembic"), *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        msg = f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"
        raise RuntimeError(msg)


@pytest.fixture
def anyio_backend() -> str:  # pragma: no cover - configuration hook
    return "asyncio"
