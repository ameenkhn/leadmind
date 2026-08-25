"""Company endpoints, plus the taxonomy lists a filter UI needs to populate itself."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import DbSession, PageParams
from app.core.errors import NotFoundError
from app.schemas.common import Page
from app.schemas.companies import (
    CategoryCount,
    CompanyDetail,
    CompanySummary,
    LocationCount,
)
from app.services import companies as company_service

router = APIRouter(tags=["companies"])


@router.get("/companies", response_model=Page[CompanySummary], summary="List companies")
def list_companies(
    session: DbSession,
    pagination: PageParams,
    q: Annotated[str | None, Query(description="Substring match on domain or name")] = None,
    min_branches: Annotated[
        int | None,
        Query(ge=1, description="`2` gives the 28 multi-branch companies — franchises, mostly"),
    ] = None,
    sort: Annotated[
        str, Query(description="branches, domain, created or quality; prefix `-` for descending")
    ] = company_service.DEFAULT_SORT,
) -> Page[CompanySummary]:
    items, total = company_service.list_companies(
        session,
        q=q,
        min_branches=min_branches,
        sort=sort,
        page=pagination.page,
        page_size=pagination.page_size,
    )
    return Page.build(items, total=total, page=pagination.page, page_size=pagination.page_size)


@router.get(
    "/companies/{company_id}",
    response_model=CompanyDetail,
    summary="One company and all its branches",
)
def get_company(session: DbSession, company_id: uuid.UUID) -> CompanyDetail:
    company = company_service.get_company(session, company_id)
    if company is None:
        raise NotFoundError("company not found", company_id=str(company_id))
    return company


@router.get(
    "/meta/categories",
    response_model=list[CategoryCount],
    tags=["metadata"],
    summary="Controlled verticals with live lead counts",
)
def list_categories(session: DbSession) -> list[CategoryCount]:
    """The vertical taxonomy, not the 240 raw Facebook category strings it was built from."""
    return company_service.list_categories(session)


@router.get(
    "/meta/locations",
    response_model=list[LocationCount],
    tags=["metadata"],
    summary="Resolved locations with live lead counts",
)
def list_locations(
    session: DbSession,
    limit: Annotated[int | None, Query(ge=1, le=1000)] = None,
) -> list[LocationCount]:
    """Only gazetteer-resolved places appear here.

    The 16 city values that never resolved are kept on their leads verbatim and reported through
    `location.raw` with `resolved: false`, rather than being fuzzy-matched into a plausible wrong
    town and then offered here as if they were real.
    """
    return company_service.list_locations(session, limit=limit)
