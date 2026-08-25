"""Companies — the franchise view.

Worth its own resource because the shared-domain relationship is the one Phase 1 refused to
collapse. 28 companies own more than one lead; ``Pumo Technovation`` owns five. A leads list
alone cannot show that, and a system that cannot show it will eventually be asked to merge it.
"""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, Field

from app.schemas.leads import LeadSummary


class CompanySummary(BaseModel):
    id: uuid.UUID
    primary_domain: str
    name: str | None = None
    website_url: str | None = None
    branch_count: int = Field(description="Leads sharing this domain")
    locations: list[str] = Field(
        default_factory=list, description="Distinct resolved cities across the branches"
    )
    mean_quality: float | None = None
    created_at: dt.datetime


class CompanyDetail(CompanySummary):
    industry: str | None = Field(
        default=None,
        description="Empty across this dataset. The source workbook contains no firmographics "
        "at all; Phase 3 enrichment fills this from the crawl.",
    )
    company_size: str | None = None
    description: str | None = None
    leads: list[LeadSummary] = Field(default_factory=list)


class CategoryCount(BaseModel):
    slug: str
    label: str
    parent_slug: str | None = None
    lead_count: int


class LocationCount(BaseModel):
    slug: str
    name: str
    state: str | None = None
    lead_count: int
