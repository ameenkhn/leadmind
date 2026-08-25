"""Structured logging.

Every log record emitted inside a pipeline run carries the same ``run_id``, so a single ingest
(or, later, a research run) can be reconstructed from logs alone. That property is what makes
the observability requirements in later phases cheap to add rather than retrofitted.
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog

from app.core.config import get_settings

_CONFIGURED = False


def configure_logging(*, force: bool = False) -> None:
    """Install the structlog pipeline. Idempotent."""
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    settings = get_settings()
    # Logs go to stderr so stdout stays a clean channel for machine-readable CLI output
    # (`leadmind ingest --json | jq`). Mixing the two is how a pipeline breaks a shell script.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=getattr(logging, settings.log_level, logging.INFO),
        force=True,
    )

    processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]
    processors.append(
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level, logging.INFO)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    configure_logging()
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


def new_run_id() -> str:
    return uuid.uuid4().hex


@contextmanager
def run_context(**bindings: Any) -> Iterator[str]:
    """Bind a ``run_id`` (and any extra keys) to every log record inside the block."""
    configure_logging()
    run_id = str(bindings.pop("run_id", None) or new_run_id())
    tokens = structlog.contextvars.bind_contextvars(run_id=run_id, **bindings)
    try:
        yield run_id
    finally:
        structlog.contextvars.reset_contextvars(**tokens)
