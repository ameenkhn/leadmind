"""Liveness and readiness.

Two endpoints, not one, because they answer different questions and a caller acts differently on
each. `/healthz` says the process is running: if it fails, restart. `/readyz` says the process
can serve: if it fails, stop sending traffic but do *not* restart — a database blip is not a
crash loop, and treating it as one turns a two-minute outage into a rolling restart storm.
"""

from __future__ import annotations

from functools import lru_cache

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app import __version__
from app.api.deps import AppSettings, DbSession
from app.core.logging import get_logger
from app.schemas.common import HealthResponse, ReadinessResponse

router = APIRouter(tags=["operations"])
logger = get_logger(__name__)


@lru_cache(maxsize=1)
def _expected_revision() -> str | None:
    """The migration head this build of the code expects.

    Read from the migration scripts rather than hard-coded, so it cannot drift from what
    ``alembic upgrade head`` would actually apply. Cached because it is a property of the
    deployed code, not of the database: re-reading the whole migration directory on every
    readiness probe made the check the slowest endpoint in the service.
    """
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        from app.core.config import get_settings

        settings = get_settings()
        config = Config(str(settings.repo_root / "alembic.ini"))
        config.set_main_option(
            "script_location", str(settings.repo_root / "backend/app/db/migrations")
        )
        return ScriptDirectory.from_config(config).get_current_head()
    except Exception:  # pragma: no cover - alembic layout problems are an operator issue
        logger.warning("could not resolve expected migration head")
        return None


@router.get("/healthz", response_model=HealthResponse, summary="Liveness")
def healthz(settings: AppSettings) -> HealthResponse:
    """Does not touch the database on purpose — see the module docstring."""
    return HealthResponse(status="ok", version=__version__, environment=settings.environment)


@router.get("/readyz", response_model=ReadinessResponse, summary="Readiness")
def readyz(session: DbSession, response: Response) -> ReadinessResponse:
    expected = _expected_revision()
    try:
        session.execute(text("SELECT 1"))
        current = session.scalar(text("SELECT version_num FROM alembic_version LIMIT 1"))
    except Exception as exc:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        logger.warning("readiness_check_failed", error=str(exc))
        return ReadinessResponse(
            status="unavailable",
            database="unreachable",
            expected_revision=expected,
            schema_current=False,
            detail="The database could not be reached.",
        )

    schema_current = expected is None or current == expected
    if not schema_current:
        # Serving against a schema the code was not written for is how a deploy corrupts data
        # quietly. Better to report not-ready and let the operator run the migration.
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(
        status="ready" if schema_current else "unavailable",
        database="reachable",
        schema_revision=str(current) if current else None,
        expected_revision=expected,
        schema_current=schema_current,
        detail=None if schema_current else "Database schema is not at the expected revision.",
    )
