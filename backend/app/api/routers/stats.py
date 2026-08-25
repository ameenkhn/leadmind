"""Statistics endpoints — the system's own numbers, read live rather than remembered."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import DbSession
from app.schemas.stats import CorpusStats, QualityStats, ReviewStats, VerificationStats
from app.services import stats as stats_service

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("", response_model=CorpusStats, summary="Corpus overview and reconciliation")
def corpus(session: DbSession) -> CorpusStats:
    """Counts, plus the reconciliation identity from the last successful ingest.

    `rows_read == leads_total + rows_merged` is the only proof that ingestion did not silently
    lose a row. It is asserted by the CLI and by the golden test; exposing it here keeps it
    checkable against a running system rather than only at build time.
    """
    return stats_service.corpus_stats(session)


@router.get("/quality", response_model=QualityStats, summary="Data quality distribution")
def quality(
    session: DbSession,
    rubric_version: Annotated[str | None, Query()] = None,
) -> QualityStats:
    """Distribution of the data quality score — how much is known per record.

    Not a lead score. It answers "how much do we reliably know about this record", which is a
    prerequisite for, and independent of, "is this lead worth contacting".

    `factors_evaluated` is the important column. A lead scored on 9 of 11 factors and one scored
    on all 11 are not directly comparable, and a rubric factor that could not be evaluated is
    dropped from both numerator and denominator rather than counted as zero.
    """
    return stats_service.quality_stats(session, rubric_version=rubric_version)


@router.get(
    "/verification", response_model=VerificationStats, summary="Verification coverage and results"
)
def verification(session: DbSession) -> VerificationStats:
    """What has been measured about reachability, and how stale it is.

    `stale` counts records past their TTL, which the next verification run re-checks. A
    verification is a statement about a moment; without expiry the system would keep presenting
    a months-old DNS answer as current fact.
    """
    return stats_service.verification_stats(session)


@router.get(
    "/review", response_model=ReviewStats, summary="Review queue depth and detector precision"
)
def review(session: DbSession) -> ReviewStats:
    """Queue depth, and what the human decisions say about the detectors that filled it.

    `confirm_rate` per method is the observed precision of that detector on pairs a person has
    actually looked at. That is the number the fuzzy-name threshold should be tuned against —
    labelled data produced by the review process itself, rather than a threshold picked because
    92 sounded strict.
    """
    return stats_service.review_stats(session)
