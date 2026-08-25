"""Shared test fixtures.

Integration tests run against a dedicated ``*_test`` database that is created and migrated on
demand, then truncated once per session. They never touch the development database, and they
never leave rows behind: each test body runs inside a transaction that is rolled back.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKBOOK = REPO_ROOT / "data" / "raw" / "Outbound_Leads.xlsx"


def pytest_configure(config: pytest.Config) -> None:
    os.environ.setdefault("LEADMIND_LOG_LEVEL", "WARNING")


@pytest.fixture(scope="session")
def workbook_path() -> Path:
    if not WORKBOOK.exists():
        pytest.skip(f"source workbook not present at {WORKBOOK}")
    return WORKBOOK


@pytest.fixture(scope="session")
def prepared(workbook_path: Path):  # type: ignore[no-untyped-def]
    """The full in-memory pipeline over the real dataset, computed once for the whole session."""
    from app.ingestion.pipeline import prepare

    return prepare(workbook_path)


def _test_database_url() -> tuple[str, str, str]:
    """Return (admin_url, test_url, test_db_name) derived from the configured database."""
    from app.core.config import get_settings

    base = get_settings().sync_database_url
    head, _, name = base.rpartition("/")
    test_name = os.environ.get("LEADMIND_TEST_DB", f"{name}_test")
    return f"{head}/postgres", f"{head}/{test_name}", test_name


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """A migrated, empty test database."""
    from alembic import command
    from alembic.config import Config

    admin_url, test_url, test_name = _test_database_url()

    try:
        admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with admin.connect() as connection:
            exists = connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": test_name}
            )
            if not exists:
                connection.execute(text(f'CREATE DATABASE "{test_name}"'))
        admin.dispose()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"PostgreSQL not reachable: {exc}")

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "backend/app/db/migrations"))
    previous = os.environ.get("LEADMIND_DATABASE_URL")
    os.environ["LEADMIND_DATABASE_URL"] = test_url

    from app.core.config import get_settings

    get_settings.cache_clear()
    command.upgrade(config, "head")

    test_engine = create_engine(test_url, future=True)
    with test_engine.begin() as connection:
        tables = connection.scalars(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
            )
        ).all()
        if tables:
            joined = ", ".join(f'"{table}"' for table in tables)
            connection.execute(text(f"TRUNCATE {joined} RESTART IDENTITY CASCADE"))

    yield test_engine

    test_engine.dispose()
    if previous is None:
        os.environ.pop("LEADMIND_DATABASE_URL", None)
    else:
        os.environ["LEADMIND_DATABASE_URL"] = previous
    get_settings.cache_clear()


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    """A session inside a transaction that is rolled back after the test."""
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
