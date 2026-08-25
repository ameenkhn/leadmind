"""One error shape, for every failure.

Errors are part of the interface. A client that gets a JSON body for a 404, an HTML page for a
500 and a differently-shaped JSON body for a validation failure has to write three parsers, and
will write one and guess at the rest.

Everything here answers in RFC 9457 ``application/problem+json``, carries the request id that
appears in the server logs, and never leaks an exception message from an unexpected error — the
detail goes to the log, the client gets a stable code and a request id to quote.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import (
    ConfigurationError,
    ConflictError,
    InvalidRequestError,
    LeadMindError,
    NotFoundError,
)
from app.core.logging import get_logger

logger = get_logger(__name__)

PROBLEM_MEDIA_TYPE = "application/problem+json"

_STATUS_TITLES: dict[int, str] = {
    400: "Bad request",
    404: "Not found",
    409: "Conflict",
    422: "Validation failed",
    500: "Internal server error",
    503: "Service unavailable",
}


def problem_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    detail: str | None = None,
    title: str | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"about:blank#{code}",
        "title": title or _STATUS_TITLES.get(status_code, "Error"),
        "status": status_code,
        "detail": detail,
        "instance": request.url.path,
        "request_id": getattr(request.state, "request_id", None),
    }
    if errors:
        body["errors"] = errors
    return JSONResponse(status_code=status_code, content=body, media_type=PROBLEM_MEDIA_TYPE)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(NotFoundError)
    async def _not_found(request: Request, exc: NotFoundError) -> JSONResponse:
        return problem_response(request, status_code=404, code="not_found", detail=str(exc))

    @app.exception_handler(ConflictError)
    async def _conflict(request: Request, exc: ConflictError) -> JSONResponse:
        return problem_response(request, status_code=409, code="conflict", detail=str(exc))

    @app.exception_handler(InvalidRequestError)
    async def _invalid(request: Request, exc: InvalidRequestError) -> JSONResponse:
        return problem_response(request, status_code=422, code="invalid_request", detail=str(exc))

    @app.exception_handler(ConfigurationError)
    async def _misconfigured(request: Request, exc: ConfigurationError) -> JSONResponse:
        # A bad config file is the operator's problem, not the caller's, and it is not
        # something a retry will fix — so it is a 503 with the reason logged, not a 500.
        logger.error("configuration_error", error=str(exc), path=request.url.path)
        return problem_response(
            request,
            status_code=503,
            code="misconfigured",
            detail="The service is misconfigured and cannot answer this request.",
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                "location": list(error.get("loc", ())),
                "message": error.get("msg", ""),
                "type": error.get("type", ""),
            }
            for error in exc.errors()
        ]
        return problem_response(
            request,
            status_code=422,
            code="validation_failed",
            detail="One or more parameters were rejected.",
            errors=errors,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem_response(
            request,
            status_code=exc.status_code,
            code=_STATUS_TITLES.get(exc.status_code, "error").lower().replace(" ", "_"),
            detail=str(exc.detail) if exc.detail else None,
        )

    @app.exception_handler(LeadMindError)
    async def _domain(request: Request, exc: LeadMindError) -> JSONResponse:
        logger.error("domain_error", error=str(exc), path=request.url.path)
        return problem_response(
            request,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="internal_error",
            detail="The request could not be completed.",
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001
        # The message goes to the log with the request id; the client gets the id and nothing
        # else. An unhandled exception's text is as likely to contain a connection string as
        # anything useful.
        logger.exception("unhandled_exception", path=request.url.path)
        return problem_response(
            request,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="internal_error",
            detail="The request could not be completed. Quote the request_id when reporting it.",
        )
