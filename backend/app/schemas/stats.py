"""Corpus statistics.

These endpoints exist so the numbers quoted about this system are read from the database rather
than copied from a README that was true once. Everything here is a live query.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field


class ReconciliationOut(BaseModel):
    """The identity that proves ingestion did not lose anything.

    ``rows_read == leads_total + rows_merged``. It is asserted by the ingest CLI, by the golden
    test, and reported here so it stays checkable at runtime rather than only at build time.
    """

    rows_read: int
    rows_merged: int
    leads_total: int
    reconciles: bool
    source_file: str | None = None
    source_sha256: str | None = None
    ingested_at: dt.datetime | None = None
    code_version: str | None = None


class CorpusStats(BaseModel):
    leads: int
    leads_merged_by_review: int = Field(
        description="Leads a human confirmed as duplicates. Hidden from listings, still present."
    )
    companies: int
    companies_multi_branch: int
    identifiers: int
    identifiers_by_kind: dict[str, int] = Field(default_factory=dict)
    leads_by_entity_kind: dict[str, int] = Field(default_factory=dict)
    metric_observations: int
    source_queries: int
    eval_labels_by_source: dict[str, int] = Field(default_factory=dict)
    validation_issues: int
    validation_issues_by_code: dict[str, int] = Field(default_factory=dict)
    leads_with_owned_website: int
    leads_with_location: int
    leads_with_category: int
    reconciliation: ReconciliationOut | None = None


class QualityStats(BaseModel):
    rubric_version: str
    scored_leads: int
    mean: float | None = None
    median: float | None = None
    p10: float | None = None
    p90: float | None = None
    histogram: dict[str, int] = Field(default_factory=dict)
    factors_evaluated: dict[str, int] = Field(
        default_factory=dict,
        description="How many leads had N rubric factors actually measured. A wide spread here "
        "means scores are not comparable to each other without reading the count.",
    )


class VerificationCoverage(BaseModel):
    kind: str
    records: int
    fresh: int = Field(description="Inside their TTL")
    stale: int = Field(description="Past their TTL; the next run re-checks these")
    by_status: dict[str, int] = Field(default_factory=dict)


class VerificationStats(BaseModel):
    coverage: list[VerificationCoverage] = Field(default_factory=list)
    leads_with_verified_mailbox: int
    leads_proven_undeliverable: int = Field(
        description="Measured, not suspected: the domain resolved and publishes no MX record."
    )
    leads_with_live_website: int
    leads_on_managed_mail: int = Field(
        description="Leads on a non-free domain whose MX is Google Workspace, Microsoft 365, "
        "Zoho or similar — a business paying for managed email, which is a digital-maturity "
        "proxy in a dataset with no firmographics. Free providers are excluded: gmail.com's MX "
        "is Google's, and counting it would turn every personal address into a buying signal."
    )


class MethodReviewStats(BaseModel):
    method: str
    pending: int
    confirmed_duplicate: int
    rejected_distinct: int
    confirm_rate: float | None = Field(
        default=None,
        description="Of the decided pairs, the share confirmed. This is the detector's observed "
        "precision, and the number the fuzzy-name threshold should be tuned against — measured "
        "on reviewed pairs rather than chosen by feel.",
    )
    mean_confidence_confirmed: float | None = None
    mean_confidence_rejected: float | None = None


class ReviewStats(BaseModel):
    total: int
    pending: int
    decided: int
    by_method: list[MethodReviewStats] = Field(default_factory=list)
    reviewers: dict[str, int] = Field(default_factory=dict)
