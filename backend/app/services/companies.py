"""Company queries — the franchise view, plus the taxonomy lists a filter UI needs."""

from __future__ import annotations

import uuid
from typing import Any, Final

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, joinedload

from app.ingestion.quality.rubric import get_rubric
from app.models import Category, Company, DataQualityScore, Lead, Location
from app.schemas.companies import (
    CategoryCount,
    CompanyDetail,
    CompanySummary,
    LocationCount,
)
from app.services.leads import summarise_leads

SORT_FIELDS: Final[frozenset[str]] = frozenset({"branches", "domain", "created", "quality"})
DEFAULT_SORT: Final[str] = "-branches"


def _branch_count_subquery() -> Any:
    return (
        select(func.count())
        .select_from(Lead)
        .where(Lead.company_id == Company.id, Lead.merged_into_id.is_(None))
        .correlate(Company)
        .scalar_subquery()
    )


def _mean_quality_subquery(rubric_version: str) -> Any:
    return (
        select(func.avg(DataQualityScore.score))
        .select_from(DataQualityScore)
        .join(Lead, Lead.id == DataQualityScore.lead_id)
        .where(
            Lead.company_id == Company.id,
            Lead.merged_into_id.is_(None),
            DataQualityScore.rubric_version == rubric_version,
        )
        .correlate(Company)
        .scalar_subquery()
    )


def build_company_query(
    *, q: str | None = None, min_branches: int | None = None
) -> Select[tuple[Company]]:
    query: Select[tuple[Company]] = select(Company)
    if q:
        pattern = f"%{q.strip()}%"
        query = query.where(Company.primary_domain.ilike(pattern) | Company.name.ilike(pattern))
    if min_branches is not None and min_branches > 1:
        query = query.where(_branch_count_subquery() >= min_branches)
    return query


def list_companies(
    session: Session,
    *,
    q: str | None = None,
    min_branches: int | None = None,
    sort: str = DEFAULT_SORT,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[CompanySummary], int]:
    rubric_version = get_rubric().version
    base = build_company_query(q=q, min_branches=min_branches)
    total = int(session.scalar(base.with_only_columns(func.count(Company.id)).order_by(None)) or 0)

    descending = sort.startswith("-")
    field_name = sort.lstrip("-") or "branches"
    if field_name not in SORT_FIELDS:
        field_name = "branches"
    column = {
        "branches": _branch_count_subquery(),
        "domain": Company.primary_domain,
        "created": Company.created_at,
        "quality": _mean_quality_subquery(rubric_version),
    }[field_name]
    ordering = column.desc().nullslast() if descending else column.asc().nullslast()
    query = (
        base.order_by(ordering, Company.id.asc())
        .options(joinedload(Company.leads).joinedload(Lead.location))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    companies = list(session.scalars(query).unique().all())
    summaries: list[CompanySummary] = []
    for company in companies:
        branches = [lead for lead in company.leads if lead.merged_into_id is None]
        mean = session.scalar(
            select(func.avg(DataQualityScore.score))
            .join(Lead, Lead.id == DataQualityScore.lead_id)
            .where(
                Lead.company_id == company.id,
                Lead.merged_into_id.is_(None),
                DataQualityScore.rubric_version == rubric_version,
            )
        )
        summaries.append(
            CompanySummary(
                id=company.id,
                primary_domain=company.primary_domain,
                name=company.name,
                website_url=company.website_url,
                branch_count=len(branches),
                locations=sorted(
                    {lead.location.name for lead in branches if lead.location is not None}
                ),
                mean_quality=round(float(mean), 2) if mean is not None else None,
                created_at=company.created_at,
            )
        )
    return summaries, total


def get_company(session: Session, company_id: uuid.UUID) -> CompanyDetail | None:
    company = (
        session.scalars(
            select(Company)
            .where(Company.id == company_id)
            .options(
                joinedload(Company.leads).joinedload(Lead.location),
                joinedload(Company.leads).joinedload(Lead.category),
                joinedload(Company.leads).joinedload(Lead.company),
            )
        )
        .unique()
        .one_or_none()
    )
    if company is None:
        return None

    rubric_version = get_rubric().version
    branches = [lead for lead in company.leads if lead.merged_into_id is None]
    summaries = summarise_leads(session, branches, rubric_version=rubric_version)
    scores = [s.quality.score for s in summaries.values() if s.quality is not None]

    return CompanyDetail(
        id=company.id,
        primary_domain=company.primary_domain,
        name=company.name,
        website_url=company.website_url,
        branch_count=len(branches),
        locations=sorted({lead.location.name for lead in branches if lead.location is not None}),
        mean_quality=round(sum(scores) / len(scores), 2) if scores else None,
        created_at=company.created_at,
        industry=company.industry,
        company_size=company.company_size,
        description=company.description,
        leads=sorted(summaries.values(), key=lambda lead: lead.display_name),
    )


def list_categories(session: Session) -> list[CategoryCount]:
    """Verticals with live lead counts — what a filter dropdown needs, in one query."""
    rows = session.execute(
        select(
            Category.slug,
            Category.label,
            Category.parent_slug,
            func.count(Lead.id).filter(Lead.merged_into_id.is_(None)),
        )
        .outerjoin(Lead, Lead.category_id == Category.id)
        .group_by(Category.slug, Category.label, Category.parent_slug)
        .order_by(func.count(Lead.id).desc(), Category.slug)
    ).all()
    return [
        CategoryCount(slug=slug, label=label, parent_slug=parent, lead_count=int(count))
        for slug, label, parent, count in rows
    ]


def list_locations(session: Session, *, limit: int | None = None) -> list[LocationCount]:
    query = (
        select(
            Location.slug,
            Location.name,
            Location.state,
            func.count(Lead.id).filter(Lead.merged_into_id.is_(None)),
        )
        .outerjoin(Lead, Lead.location_id == Location.id)
        .group_by(Location.slug, Location.name, Location.state)
        .order_by(func.count(Lead.id).desc(), Location.name)
    )
    if limit is not None:
        query = query.limit(limit)
    return [
        LocationCount(slug=slug, name=name, state=state, lead_count=int(count))
        for slug, name, state, count in session.execute(query).all()
    ]
