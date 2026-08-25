"""Lead querying and serialisation.

Two things this module is careful about.

**Query count is constant per page, not proportional to it.** A page of leads needs the lead
rows, their identifiers, their follower observations, their quality score, their verification
status and their issue counts. Loading those per row is the N+1 that turns a 25-row page into
150 round trips. Everything here fetches the page first, then resolves each dependency in one
further query keyed on the page's ids.

**Filters are declared, not concatenated.** Every filter is a predicate built from a typed
:class:`LeadFilters`; nothing takes a string from the request and puts it into SQL. The free-text
search is a bound parameter against a trigram index, not an f-string.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import Select, and_, exists, func, or_, select
from sqlalchemy.orm import Session, aliased, joinedload, selectinload

from app.ingestion.quality.rubric import get_rubric
from app.models import (
    Category,
    DataQualityScore,
    DuplicateCandidate,
    Lead,
    LeadIdentifier,
    LeadSourceRecord,
    Location,
    MetricObservation,
    ValidationIssue,
)
from app.models.enums import EntityKind, IdentifierKind, MetricKind
from app.models.verification import DomainVerificationRecord, UrlVerificationRecord
from app.schemas.leads import (
    CategoryRef,
    CompanyRef,
    FollowersOut,
    IdentifierOut,
    LeadDetail,
    LeadQualityDetail,
    LeadSummary,
    LocationRef,
    MergeInfo,
    MetricOut,
    ProvenanceOut,
    QualityFactorOut,
    QualityPenaltyOut,
    QualityRef,
    SiblingOut,
    SourceQueryOut,
    SourceRecordOut,
    ValidationIssueOut,
    VerificationOut,
)
from app.verification.types import VerificationStatus

SORT_FIELDS: Final[frozenset[str]] = frozenset(
    {"quality", "followers", "name", "created", "updated"}
)
DEFAULT_SORT: Final[str] = "-quality"


@dataclass(frozen=True, slots=True)
class LeadFilters:
    """Everything the leads list can be narrowed by.

    A frozen dataclass rather than loose keyword arguments so the filter set is one testable
    value: the same object can be built from a request, from a test, or from a saved segment in
    a later phase without the query builder knowing the difference.
    """

    q: str | None = None
    categories: tuple[str, ...] = ()
    locations: tuple[str, ...] = ()
    states: tuple[str, ...] = ()
    entity_kinds: tuple[EntityKind, ...] = ()
    has_channels: tuple[IdentifierKind, ...] = ()
    missing_channels: tuple[IdentifierKind, ...] = ()
    min_quality: float | None = None
    max_quality: float | None = None
    min_followers: int | None = None
    max_followers: int | None = None
    owned_website: bool | None = None
    mailbox_status: VerificationStatus | None = None
    website_status: VerificationStatus | None = None
    mail_provider: str | None = None
    placeholder_name: bool | None = None
    company_id: uuid.UUID | None = None
    multi_branch: bool | None = None
    issue_code: str | None = None
    include_merged: bool = False
    rubric_version: str | None = None


@dataclass(slots=True)
class _PageContext:
    """Everything a page of leads needs, fetched once for the whole page."""

    identifiers: dict[uuid.UUID, list[LeadIdentifier]] = field(default_factory=dict)
    metrics: dict[uuid.UUID, list[MetricObservation]] = field(default_factory=dict)
    quality: dict[uuid.UUID, DataQualityScore] = field(default_factory=dict)
    verification: dict[uuid.UUID, dict[str, Any]] = field(default_factory=dict)
    issue_counts: dict[uuid.UUID, int] = field(default_factory=dict)
    branch_counts: dict[uuid.UUID, int] = field(default_factory=dict)


def active_rubric_version(filters: LeadFilters | None = None) -> str:
    if filters is not None and filters.rubric_version:
        return filters.rubric_version
    return get_rubric().version


def _latest_followers_subquery() -> Any:
    """The most recent follower observation for a lead.

    Most recent, not maximum: 63 leads were measured twice with different values, and taking the
    larger of the two would quietly report the more flattering scrape rather than the current one.
    """
    return (
        select(MetricObservation.value)
        .where(
            MetricObservation.lead_id == Lead.id,
            MetricObservation.metric == MetricKind.FOLLOWERS,
        )
        .order_by(MetricObservation.batch_sequence.desc(), MetricObservation.created_at.desc())
        .limit(1)
        .correlate(Lead)
        .scalar_subquery()
    )


def _has_channel(kind: IdentifierKind) -> Any:
    return exists().where(
        and_(
            LeadIdentifier.lead_id == Lead.id,
            LeadIdentifier.kind == kind,
            LeadIdentifier.is_valid.is_(True),
        )
    )


def _email_domain_status(status: VerificationStatus) -> Any:
    """Leads whose email domain has a given verification outcome.

    The join is on ``split_part(value, '@', 2)`` because the verification cache is keyed by
    domain: 2 520 addresses share 1 128 domains, and one ``gmail.com`` row answers for 1 269
    of them.
    """
    return exists().where(
        and_(
            LeadIdentifier.lead_id == Lead.id,
            LeadIdentifier.kind == IdentifierKind.EMAIL,
            DomainVerificationRecord.domain
            == func.split_part(LeadIdentifier.value_normalized, "@", 2),
            DomainVerificationRecord.status == status,
        )
    )


def _mail_provider_clause(provider: str) -> Any:
    return exists().where(
        and_(
            LeadIdentifier.lead_id == Lead.id,
            LeadIdentifier.kind == IdentifierKind.EMAIL,
            DomainVerificationRecord.domain
            == func.split_part(LeadIdentifier.value_normalized, "@", 2),
            DomainVerificationRecord.provider == provider,
        )
    )


def _website_status_clause(status: VerificationStatus) -> Any:
    return exists().where(
        and_(
            LeadIdentifier.lead_id == Lead.id,
            LeadIdentifier.kind == IdentifierKind.WEBSITE,
            UrlVerificationRecord.url == LeadIdentifier.value_normalized,
            UrlVerificationRecord.status == status,
        )
    )


def build_lead_query(filters: LeadFilters) -> Select[tuple[Lead]]:
    """Turn a filter set into a ``SELECT`` over leads."""
    query: Select[tuple[Lead]] = select(Lead)

    if not filters.include_merged:
        # A lead a reviewer confirmed as a duplicate is still a row, still queryable, still
        # counted in reconciliation — just not offered as a prospect.
        query = query.where(Lead.merged_into_id.is_(None))

    if filters.q:
        pattern = f"%{filters.q.strip()}%"
        query = query.where(
            or_(
                Lead.display_name.ilike(pattern),
                Lead.normalized_name.ilike(pattern),
                exists().where(
                    and_(
                        LeadIdentifier.lead_id == Lead.id,
                        LeadIdentifier.value_normalized.ilike(pattern),
                    )
                ),
            )
        )

    if filters.categories:
        query = query.where(
            Lead.category_id.in_(select(Category.id).where(Category.slug.in_(filters.categories)))
        )

    if filters.locations:
        query = query.where(
            Lead.location_id.in_(select(Location.id).where(Location.slug.in_(filters.locations)))
        )

    if filters.states:
        query = query.where(
            Lead.location_id.in_(select(Location.id).where(Location.state.in_(filters.states)))
        )

    if filters.entity_kinds:
        query = query.where(Lead.entity_kind.in_(filters.entity_kinds))

    if filters.placeholder_name is not None:
        query = query.where(Lead.is_placeholder_name.is_(filters.placeholder_name))

    if filters.company_id is not None:
        query = query.where(Lead.company_id == filters.company_id)

    if filters.owned_website is not None:
        # A company exists exactly when the lead has an owned domain: link aggregators and
        # social profiles are excluded at ingest and never key a company.
        query = query.where(
            Lead.company_id.is_not(None) if filters.owned_website else Lead.company_id.is_(None)
        )

    if filters.multi_branch is not None:
        sibling = aliased(Lead)
        branch_count = (
            select(func.count())
            .select_from(sibling)
            .where(sibling.company_id == Lead.company_id, sibling.merged_into_id.is_(None))
            .correlate(Lead)
            .scalar_subquery()
        )
        query = query.where(Lead.company_id.is_not(None))
        query = query.where(branch_count > 1 if filters.multi_branch else branch_count == 1)

    for kind in filters.has_channels:
        query = query.where(_has_channel(kind))
    for kind in filters.missing_channels:
        query = query.where(~_has_channel(kind))

    if filters.min_followers is not None or filters.max_followers is not None:
        followers = _latest_followers_subquery()
        if filters.min_followers is not None:
            query = query.where(followers >= filters.min_followers)
        if filters.max_followers is not None:
            query = query.where(followers <= filters.max_followers)

    if filters.min_quality is not None or filters.max_quality is not None:
        version = active_rubric_version(filters)
        score = (
            select(DataQualityScore.score)
            .where(
                DataQualityScore.lead_id == Lead.id,
                DataQualityScore.rubric_version == version,
            )
            .correlate(Lead)
            .scalar_subquery()
        )
        if filters.min_quality is not None:
            query = query.where(score >= filters.min_quality)
        if filters.max_quality is not None:
            query = query.where(score <= filters.max_quality)

    if filters.mailbox_status is not None:
        query = query.where(_email_domain_status(filters.mailbox_status))
    if filters.website_status is not None:
        query = query.where(_website_status_clause(filters.website_status))
    if filters.mail_provider is not None:
        query = query.where(_mail_provider_clause(filters.mail_provider))

    if filters.issue_code:
        query = query.where(
            exists().where(
                and_(
                    ValidationIssue.lead_id == Lead.id,
                    ValidationIssue.code == filters.issue_code,
                )
            )
        )

    return query


def apply_sort(
    query: Select[tuple[Lead]], sort: str, *, rubric_version: str
) -> Select[tuple[Lead]]:
    """Order the result set deterministically.

    Every ordering ends in ``Lead.id``. Without that tiebreak two leads with the same quality
    score can swap places between page 1 and page 2, and a reviewer paging through the list would
    see one twice and the other never — silently, with no error anywhere.
    """
    descending = sort.startswith("-")
    field_name = sort.lstrip("-") or "quality"
    if field_name not in SORT_FIELDS:
        field_name = "quality"

    if field_name == "quality":
        column: Any = (
            select(DataQualityScore.score)
            .where(
                DataQualityScore.lead_id == Lead.id,
                DataQualityScore.rubric_version == rubric_version,
            )
            .correlate(Lead)
            .scalar_subquery()
        )
    elif field_name == "followers":
        column = _latest_followers_subquery()
    elif field_name == "name":
        column = Lead.normalized_name
    elif field_name == "updated":
        column = Lead.updated_at
    else:
        column = Lead.created_at

    # NULLS LAST in both directions: an unscored lead is an absence of evidence and should not
    # be presented ahead of a measured one just because the sort flipped.
    ordering = column.desc().nullslast() if descending else column.asc().nullslast()
    return query.order_by(ordering, Lead.id.asc())


def count_leads(session: Session, filters: LeadFilters) -> int:
    query = build_lead_query(filters).with_only_columns(func.count(Lead.id)).order_by(None)
    return int(session.scalar(query) or 0)


def _load_page_context(
    session: Session, leads: Sequence[Lead], *, rubric_version: str
) -> _PageContext:
    context = _PageContext()
    if not leads:
        return context

    lead_ids = [lead.id for lead in leads]

    identifiers: dict[uuid.UUID, list[LeadIdentifier]] = defaultdict(list)
    for identifier in session.scalars(
        select(LeadIdentifier)
        .where(LeadIdentifier.lead_id.in_(lead_ids))
        .order_by(LeadIdentifier.kind, LeadIdentifier.is_primary.desc())
    ).all():
        identifiers[identifier.lead_id].append(identifier)
    context.identifiers = dict(identifiers)

    metrics: dict[uuid.UUID, list[MetricObservation]] = defaultdict(list)
    for metric in session.scalars(
        select(MetricObservation)
        .where(MetricObservation.lead_id.in_(lead_ids))
        .order_by(MetricObservation.batch_sequence.asc())
    ).all():
        metrics[metric.lead_id].append(metric)
    context.metrics = dict(metrics)

    context.quality = {
        score.lead_id: score
        for score in session.scalars(
            select(DataQualityScore).where(
                DataQualityScore.lead_id.in_(lead_ids),
                DataQualityScore.rubric_version == rubric_version,
            )
        ).all()
    }

    from app.verification.runner import lead_verification_signals

    context.verification = lead_verification_signals(session, lead_ids)

    context.issue_counts = {
        lead_id: count
        for lead_id, count in session.execute(
            select(ValidationIssue.lead_id, func.count())
            .where(ValidationIssue.lead_id.in_(lead_ids))
            .group_by(ValidationIssue.lead_id)
        ).all()
        if lead_id is not None
    }

    company_ids = [lead.company_id for lead in leads if lead.company_id is not None]
    if company_ids:
        context.branch_counts = {
            company_id: count
            for company_id, count in session.execute(
                select(Lead.company_id, func.count())
                .where(Lead.company_id.in_(company_ids), Lead.merged_into_id.is_(None))
                .group_by(Lead.company_id)
            ).all()
            if company_id is not None
        }

    return context


def _primary(identifiers: Sequence[LeadIdentifier], kind: IdentifierKind) -> str | None:
    for identifier in identifiers:
        if identifier.kind == kind and identifier.is_primary:
            return identifier.value_normalized
    return next(
        (i.value_normalized for i in identifiers if i.kind == kind),
        None,
    )


def _followers(observations: Sequence[MetricObservation]) -> FollowersOut | None:
    follower_rows = [o for o in observations if o.metric == MetricKind.FOLLOWERS]
    if not follower_rows:
        return None
    latest = follower_rows[-1]
    values = {row.value for row in follower_rows}
    return FollowersOut(
        value=latest.value,
        value_raw=latest.value_raw,
        batch=latest.batch,
        observations=len(follower_rows),
        changed=len(values) > 1,
    )


def _location(lead: Lead) -> LocationRef | None:
    if lead.location is None and not lead.location_raw:
        return None
    location = lead.location
    return LocationRef(
        slug=location.slug if location else None,
        name=location.name if location else None,
        state=location.state if location else None,
        raw=lead.location_raw,
        confidence=lead.location_confidence,
        resolved=location is not None,
    )


def _category(lead: Lead) -> CategoryRef | None:
    if lead.category is None:
        return None
    return CategoryRef(
        slug=lead.category.slug,
        label=lead.category.label,
        parent_slug=lead.category.parent_slug,
    )


def to_summary(lead: Lead, context: _PageContext) -> LeadSummary:
    identifiers = context.identifiers.get(lead.id, [])
    score = context.quality.get(lead.id)
    return LeadSummary(
        id=lead.id,
        display_name=lead.display_name,
        entity_kind=lead.entity_kind,
        is_placeholder_name=lead.is_placeholder_name,
        company=(
            CompanyRef(
                id=lead.company.id,
                primary_domain=lead.company.primary_domain,
                name=lead.company.name,
                branch_count=context.branch_counts.get(lead.company.id, 1),
            )
            if lead.company is not None
            else None
        ),
        category=_category(lead),
        fb_category_raw=lead.fb_category_raw,
        location=_location(lead),
        channels=sorted({i.kind for i in identifiers}, key=lambda k: k.value),
        primary_email=_primary(identifiers, IdentifierKind.EMAIL),
        primary_phone=_primary(identifiers, IdentifierKind.PHONE),
        website=_primary(identifiers, IdentifierKind.WEBSITE),
        followers=_followers(context.metrics.get(lead.id, [])),
        quality=(
            QualityRef(
                score=score.score,
                rubric_version=score.rubric_version,
                factors_evaluated=int(score.factors.get("factors_evaluated", 0)),
            )
            if score is not None
            else None
        ),
        verification=VerificationOut.from_signals(
            context.verification.get(lead.id),
            has_email=any(i.kind is IdentifierKind.EMAIL for i in identifiers),
            has_website=any(i.kind is IdentifierKind.WEBSITE for i in identifiers),
        ),
        issue_count=context.issue_counts.get(lead.id, 0),
        merged=(
            MergeInfo(
                merged_into_id=lead.merged_into_id,
                merged_at=lead.merged_at,
                merged_by=lead.merged_by,
            )
            if lead.merged_into_id is not None
            else None
        ),
        created_at=lead.created_at,
        updated_at=lead.updated_at,
    )


def summarise_leads(
    session: Session, leads: Sequence[Lead], *, rubric_version: str | None = None
) -> dict[uuid.UUID, LeadSummary]:
    """Summarise an arbitrary set of leads with the same constant-query-count guarantee.

    Used by the review queue, which embeds both sides of every pair: a reviewer who has to fetch
    two more URLs per row to see what they are deciding will not review anything.
    """
    version = rubric_version or get_rubric().version
    context = _load_page_context(session, leads, rubric_version=version)
    return {lead.id: to_summary(lead, context) for lead in leads}


def list_leads(
    session: Session,
    filters: LeadFilters,
    *,
    sort: str = DEFAULT_SORT,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[LeadSummary], int, str]:
    """Return one page of leads, the total matching the filter, and the rubric used."""
    rubric_version = active_rubric_version(filters)
    total = count_leads(session, filters)

    query = apply_sort(build_lead_query(filters), sort, rubric_version=rubric_version)
    query = query.options(
        joinedload(Lead.company), joinedload(Lead.category), joinedload(Lead.location)
    )
    query = query.offset((page - 1) * page_size).limit(page_size)

    leads = list(session.scalars(query).unique().all())
    context = _load_page_context(session, leads, rubric_version=rubric_version)
    return [to_summary(lead, context) for lead in leads], total, rubric_version


def get_lead(session: Session, lead_id: uuid.UUID) -> Lead | None:
    return (
        session.scalars(
            select(Lead)
            .where(Lead.id == lead_id)
            .options(
                joinedload(Lead.company),
                joinedload(Lead.category),
                joinedload(Lead.location),
                selectinload(Lead.source_queries),
            )
        )
        .unique()
        .one_or_none()
    )


def to_detail(session: Session, lead: Lead, *, rubric_version: str | None = None) -> LeadDetail:
    version = rubric_version or get_rubric().version
    context = _load_page_context(session, [lead], rubric_version=version)
    summary = to_summary(lead, context)

    issues = session.scalars(
        select(ValidationIssue)
        .where(ValidationIssue.lead_id == lead.id)
        .order_by(ValidationIssue.field, ValidationIssue.code)
    ).all()

    siblings: list[SiblingOut] = []
    if lead.company_id is not None:
        sibling_leads = (
            session.scalars(
                select(Lead)
                .where(
                    Lead.company_id == lead.company_id,
                    Lead.id != lead.id,
                    Lead.merged_into_id.is_(None),
                )
                .options(joinedload(Lead.location))
                .order_by(Lead.normalized_name)
            )
            .unique()
            .all()
        )
        sibling_context = _load_page_context(session, list(sibling_leads), rubric_version=version)
        siblings = [
            SiblingOut(
                id=sibling.id,
                display_name=sibling.display_name,
                location=sibling.location.name if sibling.location else sibling.location_raw,
                primary_email=_primary(
                    sibling_context.identifiers.get(sibling.id, []), IdentifierKind.EMAIL
                ),
            )
            for sibling in sibling_leads
        ]

    candidate_count = int(
        session.scalar(
            select(func.count())
            .select_from(DuplicateCandidate)
            .where(
                or_(
                    DuplicateCandidate.lead_a_id == lead.id,
                    DuplicateCandidate.lead_b_id == lead.id,
                )
            )
        )
        or 0
    )

    return LeadDetail(
        **summary.model_dump(),
        identifiers=[
            IdentifierOut(
                kind=i.kind,
                value=i.value_normalized,
                value_raw=i.value_raw,
                is_primary=i.is_primary,
                is_valid=i.is_valid,
                confidence=i.confidence,
                attributes=i.attributes,
            )
            for i in context.identifiers.get(lead.id, [])
        ],
        metrics=[
            MetricOut(
                metric=m.metric.value,
                value=m.value,
                value_raw=m.value_raw,
                batch=m.batch,
                observed_at=m.observed_at,
                source=m.source,
            )
            for m in context.metrics.get(lead.id, [])
        ],
        source_queries=[
            SourceQueryOut(query=q.query, source=q.source, source_is_inferred=q.source_is_inferred)
            for q in lead.source_queries
        ],
        issues=[
            ValidationIssueOut(
                field=i.field,
                code=i.code,
                severity=i.severity,
                message=i.message,
                value_raw=i.value_raw,
            )
            for i in issues
        ],
        siblings=siblings,
        duplicate_candidate_count=candidate_count,
        niche_raw=lead.niche_raw,
        first_seen_at=lead.first_seen_at,
        last_seen_at=lead.last_seen_at,
    )


def get_quality_detail(
    session: Session, lead_id: uuid.UUID, *, rubric_version: str | None = None
) -> LeadQualityDetail | None:
    """Read back the stored explanation for a score, rather than recomputing it."""
    version = rubric_version or get_rubric().version
    score = session.scalars(
        select(DataQualityScore).where(
            DataQualityScore.lead_id == lead_id,
            DataQualityScore.rubric_version == version,
        )
    ).one_or_none()
    if score is None:
        return None

    rubric = get_rubric()
    payload: dict[str, Any] = score.factors
    factors_payload: dict[str, Any] = payload.get("factors", {})
    penalties_payload: dict[str, Any] = payload.get("penalties", {})

    return LeadQualityDetail(
        lead_id=lead_id,
        score=score.score,
        rubric_version=score.rubric_version,
        computed_at=score.computed_at,
        base_score=float(payload.get("base_score", score.score)),
        penalty_total=float(payload.get("penalty_total", 0.0)),
        penalty_applied=float(payload.get("penalty_applied", 0.0)),
        factors_evaluated=int(payload.get("factors_evaluated", 0)),
        factors_total=int(payload.get("factors_total", len(factors_payload))),
        weight_available=float(payload.get("weight_available", 0.0)),
        factors=[
            QualityFactorOut(
                name=name,
                value=detail.get("value"),
                weight=float(detail.get("weight", 0.0)),
                contribution=float(detail.get("contribution", 0.0)),
                reason=str(detail.get("reason", "")),
                measured=bool(detail.get("measured", detail.get("value") is not None)),
                description=rubric.descriptions.get(name),
            )
            for name, detail in factors_payload.items()
        ],
        penalties=[
            QualityPenaltyOut(
                name=name,
                amount=float(detail.get("amount", 0.0)),
                triggered_by=str(detail.get("triggered_by", "")),
                reason=str(detail.get("reason", "")),
            )
            for name, detail in penalties_payload.items()
        ],
    )


def get_provenance(session: Session, lead_id: uuid.UUID) -> ProvenanceOut:
    records = session.scalars(
        select(LeadSourceRecord)
        .where(LeadSourceRecord.lead_id == lead_id)
        .order_by(LeadSourceRecord.source_sheet, LeadSourceRecord.source_row_no)
    ).all()
    return ProvenanceOut(
        lead_id=lead_id,
        source_records=[
            SourceRecordOut(
                source_file=record.source_file,
                source_sheet=record.source_sheet,
                source_row_no=record.source_row_no,
                source_serial=record.source_serial,
                row_sha256=record.row_sha256,
                raw=record.raw,
            )
            for record in records
        ],
    )
