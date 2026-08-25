"""The FastAPI application.

Phase 2's whole remit is putting an interface on what Phases 1 and 1b measured, without
weakening any of it. Three things that meant in practice:

**Nothing is smoothed over on the way out.** A city carries its match confidence, a mailbox
carries the difference between *unreachable* and *never checked*, a score carries how many of its
factors could actually be evaluated. Every one of those could have been flattened into a friendlier
scalar, and every one of them would have hidden the uncertainty the pipeline spent Phase 1
measuring.

**The only writes are review decisions.** No endpoint edits a lead. Lead data comes from ingest,
which is idempotent and reproducible from the source workbook; letting an API mutate it would
make the next re-ingest either destructive or a merge conflict, and neither is worth it before
there is a reason.

**A confirmed duplicate is a pointer, not a deletion.** Which keeps the reconciliation identity
true and makes every reviewer decision reversible.

Deliberately not here: authentication, rate limiting and caching. They belong in Phase 10 with
the deployment story, and a token check bolted on now would be security theatre against a service
that binds to localhost. This is stated rather than left to be discovered.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.errors import install_error_handlers
from app.api.middleware import RequestContextMiddleware
from app.api.routers import companies, duplicates, health, leads, stats
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger

logger = get_logger(__name__)

DESCRIPTION = """
Read access to the LeadMind corpus, and the human review queue for duplicates the pipeline
deliberately refused to merge.

**Data quality is not lead quality.** Every `quality` field in this API answers *how much do we
reliably know about this record* — completeness, reachability, verifiability. Whether a lead is
worth contacting is a separate judgement with different inputs, and arrives in Phase 5.

**`unknown` is not `unreachable`.** A domain with no MX record is a measurement. A resolver
timeout is the absence of one. They are different values everywhere in this API and must not be
collapsed by a client.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:  # noqa: ARG001
    configure_logging()
    settings = get_settings()
    logger.info(
        "api_starting",
        version=__version__,
        environment=settings.environment,
        prefix=settings.api_prefix,
        docs_enabled=settings.api_docs_enabled,
    )
    yield
    # The engine is process-wide and cached; disposing it here returns pooled connections
    # rather than leaving the database to time them out.
    from app.db.session import get_engine

    get_engine().dispose()
    logger.info("api_stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    A factory rather than a module-level singleton so tests can build an app against a test
    database without the import order deciding which database that is.
    """
    configure_logging()
    config = settings or get_settings()

    app = FastAPI(
        title="LeadMind API",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs" if config.api_docs_enabled else None,
        redoc_url="/redoc" if config.api_docs_enabled else None,
        openapi_url="/openapi.json" if config.api_docs_enabled else None,
    )

    app.add_middleware(RequestContextMiddleware)
    if config.api_cors_origins:
        # Exact origins only. `allow_origins=["*"]` together with credentials is rejected by
        # browsers anyway, and a wildcard here would be a default nobody revisits.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.api_cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["X-Request-ID", "X-Rubric-Version"],
        )

    install_error_handlers(app)

    # Probes stay off the versioned prefix: an orchestrator's health check should not have to
    # know which version of the API is deployed.
    app.include_router(health.router)

    versioned = APIRouter(prefix=config.api_prefix)
    versioned.include_router(leads.router)
    versioned.include_router(companies.router)
    versioned.include_router(duplicates.router)
    versioned.include_router(stats.router)
    app.include_router(versioned)

    @app.get("/", include_in_schema=False)
    def index() -> dict[str, str | None]:
        return {
            "name": "LeadMind API",
            "version": __version__,
            "api": config.api_prefix,
            "docs": "/docs" if config.api_docs_enabled else None,
            "health": "/healthz",
        }

    return app


app = create_app()
