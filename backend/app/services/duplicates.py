"""The duplicate review queue, and what happens when a human decides.

Phase 1 drew a hard line: exact identity keys (email, phone, Facebook URL) auto-merge; anything
weaker — a shared website, a similar name — is queued and never merged by machine. That line
exists because five ``Pumo Technovation`` branches share one corporate domain and are five real
prospects. This module is the other side of it: the place a person resolves what the pipeline
refused to guess.

Three rules shape the implementation.

**A merge is a pointer, never a deletion.** Confirming sets ``leads.merged_into_id`` on the loser
and touches nothing else. Identifiers, follower observations, provenance rows and validation
issues stay exactly where they were, so the reconciliation identity still holds and the decision
can be undone completely.

**Undo is a first-class decision.** Setting a decided pair back to ``pending`` reverses the merge
and returns it to the queue. A review tool without an undo trains its reviewers to hesitate,
which is worse than a wrong decision that can be corrected.

**The decisions are data.** Confirm rate per detection method is the observed precision of that
detector, which is what the fuzzy-name threshold should be tuned against — see
``GET /stats/review``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from typing import Final

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.core.errors import ConflictError, InvalidRequestError, NotFoundError
from app.core.logging import get_logger
from app.ingestion.quality.rubric import get_rubric
from app.models import DataQualityScore, DuplicateCandidate, Lead, LeadIdentifier
from app.models.enums import DuplicateMethod, DuplicateStatus
from app.schemas.duplicates import (
    DuplicateCandidateOut,
    DuplicateDecisionRequest,
    DuplicateDecisionResponse,
    FieldComparison,
)
from app.schemas.leads import LeadSummary
from app.services.leads import summarise_leads

logger = get_logger(__name__)

MAX_MERGE_DEPTH: Final[int] = 16

SORT_FIELDS: Final[frozenset[str]] = frozenset({"confidence", "created", "method"})
DEFAULT_SORT: Final[str] = "-confidence"


def build_candidate_query(
    *,
    status: DuplicateStatus | None = None,
    methods: Sequence[DuplicateMethod] = (),
    min_confidence: float | None = None,
    max_confidence: float | None = None,
    lead_id: uuid.UUID | None = None,
) -> Select[tuple[DuplicateCandidate]]:
    query: Select[tuple[DuplicateCandidate]] = select(DuplicateCandidate)
    if status is not None:
        query = query.where(DuplicateCandidate.status == status)
    if methods:
        query = query.where(DuplicateCandidate.method.in_(methods))
    if min_confidence is not None:
        query = query.where(DuplicateCandidate.confidence >= min_confidence)
    if max_confidence is not None:
        query = query.where(DuplicateCandidate.confidence <= max_confidence)
    if lead_id is not None:
        query = query.where(
            or_(
                DuplicateCandidate.lead_a_id == lead_id,
                DuplicateCandidate.lead_b_id == lead_id,
            )
        )
    return query


def _apply_sort(
    query: Select[tuple[DuplicateCandidate]], sort: str
) -> Select[tuple[DuplicateCandidate]]:
    descending = sort.startswith("-")
    field_name = sort.lstrip("-") or "confidence"
    if field_name not in SORT_FIELDS:
        field_name = "confidence"
    column = {
        "confidence": DuplicateCandidate.confidence,
        "created": DuplicateCandidate.created_at,
        "method": DuplicateCandidate.method,
    }[field_name]
    ordering = column.desc() if descending else column.asc()
    # Tiebreak on id, for the same reason the leads list does: without it, equal-confidence
    # pairs are free to reorder between pages and a reviewer never sees some of them.
    return query.order_by(ordering, DuplicateCandidate.id.asc())


def _compare(a: LeadSummary, b: LeadSummary) -> list[FieldComparison]:
    """Pre-compute the diff a reviewer would otherwise do by eye."""

    def entry(name: str, left: str | None, right: str | None) -> FieldComparison:
        agrees: bool | None = None
        if left is not None and right is not None:
            agrees = left.casefold() == right.casefold()
        return FieldComparison(field=name, a=left, b=right, agrees=agrees)

    return [
        entry("display_name", a.display_name, b.display_name),
        entry("email", a.primary_email, b.primary_email),
        entry("phone", a.primary_phone, b.primary_phone),
        entry("website", a.website, b.website),
        entry(
            "company_domain",
            a.company.primary_domain if a.company else None,
            b.company.primary_domain if b.company else None,
        ),
        entry(
            "location",
            a.location.name or a.location.raw if a.location else None,
            b.location.name or b.location.raw if b.location else None,
        ),
        entry(
            "category",
            a.category.label if a.category else a.fb_category_raw,
            b.category.label if b.category else b.fb_category_raw,
        ),
        entry(
            "followers",
            str(a.followers.value) if a.followers else None,
            str(b.followers.value) if b.followers else None,
        ),
        entry(
            "quality",
            f"{a.quality.score:.1f}" if a.quality else None,
            f"{b.quality.score:.1f}" if b.quality else None,
        ),
    ]


def _to_out(
    candidate: DuplicateCandidate, summaries: dict[uuid.UUID, LeadSummary]
) -> DuplicateCandidateOut:
    lead_a = summaries[candidate.lead_a_id]
    lead_b = summaries[candidate.lead_b_id]
    return DuplicateCandidateOut(
        id=candidate.id,
        method=candidate.method,
        is_auto_mergeable=candidate.method.is_auto_mergeable,
        confidence=candidate.confidence,
        status=candidate.status,
        evidence=candidate.evidence,
        resolved_at=candidate.resolved_at,
        resolved_by=candidate.resolved_by,
        resolution_note=candidate.resolution_note,
        lead_a=lead_a,
        lead_b=lead_b,
        comparison=_compare(lead_a, lead_b),
        created_at=candidate.created_at,
    )


def _summaries_for(
    session: Session, candidates: Sequence[DuplicateCandidate]
) -> dict[uuid.UUID, LeadSummary]:
    lead_ids = {c.lead_a_id for c in candidates} | {c.lead_b_id for c in candidates}
    if not lead_ids:
        return {}
    leads = list(
        session.scalars(
            select(Lead)
            .where(Lead.id.in_(lead_ids))
            .options(joinedload(Lead.company), joinedload(Lead.category), joinedload(Lead.location))
        )
        .unique()
        .all()
    )
    return summarise_leads(session, leads)


def list_candidates(
    session: Session,
    *,
    status: DuplicateStatus | None = None,
    methods: Sequence[DuplicateMethod] = (),
    min_confidence: float | None = None,
    max_confidence: float | None = None,
    lead_id: uuid.UUID | None = None,
    sort: str = DEFAULT_SORT,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[DuplicateCandidateOut], int]:
    base = build_candidate_query(
        status=status,
        methods=methods,
        min_confidence=min_confidence,
        max_confidence=max_confidence,
        lead_id=lead_id,
    )
    total = int(
        session.scalar(base.with_only_columns(func.count(DuplicateCandidate.id)).order_by(None))
        or 0
    )
    query = _apply_sort(base, sort).offset((page - 1) * page_size).limit(page_size)
    candidates = list(session.scalars(query).all())
    summaries = _summaries_for(session, candidates)
    return [_to_out(candidate, summaries) for candidate in candidates], total


def get_candidate(session: Session, candidate_id: uuid.UUID) -> DuplicateCandidateOut:
    candidate = session.get(DuplicateCandidate, candidate_id)
    if candidate is None:
        raise NotFoundError("duplicate candidate not found", candidate_id=str(candidate_id))
    summaries = _summaries_for(session, [candidate])
    return _to_out(candidate, summaries)


# ---------------------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------------------


def _merge_root(session: Session, lead: Lead) -> Lead:
    """Follow a merge chain to the lead that actually survived.

    Merging into a lead that is itself merged would build a chain, and every consumer would then
    have to walk it. Following to the root keeps the invariant at depth one. The depth cap is a
    cycle guard: the database forbids self-merges but not an A→B→A pair written by two racing
    requests, and an unbounded loop here would hang the request rather than fail it.
    """
    seen: set[uuid.UUID] = {lead.id}
    current = lead
    for _ in range(MAX_MERGE_DEPTH):
        if current.merged_into_id is None:
            return current
        parent = session.get(Lead, current.merged_into_id)
        if parent is None or parent.id in seen:
            return current
        seen.add(parent.id)
        current = parent
    raise ConflictError("merge chain is too deep to resolve", lead_id=str(lead.id))


def _identifier_count(session: Session, lead_id: uuid.UUID) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(LeadIdentifier)
            .where(LeadIdentifier.lead_id == lead_id)
        )
        or 0
    )


def _quality(session: Session, lead_id: uuid.UUID, rubric_version: str) -> float:
    return float(
        session.scalar(
            select(DataQualityScore.score).where(
                DataQualityScore.lead_id == lead_id,
                DataQualityScore.rubric_version == rubric_version,
            )
        )
        or 0.0
    )


def choose_survivor(session: Session, lead_a: Lead, lead_b: Lead) -> tuple[Lead, Lead]:
    """Pick which lead to keep when the reviewer does not say.

    Better-evidenced wins: higher data quality score, then more identifiers, then the row that
    arrived first. Deterministic on purpose — a default that depends on dictionary ordering makes
    the same decision produce different databases on two machines.
    """
    version = get_rubric().version
    key_a = (
        _quality(session, lead_a.id, version),
        _identifier_count(session, lead_a.id),
        -lead_a.created_at.timestamp(),
    )
    key_b = (
        _quality(session, lead_b.id, version),
        _identifier_count(session, lead_b.id),
        -lead_b.created_at.timestamp(),
    )
    if key_b > key_a:
        return lead_b, lead_a
    if key_a > key_b:
        return lead_a, lead_b
    # Fully tied: fall back to id order so the outcome is still reproducible.
    return (lead_a, lead_b) if str(lead_a.id) <= str(lead_b.id) else (lead_b, lead_a)


def _other_confirmations(
    session: Session, candidate: DuplicateCandidate
) -> list[DuplicateCandidate]:
    """Other confirmed candidates linking the same pair by a different detection method.

    A pair can be flagged twice — a shared website *and* a similar name. Undoing one of those
    must not undo a merge the other still confirms.
    """
    return list(
        session.scalars(
            select(DuplicateCandidate).where(
                DuplicateCandidate.id != candidate.id,
                DuplicateCandidate.status == DuplicateStatus.CONFIRMED_DUPLICATE,
                or_(
                    (DuplicateCandidate.lead_a_id == candidate.lead_a_id)
                    & (DuplicateCandidate.lead_b_id == candidate.lead_b_id),
                    (DuplicateCandidate.lead_a_id == candidate.lead_b_id)
                    & (DuplicateCandidate.lead_b_id == candidate.lead_a_id),
                ),
            )
        ).all()
    )


def _reverse_merge(session: Session, candidate: DuplicateCandidate, leads: Sequence[Lead]) -> bool:
    """Undo the merge this candidate's confirmation applied, if it still stands alone."""
    if _other_confirmations(session, candidate):
        logger.info(
            "merge_retained_by_other_candidate",
            candidate_id=str(candidate.id),
        )
        return False

    pair = {lead.id: lead for lead in leads}
    reversed_any = False
    for lead in leads:
        if lead.merged_into_id is not None and lead.merged_into_id in pair:
            lead.merged_into_id = None
            lead.merged_at = None
            lead.merged_by = None
            reversed_any = True
    return reversed_any


