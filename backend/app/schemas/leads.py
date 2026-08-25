"""Lead representations.

The shape here is deliberate in one respect worth stating: every field that was *measured*
travels with how it was measured. A city is not a string, it is a string plus the confidence of
the gazetteer match. A mailbox is not "valid", it is a status that distinguishes verified from
unreachable from never-checked. Flattening those into bare values would hand the caller a
confident-looking record and hide exactly the uncertainty the pipeline spent Phase 1 measuring.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, Field

from app.models.enums import EntityKind, IdentifierKind, ValidationSeverity
from app.verification.types import VerificationStatus


class CategoryRef(BaseModel):
    slug: str
    label: str
    parent_slug: str | None = None


class LocationRef(BaseModel):
    slug: str | None = None
    name: str | None = None
    state: str | None = None
    raw: str | None = Field(default=None, description="The source value, kept verbatim")
    confidence: float | None = Field(
        default=None,
        description="Gazetteer match confidence. Null means the value was never resolved and is "
        "shown as it arrived rather than fuzzy-matched into a plausible wrong answer.",
    )
    resolved: bool = False


class CompanyRef(BaseModel):
    id: uuid.UUID
    primary_domain: str
    name: str | None = None
    branch_count: int = Field(description="Leads sharing this domain, this lead included")


class IdentifierOut(BaseModel):
    kind: IdentifierKind
    value: str = Field(description="The normalised value")
    value_raw: str
    is_primary: bool
    is_valid: bool
    confidence: float
    attributes: dict[str, Any] = Field(
        default_factory=dict,
        description="Normaliser output — e.g. whether an email is freemail or role-based",
    )


class MetricOut(BaseModel):
    metric: str
    value: int
    value_raw: str
    batch: str = Field(description="Which observation this is — here, the source sheet")
    observed_at: dt.datetime | None = Field(
        default=None,
        description="Null throughout this dataset: the workbook carries no scrape dates, and "
        "inventing one would fabricate the growth rate it was meant to measure.",
    )
    source: str


class FollowersOut(BaseModel):
    value: int
    value_raw: str
    batch: str
    observations: int = Field(description="How many times this lead's audience was measured")
    changed: bool = Field(
        description="Whether the observations disagree. 63 leads do, which is a growth signal "
        "rather than dirt."
    )


class VerificationOut(BaseModel):
    """What was measured about reachability, and what was not.

    Three distinct states, and collapsing any two of them loses real information:

    * ``null`` — there is nothing to check. The lead has no email address, or no website.
    * ``unknown`` — there is something to check and it has not been measured: the check has not
      run, or the resolver failed.
    * ``verified`` / ``unreachable`` — a measurement. ``unreachable`` means the domain answered
      and publishes no mail exchanger; mail to this lead cannot be delivered.

    A client that treats ``unknown`` as ``unreachable`` will mark good leads dead on the strength
    of one bad afternoon of DNS — and the result is cached, so it stays wrong.
    """

    mailbox_status: VerificationStatus | None = None
    mailbox_accepts_mail: bool = False
    mail_provider: str | None = None
    website_status: VerificationStatus | None = None
    website_is_live: bool = False
    website_is_parked: bool = False

    @classmethod
    def from_signals(
        cls,
        payload: dict[str, Any] | None,
        *,
        has_email: bool,
        has_website: bool,
    ) -> VerificationOut:
        """Build from ``lead_verification_signals`` output.

        The signals mapping uses the rubric's vocabulary and defaults every field, so the
        translation is done here once rather than being guessed at by each caller.
        """
        data = payload or {}

        def status(key: str, applicable: bool) -> VerificationStatus | None:
            if not applicable:
                return None
            raw = data.get(key)
            return VerificationStatus(raw) if raw else VerificationStatus.UNKNOWN

        return cls(
            mailbox_status=status("mailbox_domain_status", has_email),
            mailbox_accepts_mail=bool(data.get("mailbox_accepts_mail")) and has_email,
            mail_provider=data.get("mail_provider") if has_email else None,
            website_status=status("website_status", has_website),
            website_is_live=bool(data.get("website_is_live")) and has_website,
            website_is_parked=bool(data.get("website_is_parked")) and has_website,
        )


class QualityRef(BaseModel):
    score: float
    rubric_version: str
    factors_evaluated: int = Field(
        description="How many rubric factors could actually be evaluated. A partially-measured "
        "80 is a different claim from a fully-measured 80, so the count travels with the score."
    )


class MergeInfo(BaseModel):
    merged_into_id: uuid.UUID
    merged_at: dt.datetime | None = None
    merged_by: str | None = None


class LeadSummary(BaseModel):
    """One row of the leads list."""

    id: uuid.UUID
    display_name: str
    entity_kind: EntityKind
    is_placeholder_name: bool
    company: CompanyRef | None = None
    category: CategoryRef | None = None
    fb_category_raw: str | None = None
    location: LocationRef | None = None
    channels: list[IdentifierKind] = Field(
        default_factory=list, description="Which contact channels exist for this lead"
    )
    primary_email: str | None = None
    primary_phone: str | None = None
    website: str | None = None
    followers: FollowersOut | None = None
    quality: QualityRef | None = None
    verification: VerificationOut = Field(default_factory=VerificationOut)
    issue_count: int = 0
    merged: MergeInfo | None = Field(
        default=None,
        description="Set when a reviewer confirmed this lead as a duplicate of another. The row "
        "is intact and hidden from listings, not deleted.",
    )
    created_at: dt.datetime
    updated_at: dt.datetime


class ValidationIssueOut(BaseModel):
    field: str
    code: str
    severity: ValidationSeverity
    message: str
    value_raw: str | None = None


class SourceQueryOut(BaseModel):
    query: str
    source: str
    source_is_inferred: bool


class SourceRecordOut(BaseModel):
    """Provenance: the spreadsheet cell every normalised value came from."""

    source_file: str
    source_sheet: str
    source_row_no: int
    source_serial: str | None = None
    row_sha256: str
    raw: dict[str, Any]


class SiblingOut(BaseModel):
    id: uuid.UUID
    display_name: str
    location: str | None = None
    primary_email: str | None = None


class LeadDetail(LeadSummary):
    """One lead, with everything known about it and everything not known about it."""

    identifiers: list[IdentifierOut] = Field(default_factory=list)
    metrics: list[MetricOut] = Field(default_factory=list)
    source_queries: list[SourceQueryOut] = Field(default_factory=list)
    issues: list[ValidationIssueOut] = Field(
        default_factory=list,
        description="Recorded validation failures. Nothing was dropped because of them — they "
        "are the explicit gaps in this record.",
    )
    siblings: list[SiblingOut] = Field(
        default_factory=list, description="Other leads at the same company — branches, usually"
    )
    duplicate_candidate_count: int = 0
    niche_raw: str | None = None
    first_seen_at: dt.datetime | None = None
    last_seen_at: dt.datetime | None = None


class QualityFactorOut(BaseModel):
    name: str
    value: float | None = Field(
        description="The factor's normalised value in [0, 1], or null when it could not be "
        "evaluated at all — in which case it is dropped from the score rather than counted zero."
    )
    weight: float
    contribution: float
    reason: str
    measured: bool
    description: str | None = Field(
        default=None, description="The rubric's own explanation of what this factor asks"
    )


class QualityPenaltyOut(BaseModel):
    name: str
    amount: float
    triggered_by: str = Field(description="The validation code or measurement that fired it")
    reason: str = ""


class LeadQualityDetail(BaseModel):
    """The answer to "why is this lead an 87?", without recomputing anything.

    Every factor's value, weight, contribution and reason was persisted at scoring time along
    with the rubric version that produced it, so this endpoint reads rather than re-derives.
    Re-deriving would answer a subtly different question — what the *current* rubric would say —
    and quietly make old scores unexplainable the day the rubric changes.
    """

    lead_id: uuid.UUID
    score: float
    rubric_version: str
    computed_at: dt.datetime
    base_score: float = Field(description="Weighted factor score before penalties")
    penalty_total: float = Field(description="Penalties as accrued, before the cap")
    penalty_applied: float = Field(description="Penalties after the cap — what was subtracted")
    factors_evaluated: int
    factors_total: int
    weight_available: float = Field(
        description="Sum of the weights of the factors that could be evaluated. The score is "
        "earned/available, so unmeasurable factors neither help nor hurt."
    )
    factors: list[QualityFactorOut] = Field(default_factory=list)
    penalties: list[QualityPenaltyOut] = Field(default_factory=list)


class ProvenanceOut(BaseModel):
    lead_id: uuid.UUID
    source_records: list[SourceRecordOut] = Field(default_factory=list)
