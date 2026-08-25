"""The review queue: what the pipeline refused to decide, and where a human decides it."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import DbSession, PageParams
from app.models.enums import DuplicateMethod, DuplicateStatus
from app.schemas.common import Page
from app.schemas.duplicates import (
    DuplicateCandidateOut,
    DuplicateDecisionRequest,
    DuplicateDecisionResponse,
)
from app.services import duplicates as duplicate_service

router = APIRouter(prefix="/duplicates", tags=["review queue"])


@router.get(
    "",
    response_model=Page[DuplicateCandidateOut],
    summary="List duplicate candidates awaiting review",
    response_description="Pairs, each with both leads embedded and the field diff pre-computed",
)
def list_duplicates(
    session: DbSession,
    pagination: PageParams,
    status: Annotated[
        DuplicateStatus | None, Query(description="Defaults to everything; usually `pending`")
    ] = None,
    method: Annotated[
        list[DuplicateMethod] | None,
        Query(
            description="`shared_website` is usually a franchise; `fuzzy_name` is a hypothesis. "
            "Exact-key methods never appear here — they are merged during ingest."
        ),
    ] = None,
    min_confidence: Annotated[float | None, Query(ge=0, le=1)] = None,
    max_confidence: Annotated[float | None, Query(ge=0, le=1)] = None,
    lead_id: Annotated[
        uuid.UUID | None, Query(description="Only pairs involving this lead")
    ] = None,
    sort: Annotated[
        str, Query(description="confidence, created or method; prefix `-` for descending")
    ] = duplicate_service.DEFAULT_SORT,
) -> Page[DuplicateCandidateOut]:
    items, total = duplicate_service.list_candidates(
        session,
        status=status,
        methods=tuple(method or ()),
        min_confidence=min_confidence,
        max_confidence=max_confidence,
        lead_id=lead_id,
        sort=sort,
        page=pagination.page,
        page_size=pagination.page_size,
    )
    return Page.build(items, total=total, page=pagination.page, page_size=pagination.page_size)


@router.get(
    "/{candidate_id}",
    response_model=DuplicateCandidateOut,
    summary="One pair, side by side",
)
def get_duplicate(session: DbSession, candidate_id: uuid.UUID) -> DuplicateCandidateOut:
    return duplicate_service.get_candidate(session, candidate_id)


@router.post(
    "/{candidate_id}/decision",
    response_model=DuplicateDecisionResponse,
    summary="Decide a pair",
    response_description="The updated candidate, and what the decision did to the leads",
    responses={
        409: {
            "description": "The decision conflicts with a merge already applied to one of "
            "these leads. Undo that one first."
        },
        422: {"description": "survivor_id is not one of the two leads in the pair."},
    },
)
def decide_duplicate(
    session: DbSession, candidate_id: uuid.UUID, request: DuplicateDecisionRequest
) -> DuplicateDecisionResponse:
    """Record a judgement.

    `confirmed_duplicate` points the loser at the survivor — a pointer, not a deletion. Nothing
    is moved or removed, so the decision is fully reversible by sending `pending`, which is what
    makes it safe to decide quickly.

    `rejected_distinct` records that the pair is two real businesses. That answer is as valuable
    as a confirmation: the confirm rate per detection method in `GET /stats/review` is the
    detector's measured precision, and it is what the fuzzy-name threshold should be tuned
    against.
    """
    return duplicate_service.decide(session, candidate_id, request)