def decide(
    session: Session, candidate_id: uuid.UUID, request: DuplicateDecisionRequest
) -> DuplicateDecisionResponse:
    """Record a reviewer's judgement and apply or reverse the merge it implies.

    The candidate row is locked for the duration. Two reviewers opening the same pair is normal;
    two reviewers deciding it simultaneously and each reading the other's half-applied state is
    not, and read-modify-write on ``merged_into_id`` without a lock is exactly that race.
    """
    candidate = session.get(DuplicateCandidate, candidate_id, with_for_update=True)
    if candidate is None:
        raise NotFoundError("duplicate candidate not found", candidate_id=str(candidate_id))

    lead_a = session.get(Lead, candidate.lead_a_id, with_for_update=True)
    lead_b = session.get(Lead, candidate.lead_b_id, with_for_update=True)
    if lead_a is None or lead_b is None:  # pragma: no cover - foreign keys prevent this
        raise NotFoundError("candidate references a missing lead", candidate_id=str(candidate_id))

    now = dt.datetime.now(dt.UTC)
    unmerged = False

    if candidate.status is DuplicateStatus.CONFIRMED_DUPLICATE:
        unmerged = _reverse_merge(session, candidate, (lead_a, lead_b))

    merged_lead_id: uuid.UUID | None = None
    survivor_id: uuid.UUID | None = None

    if request.decision == "confirmed_duplicate":
        if request.survivor_id is not None and request.survivor_id not in (lead_a.id, lead_b.id):
            raise InvalidRequestError(
                "survivor_id must be one of the two leads in this pair",
                survivor_id=str(request.survivor_id),
            )

        if request.survivor_id == lead_a.id:
            survivor, loser = lead_a, lead_b
        elif request.survivor_id == lead_b.id:
            survivor, loser = lead_b, lead_a
        else:
            survivor, loser = choose_survivor(session, lead_a, lead_b)

        survivor = _merge_root(session, survivor)
        if loser.id == survivor.id:
            raise ConflictError(
                "these leads are already merged into one another",
                candidate_id=str(candidate_id),
            )
        if loser.merged_into_id is not None and loser.merged_into_id != survivor.id:
            raise ConflictError(
                "lead is already merged into a different lead; undo that decision first",
                lead_id=str(loser.id),
                merged_into_id=str(loser.merged_into_id),
            )

        loser.merged_into_id = survivor.id
        loser.merged_at = now
        loser.merged_by = request.reviewer
        merged_lead_id = loser.id
        survivor_id = survivor.id

        candidate.status = DuplicateStatus.CONFIRMED_DUPLICATE
        candidate.resolved_at = now
        candidate.resolved_by = request.reviewer
    elif request.decision == "rejected_distinct":
        candidate.status = DuplicateStatus.REJECTED_DISTINCT
        candidate.resolved_at = now
        candidate.resolved_by = request.reviewer
    else:
        candidate.status = DuplicateStatus.PENDING
        candidate.resolved_at = None
        candidate.resolved_by = None

    candidate.resolution_note = request.note

    session.flush()
    logger.info(
        "duplicate_decided",
        candidate_id=str(candidate.id),
        method=candidate.method.value,
        decision=request.decision,
        reviewer=request.reviewer,
        merged_lead_id=str(merged_lead_id) if merged_lead_id else None,
        survivor_id=str(survivor_id) if survivor_id else None,
        unmerged=unmerged,
    )

    session.refresh(candidate)
    summaries = _summaries_for(session, [candidate])
    return DuplicateDecisionResponse(
        candidate=_to_out(candidate, summaries),
        merged_lead_id=merged_lead_id,
        survivor_id=survivor_id,
        unmerged=unmerged,
    )
