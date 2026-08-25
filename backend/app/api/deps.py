"""Shared dependencies: the database session and pagination.

The session dependency mirrors ``app.db.session.session_scope`` rather than reimplementing it:
commit on success, roll back on any exception, always close. Committing a read-only transaction
costs nothing and means a write endpoint cannot forget.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.session import get_session_factory


def get_db() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


@dataclass(frozen=True, slots=True)
class Pagination:
    page: int
    page_size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def pagination_params(
    page: Annotated[int, Query(ge=1, description="1-based page number")] = 1,
    page_size: Annotated[
        int | None,
        Query(
            ge=1,
            description="Rows per page. Capped by LEADMIND_API_MAX_PAGE_SIZE — an unbounded "
            "list endpoint is a denial of service with extra steps.",
        ),
    ] = None,
) -> Pagination:
    settings = get_settings()
    size = page_size or settings.api_default_page_size
    return Pagination(page=page, page_size=min(size, settings.api_max_page_size))


PageParams = Annotated[Pagination, Depends(pagination_params)]
