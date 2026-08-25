"""Response envelopes shared by every endpoint.

Two things live here because getting them wrong once is worse than getting them right everywhere:
the pagination envelope, and the error body.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """A page of results, always carrying the information needed to fetch the next one.

    ``total`` is a real ``COUNT`` over the same filter, not an estimate. That costs a second
    query per request and is the right trade at this corpus size: a review UI that cannot say
    "73 pairs pending" is materially less useful, and 2 351 rows do not make counting expensive.
    Offset paging is likewise chosen for the size of this dataset — it is deterministic here only
    because every sort ends in a tiebreak on ``id``. Without that tiebreak, rows with equal sort
    keys are free to swap between pages and a reviewer silently never sees some of them.
    """

    items: list[T]
    total: int = Field(description="Rows matching the filter, ignoring pagination")
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    pages: int = Field(ge=0, description="Total number of pages at this page size")
    has_next: bool
    has_previous: bool

    @classmethod
    def build(cls, items: list[T], *, total: int, page: int, page_size: int) -> Page[T]:
        pages = (total + page_size - 1) // page_size
        return cls(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            pages=pages,
            has_next=page < pages,
            has_previous=page > 1,
        )


class Problem(BaseModel):
    """RFC 9457 problem details.

    Errors are a documented part of the interface, not an accident of the framework. Every
    failure comes back in this shape with the ``request_id`` that appears in the server logs, so
    a bug report is one string rather than a screenshot.
    """

    type: str = Field(default="about:blank", description="Stable, machine-readable error code")
    title: str
    status: int
    detail: str | None = None
    instance: str | None = Field(default=None, description="The path that failed")
    request_id: str | None = None
    errors: list[dict[str, Any]] | None = Field(
        default=None, description="Field-level validation failures, when the error is a 422"
    )


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str


class ReadinessResponse(BaseModel):
    """Whether this process can actually serve traffic.

    Separate from liveness on purpose. A process with an unreachable database is alive and should
    not be restarted; it is not ready and should not receive requests. Collapsing the two makes a
    database blip look like a crash loop.
    """

    status: str
    database: str
    schema_revision: str | None = None
    expected_revision: str | None = None
    schema_current: bool
    detail: str | None = None
