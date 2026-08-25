"""Lead endpoints: list, detail, and the three sub-resources that make a lead explainable."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.api.deps import DbSession, PageParams
from app.core.errors import NotFoundError
from app.models.enums import EntityKind, IdentifierKind
from app.schemas.common import Page
from app.schemas.leads import LeadDetail, LeadQualityDetail, LeadSummary, ProvenanceOut
from app.services import leads as lead_service
from app.verification.types import VerificationStatus

router = APIRouter(prefix="/leads", tags=["leads"])

RUBRIC_HEADER = "X-Rubric-Version"


@router.get(
    "",
    response_model=Page[LeadSummary],
    summary="List leads",
    response_description="One page of leads, newest rubric scores attached",
)
def list_leads(
    session: DbSession,
    pagination: PageParams,
    response: Response,
    q: Annotated[
        str | None,
        Query(description="Substring match on name or any identifier value", max_length=200),
    ] = None,
    category: Annotated[
        list[str] | None, Query(description="Controlled vertical slug; repeatable")
    ] = None,
    location: Annotated[
        list[str] | None, Query(description="Gazetteer location slug; repeatable")
    ] = None,
    state: Annotated[list[str] | None, Query(description="Indian state; repeatable")] = None,
    entity_kind: Annotated[list[EntityKind] | None, Query()] = None,
    has: Annotated[
        list[IdentifierKind] | None,
        Query(description="Require these channels; repeatable and combined with AND"),
    ] = None,
    missing: Annotated[
        list[IdentifierKind] | None, Query(description="Require these channels to be absent")
    ] = None,
    min_quality: Annotated[float | None, Query(ge=0, le=100)] = None,
    max_quality: Annotated[float | None, Query(ge=0, le=100)] = None,
    min_followers: Annotated[int | None, Query(ge=0)] = None,
    max_followers: Annotated[int | None, Query(ge=0)] = None,
    owned_website: Annotated[
        bool | None,
        Query(description="Whether the lead has a domain of its own, as opposed to a social URL"),
    ] = None,
    mailbox_status: Annotated[
        VerificationStatus | None,
        Query(
            description="Measured MX outcome. `unreachable` means proven undeliverable; "
            "`unknown` means the resolver failed or the check has not run — they are not the "
            "same thing and this filter does not conflate them."
        ),
    ] = None,
    website_status: Annotated[VerificationStatus | None, Query()] = None,
    mail_provider: Annotated[
        str | None, Query(description="Mail host inferred from MX, e.g. `google`, `microsoft`")
    ] = None,
    placeholder_name: Annotated[
        bool | None, Query(description="Anonymised `Advertiser 13887200` names")
    ] = None,
    company_id: Annotated[uuid.UUID | None, Query()] = None,
    multi_branch: Annotated[
        bool | None, Query(description="Leads whose company has more than one branch")
    ] = None,
    issue_code: Annotated[
        str | None, Query(description="Leads carrying a given validation issue code")
    ] = None,
    include_merged: Annotated[
        bool,
        Query(
            description="Include leads a reviewer confirmed as duplicates. They are hidden by "
            "default and never deleted."
        ),
    ] = False,
    rubric_version: Annotated[
        str | None,
        Query(description="Score against a specific rubric version instead of the current one"),
    ] = None,
    sort: Annotated[
        str,
        Query(
            description="One of quality, followers, name, created, updated. Prefix with `-` for "
            "descending. Every ordering is tiebroken on id so paging is stable."
        ),
    ] = lead_service.DEFAULT_SORT,
) -> Page[LeadSummary]:
    filters = lead_service.LeadFilters(
        q=q,
        categories=tuple(category or ()),
        locations=tuple(location or ()),
        states=tuple(state or ()),
        entity_kinds=tuple(entity_kind or ()),
        has_channels=tuple(has or ()),
        missing_channels=tuple(missing or ()),
        min_quality=min_quality,
        max_quality=max_quality,
        min_followers=min_followers,
        max_followers=max_followers,
        owned_website=owned_website,
        mailbox_status=mailbox_status,
        website_status=website_status,
        mail_provider=mail_provider,
        placeholder_name=placeholder_name,
        company_id=company_id,
        multi_branch=multi_branch,
        issue_code=issue_code,
        include_merged=include_merged,
        rubric_version=rubric_version,
    )
    items, total, used_rubric = lead_service.list_leads(
        session,
        filters,
        sort=sort,
        page=pagination.page,
        page_size=pagination.page_size,
    )
    # The rubric that produced these scores travels in a header as well as in each row, so a
    # client comparing two responses can tell a scoring change from a data change.
    response.headers[RUBRIC_HEADER] = used_rubric
    return Page.build(items, total=total, page=pagination.page, page_size=pagination.page_size)


@router.get("/{lead_id}", response_model=LeadDetail, summary="One lead, with its gaps")
def get_lead(session: DbSession, lead_id: uuid.UUID) -> LeadDetail:
    lead = lead_service.get_lead(session, lead_id)
    if lead is None:
        raise NotFoundError("lead not found", lead_id=str(lead_id))
    return lead_service.to_detail(session, lead)


@router.get(
    "/{lead_id}/quality",
    response_model=LeadQualityDetail,
    summary="Why this lead scored what it scored",
)
def get_lead_quality(
    session: DbSession,
    lead_id: uuid.UUID,
    rubric_version: Annotated[str | None, Query()] = None,
) -> LeadQualityDetail:
    detail = lead_service.get_quality_detail(session, lead_id, rubric_version=rubric_version)
    if detail is None:
        raise NotFoundError(
            "no quality score for this lead and rubric version",
            lead_id=str(lead_id),
            rubric_version=rubric_version or "current",
        )
    return detail


@router.get(
    "/{lead_id}/provenance",
    response_model=ProvenanceOut,
    summary="The spreadsheet rows this lead came from",
)
def get_lead_provenance(session: DbSession, lead_id: uuid.UUID) -> ProvenanceOut:
    if lead_service.get_lead(session, lead_id) is None:
        raise NotFoundError("lead not found", lead_id=str(lead_id))
    return lead_service.get_provenance(session, lead_id)
