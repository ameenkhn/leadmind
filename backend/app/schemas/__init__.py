"""Pydantic v2 request and response models.

These are the API's contract and are kept separate from the SQLAlchemy models on purpose. A
schema that is generated from the ORM leaks every column the database happens to have — including
surrogate keys, internal flags and anything added by tomorrow's migration — into a public
interface that then cannot change without breaking clients.
"""

from __future__ import annotations

from app.schemas.common import HealthResponse, Page, Problem, ReadinessResponse
from app.schemas.companies import (
    CategoryCount,
    CompanyDetail,
    CompanySummary,
    LocationCount,
)
from app.schemas.duplicates import (
    DuplicateCandidateOut,
    DuplicateDecisionRequest,
    DuplicateDecisionResponse,
    FieldComparison,
)
from app.schemas.leads import (
    CategoryRef,
    CompanyRef,
    FollowersOut,
    IdentifierOut,
    LeadDetail,
    LeadQualityDetail,
    LeadSummary,
    LocationRef,
    MetricOut,
    ProvenanceOut,
    QualityFactorOut,
    QualityPenaltyOut,
    QualityRef,
    SourceRecordOut,
    ValidationIssueOut,
    VerificationOut,
)
from app.schemas.stats import (
    CorpusStats,
    MethodReviewStats,
    QualityStats,
    ReconciliationOut,
    ReviewStats,
    VerificationCoverage,
    VerificationStats,
)

__all__ = [
    "CategoryCount",
    "CategoryRef",
    "CompanyDetail",
    "CompanyRef",
    "CompanySummary",
    "CorpusStats",
    "DuplicateCandidateOut",
    "DuplicateDecisionRequest",
    "DuplicateDecisionResponse",
    "FieldComparison",
    "FollowersOut",
    "HealthResponse",
    "IdentifierOut",
    "LeadDetail",
    "LeadQualityDetail",
    "LeadSummary",
    "LocationCount",
    "LocationRef",
    "MethodReviewStats",
    "MetricOut",
    "Page",
    "Problem",
    "ProvenanceOut",
    "QualityFactorOut",
    "QualityPenaltyOut",
    "QualityRef",
    "QualityStats",
    "ReadinessResponse",
    "ReconciliationOut",
    "ReviewStats",
    "SourceRecordOut",
    "ValidationIssueOut",
    "VerificationCoverage",
    "VerificationOut",
    "VerificationStats",
]
