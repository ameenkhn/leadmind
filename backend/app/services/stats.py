"""Corpus statistics, read live from the database.

Every number a README or a dashboard quotes about this system should be answerable by a query
rather than by a human's memory of the last run. That is the entire reason this module exists:
the reconciliation identity, the quality distribution, verification coverage and review-queue
precision are all facts about the current database, not facts about the day the pipeline ran.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ingestion.quality.rubric import get_rubric
from app.models import (
    Company,
    DataQualityScore,
    DuplicateCandidate,
    EvalLabel,
    IngestRun,
    Lead,
    LeadIdentifier,
    LeadSourceQuery,
    MetricObservation,
    ValidationIssue,
)
from app.models.enums import DuplicateStatus, IdentifierKind, IngestStatus
from app.models.verification import DomainVerificationRecord, UrlVerificationRecord
from app.schemas.stats import (
    CorpusStats,
    MethodReviewStats,
    QualityStats,
    ReconciliationOut,
    ReviewStats,
    VerificationCoverage,
    VerificationStats,
)
from app.verification.types import MailProvider, VerificationStatus

QUALITY_BANDS: tuple[tuple[str, float, float], ...] = (
    ("0-19", 0.0, 20.0),
    ("20-39", 20.0, 40.0),
    ("40-59", 40.0, 60.0),
    ("60-79", 60.0, 80.0),
    ("80-100", 80.0, 100.01),
)


def _count(session: Session, model: Any) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def latest_run(session: Session) -> IngestRun | None:
    return session.scalars(
        select(IngestRun)
        .where(IngestRun.status == IngestStatus.SUCCEEDED)
        .order_by(IngestRun.started_at.desc())
        .limit(1)
    ).one_or_none()


def reconciliation(session: Session) -> ReconciliationOut | None:
    """The last successful ingest's row accounting.

    Read from ``ingest_runs`` rather than recomputed, because the claim being made is about that
    run: *these* 2 520 rows produced *these* leads. Recomputing it from the current tables would
    answer a different question and would keep reporting success after someone deleted a lead
    by hand.
    """
    run = latest_run(session)
    if run is None:
        return None
    stats: dict[str, Any] = run.stats or {}
    rows_read = int(stats.get("rows_read", 0))
    rows_merged = int(stats.get("rows_merged", 0))
    leads_total = int(stats.get("leads_total", 0))
    return ReconciliationOut(
        rows_read=rows_read,
        rows_merged=rows_merged,
        leads_total=leads_total,
        reconciles=rows_read == leads_total + rows_merged,
        source_file=run.source_file,
        source_sha256=run.source_sha256,
        ingested_at=run.finished_at or run.started_at,
        code_version=run.code_version,
    )


def corpus_stats(session: Session) -> CorpusStats:
    identifiers_by_kind = {
        kind.value: int(count)
        for kind, count in session.execute(
            select(LeadIdentifier.kind, func.count()).group_by(LeadIdentifier.kind)
        ).all()
    }
    leads_by_entity_kind = {
        kind.value: int(count)
        for kind, count in session.execute(
            select(Lead.entity_kind, func.count())
            .where(Lead.merged_into_id.is_(None))
            .group_by(Lead.entity_kind)
        ).all()
    }
    issues_by_code = {
        code: int(count)
        for code, count in session.execute(
            select(ValidationIssue.code, func.count())
            .group_by(ValidationIssue.code)
            .order_by(func.count().desc())
        ).all()
    }
    labels_by_source = {
        source.value: int(count)
        for source, count in session.execute(
            select(EvalLabel.label_source, func.count()).group_by(EvalLabel.label_source)
        ).all()
    }

    multi_branch = int(
        session.scalar(
            select(func.count()).select_from(
                select(Lead.company_id)
                .where(Lead.company_id.is_not(None), Lead.merged_into_id.is_(None))
                .group_by(Lead.company_id)
                .having(func.count() > 1)
                .subquery()
            )
        )
        or 0
    )

    active_leads = select(func.count()).select_from(Lead).where(Lead.merged_into_id.is_(None))

    return CorpusStats(
        leads=int(session.scalar(active_leads) or 0),
        leads_merged_by_review=int(
            session.scalar(
                select(func.count()).select_from(Lead).where(Lead.merged_into_id.is_not(None))
            )
            or 0
        ),
        companies=_count(session, Company),
        companies_multi_branch=multi_branch,
        identifiers=_count(session, LeadIdentifier),
        identifiers_by_kind=identifiers_by_kind,
        leads_by_entity_kind=leads_by_entity_kind,
        metric_observations=_count(session, MetricObservation),
        source_queries=_count(session, LeadSourceQuery),
        eval_labels_by_source=labels_by_source,
        validation_issues=_count(session, ValidationIssue),
        validation_issues_by_code=issues_by_code,
        leads_with_owned_website=int(
            session.scalar(
                select(func.count())
                .select_from(Lead)
                .where(Lead.merged_into_id.is_(None), Lead.company_id.is_not(None))
            )
            or 0
        ),
        leads_with_location=int(
            session.scalar(
                select(func.count())
                .select_from(Lead)
                .where(Lead.merged_into_id.is_(None), Lead.location_id.is_not(None))
            )
            or 0
        ),
        leads_with_category=int(
            session.scalar(
                select(func.count())
                .select_from(Lead)
                .where(Lead.merged_into_id.is_(None), Lead.category_id.is_not(None))
            )
            or 0
        ),
        reconciliation=reconciliation(session),
    )


def quality_stats(session: Session, *, rubric_version: str | None = None) -> QualityStats:
    version = rubric_version or get_rubric().version
    scores = list(
        session.scalars(
            select(DataQualityScore.score)
            .join(Lead, Lead.id == DataQualityScore.lead_id)
            .where(
                DataQualityScore.rubric_version == version,
                Lead.merged_into_id.is_(None),
            )
            .order_by(DataQualityScore.score)
        ).all()
    )

    histogram = {label: 0 for label, _, _ in QUALITY_BANDS}
    for score in scores:
        for label, low, high in QUALITY_BANDS:
            if low <= score < high:
                histogram[label] += 1
                break

    # Projected in a subquery and grouped by the label, not by the JSONB expression itself:
    # repeating `factors ->> 'factors_evaluated'` in SELECT and GROUP BY emits two different
    # bind parameters, and Postgres then rejects them as non-matching expressions.
    evaluated = (
        select(
            DataQualityScore.factors["factors_evaluated"].as_integer().label("evaluated"),
        )
        .join(Lead, Lead.id == DataQualityScore.lead_id)
        .where(
            DataQualityScore.rubric_version == version,
            Lead.merged_into_id.is_(None),
        )
        .subquery()
    )
    factors_evaluated = {
        str(int(count)): int(leads)
        for count, leads in session.execute(
            select(evaluated.c.evaluated, func.count())
            .group_by(evaluated.c.evaluated)
            .order_by(evaluated.c.evaluated)
        ).all()
        if count is not None
    }

    def percentile(fraction: float) -> float | None:
        if not scores:
            return None
        index = min(len(scores) - 1, max(0, round(fraction * (len(scores) - 1))))
        return round(scores[index], 2)

    return QualityStats(
        rubric_version=version,
        scored_leads=len(scores),
        mean=round(sum(scores) / len(scores), 2) if scores else None,
        median=percentile(0.5),
        p10=percentile(0.1),
        p90=percentile(0.9),
        histogram=histogram,
        factors_evaluated=factors_evaluated,
    )


def _coverage(session: Session, kind: str, model: Any, now: dt.datetime) -> VerificationCoverage:
    total = _count(session, model)
    fresh = int(
        session.scalar(select(func.count()).select_from(model).where(model.expires_at > now)) or 0
    )
    by_status = {
        status.value: int(count)
        for status, count in session.execute(
            select(model.status, func.count()).group_by(model.status)
        ).all()
    }
    return VerificationCoverage(
        kind=kind, records=total, fresh=fresh, stale=total - fresh, by_status=by_status
    )


def verification_stats(session: Session) -> VerificationStats:
    now = dt.datetime.now(dt.UTC)
    email_domain = func.split_part(LeadIdentifier.value_normalized, "@", 2)

    def leads_where(*clauses: Any) -> int:
        return int(
            session.scalar(
                select(func.count(func.distinct(LeadIdentifier.lead_id)))
                .select_from(LeadIdentifier)
                .join(Lead, Lead.id == LeadIdentifier.lead_id)
                .join(DomainVerificationRecord, DomainVerificationRecord.domain == email_domain)
                .where(
                    LeadIdentifier.kind == IdentifierKind.EMAIL,
                    Lead.merged_into_id.is_(None),
                    *clauses,
                )
            )
            or 0
        )

    live_websites = int(
        session.scalar(
            select(func.count(func.distinct(LeadIdentifier.lead_id)))
            .select_from(LeadIdentifier)
            .join(Lead, Lead.id == LeadIdentifier.lead_id)
            .join(
                UrlVerificationRecord, UrlVerificationRecord.url == LeadIdentifier.value_normalized
            )
            .where(
                LeadIdentifier.kind == IdentifierKind.WEBSITE,
                Lead.merged_into_id.is_(None),
                UrlVerificationRecord.is_live.is_(True),
            )
        )
        or 0
    )

    managed = [
        MailProvider(name)
        for name in get_rubric().managed_mail_providers
        if name in set(MailProvider)
    ]

    return VerificationStats(
        coverage=[
            _coverage(session, "email_domains", DomainVerificationRecord, now),
            _coverage(session, "websites", UrlVerificationRecord, now),
        ],
        leads_with_verified_mailbox=leads_where(
            DomainVerificationRecord.status == VerificationStatus.VERIFIED,
            DomainVerificationRecord.has_mx.is_(True),
        ),
        leads_proven_undeliverable=leads_where(
            DomainVerificationRecord.status == VerificationStatus.UNREACHABLE
        ),
        leads_with_live_website=live_websites,
        leads_on_managed_mail=(
            # Freemail is excluded deliberately. gmail.com's MX is Google's, so counting by
            # provider alone would report every personal Gmail address as a business paying for
            # managed email — inflating the one digital-maturity signal this dataset has.
            leads_where(
                DomainVerificationRecord.provider.in_(managed),
                DomainVerificationRecord.is_freemail.is_(False),
            )
            if managed
            else 0
        ),
    )


def review_stats(session: Session) -> ReviewStats:
    """Queue depth, and what the decisions say about the detectors that filled it.

    ``confirm_rate`` is the point of this endpoint. It is the observed precision of a detection
    method on the pairs a human has actually looked at — which is the number the fuzzy-name
    threshold should be tuned against, rather than a value picked because 92 sounded strict.
    """
    rows = session.execute(
        select(
            DuplicateCandidate.method,
            DuplicateCandidate.status,
            func.count(),
            func.avg(DuplicateCandidate.confidence),
        ).group_by(DuplicateCandidate.method, DuplicateCandidate.status)
    ).all()

    by_method: dict[str, dict[str, Any]] = {}
    for method, status, count, mean_confidence in rows:
        entry = by_method.setdefault(
            method.value,
            {
                "pending": 0,
                "confirmed_duplicate": 0,
                "rejected_distinct": 0,
                "mean_confidence_confirmed": None,
                "mean_confidence_rejected": None,
            },
        )
        entry[status.value] = int(count)
        if status is DuplicateStatus.CONFIRMED_DUPLICATE and mean_confidence is not None:
            entry["mean_confidence_confirmed"] = round(float(mean_confidence), 4)
        if status is DuplicateStatus.REJECTED_DISTINCT and mean_confidence is not None:
            entry["mean_confidence_rejected"] = round(float(mean_confidence), 4)

    methods: list[MethodReviewStats] = []
    for method_name, entry in sorted(by_method.items()):
        decided = entry["confirmed_duplicate"] + entry["rejected_distinct"]
        methods.append(
            MethodReviewStats(
                method=method_name,
                pending=entry["pending"],
                confirmed_duplicate=entry["confirmed_duplicate"],
                rejected_distinct=entry["rejected_distinct"],
                confirm_rate=(
                    round(entry["confirmed_duplicate"] / decided, 4) if decided else None
                ),
                mean_confidence_confirmed=entry["mean_confidence_confirmed"],
                mean_confidence_rejected=entry["mean_confidence_rejected"],
            )
        )

    reviewers = {
        reviewer: int(count)
        for reviewer, count in session.execute(
            select(DuplicateCandidate.resolved_by, func.count())
            .where(DuplicateCandidate.resolved_by.is_not(None))
            .group_by(DuplicateCandidate.resolved_by)
            .order_by(func.count().desc())
        ).all()
        if reviewer is not None
    }

    total = _count(session, DuplicateCandidate)
    pending = int(
        session.scalar(
            select(func.count())
            .select_from(DuplicateCandidate)
            .where(DuplicateCandidate.status == DuplicateStatus.PENDING)
        )
        or 0
    )
    return ReviewStats(
        total=total,
        pending=pending,
        decided=total - pending,
        by_method=methods,
        reviewers=reviewers,
    )
