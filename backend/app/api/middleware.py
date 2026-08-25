"""Request context and access logging.

Phase 1 bound a ``run_id`` to every log record inside an ingest so a run could be reconstructed
from logs alone. This is the same idea for HTTP: one ``request_id`` per request, bound to
structlog's contextvars, echoed in the response header, and included in every error body. A user
reporting "it returned a 500" with that id gives the whole server-side story for free.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import get_logger

logger = get_logger("app.api.access")

REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Bind a request id, time the request, log the outcome."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # An inbound id is honoured so a trace survives a proxy or a frontend that already
        # generates one; it is length-capped because it is attacker-controlled and ends up in
        # log lines.
        incoming = request.headers.get(REQUEST_ID_HEADER)
        request_id = (incoming or uuid.uuid4().hex)[:64]
        request.state.request_id = request_id

        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.exception("request_failed", duration_ms=duration_ms)
            structlog.contextvars.clear_contextvars()
            raise

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers[REQUEST_ID_HEADER] = request_id
        logger.info(
            "request_completed",
            status_code=response.status_code,
            duration_ms=duration_ms,
            query=str(request.url.query) or None,
        )
        structlog.contextvars.clear_contextvars()
        return response
