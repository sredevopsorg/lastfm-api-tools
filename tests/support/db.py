"""Disposable Postgres databases for the test suite.

Postgres-backed tests each get their own database, so that one test's rows can
never be read by another. Populating that database was the expensive part: every
test ran ``alembic upgrade head`` in a subprocess, which is most of a second
spent importing Python and replaying migrations whose result is identical every
time.

So the migration chain runs once per session, into a *template* database, and
each test's database is cloned from it. ``CREATE DATABASE ... TEMPLATE`` is a
file copy, and every test still gets a private, fully migrated database -- only
the way it is populated changed. A broken migration still fails the session
before a single test runs.

Kept free of pytest on purpose: the provisioning logic is tested directly by
``tests/integration/test_test_database_isolation.py``, which needs to create two
databases inside one test.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url

REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_ADMIN_URL = "postgresql+psycopg://metaedit:metaedit@localhost:5432/metaedit"

TEST_DATABASE_PREFIX = "metaedit_test_"
TEMPLATE_DATABASE_PREFIX = "metaedit_template_"


def admin_url() -> str:
    """The maintenance database, which must allow ``CREATE DATABASE``."""
    return os.environ.get("METAEDIT_TEST_DATABASE_URL", DEFAULT_ADMIN_URL)


def url_for(database: str, *, admin: str | None = None) -> str:
    """The connection URL for another database on the same server.

    Built with SQLAlchemy rather than string surgery so that credentials escape
    correctly and query parameters (``sslmode`` and friends) survive the trip.
    """
    return (
        make_url(admin_url() if admin is None else admin)
        .set(database=database)
        .render_as_string(hide_password=False)
    )


def postgres_available() -> bool:
    try:
        engine = create_engine(admin_url(), pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        engine.dispose()
        return True
    except Exception:
        return False


def run_alembic(database_url: str, *args: str) -> None:
    """Run the real migration command, as a user would.

    A subprocess rather than the Alembic API because this is the entrypoint
    ``docker-compose.yml`` and the ``seed`` image stage actually use; testing it
    keeps ``alembic.ini`` and ``migrations/env.py`` in the tested surface.
    """
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


def create_database(engine: Engine, name: str, *, template: str | None = None) -> None:
    """Create an empty database, or a copy of ``template``."""
    clause = f' TEMPLATE "{template}"' if template else ""
    with engine.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{name}"{clause}')


def drop_stray_connections(engine: Engine, name: str) -> None:
    """Terminate every session attached to ``name`` except our own.

    Both cloning a template and dropping a database are refused while a session
    remains attached; a test that failed mid-flight can leave a pool open.
    """
    with engine.connect() as conn:
        conn.exec_driver_sql(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '{name}' AND pid <> pg_backend_pid()"
        )


def drop_database(engine: Engine, name: str) -> None:
    drop_stray_connections(engine, name)
    with engine.connect() as conn:
        conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}"')


def new_database_name() -> str:
    return f"{TEST_DATABASE_PREFIX}{uuid.uuid4().hex[:12]}"


def create_migrated_template(engine: Engine) -> str:
    """Migrate a template database to ``head`` and return its name.

    Callers must ensure no session is attached to it while it is being cloned.
    Nothing in the suite ever connects to it: the migration subprocess has exited
    by the time this returns, and clones are made through the admin connection.
    """
    name = f"{TEMPLATE_DATABASE_PREFIX}{uuid.uuid4().hex[:12]}"
    create_database(engine, name)
    try:
        run_alembic(url_for(name), "upgrade", "head")
    except BaseException:
        drop_database(engine, name)
        raise
    drop_stray_connections(engine, name)
    return name
