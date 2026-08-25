"""The review queue.

Phase 1 auto-merged 169 pairs on exact identity keys and refused to merge anything else. What it
refused to merge is what this queue holds: shared websites, which are routinely franchises, and
high name similarity, which is a hypothesis. The API's job is to put a human in front of them
with enough context to decide in a few seconds, and to make the decision reversible.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.models.enums import DuplicateMethod, DuplicateStatus
from app.schemas.leads import LeadSummary


class FieldComparison(BaseModel):
    """One field, side by side, with the verdict pre-computed.

    Reviewers make this call fastest when the disagreements are already found for them. Rendering
    two records and leaving the diff to the eye is how franchises get merged at 2am.
    """

    field: str
    a: str | None = None
    b: str | None = None
    agrees: bool | None = Field(
        default=None, description="Null when the field is missing on at least one side"
    )


class DuplicateCandidateOut(BaseModel):
    id: uuid.UUID
    method: DuplicateMethod
    is_auto_mergeable: bool = Field(
        description="False for everything in this queue by construction — exact-key matches are "
        "merged during ingest and never reach a human."
    )
    confidence: float
    status: DuplicateStatus
    evidence: dict[str, Any] = Field(default_factory=dict)
    resolved_at: dt.datetime | None = None
    resolved_by: str | None = None
    resolution_note: str | None = None
    lead_a: LeadSummary
    lead_b: LeadSummary
    comparison: list[FieldComparison] = Field(default_factory=list)
    created_at: dt.datetime


class DuplicateDecisionRequest(BaseModel):
    """A reviewer's answer.

    ``pending`` is a legal decision, not an omission: it is the undo. Setting a decided pair back
    to pending reverses any merge the confirmation applied and returns the pair to the queue.
    """

    decision: Literal["confirmed_duplicate", "rejected_distinct", "pending"]
    reviewer: str = Field(
        min_length=1,
        max_length=128,
        description="Who decided. Recorded, because an unattributed judgement cannot be audited "
        "or reweighted later.",
    )
    survivor_id: uuid.UUID | None = Field(
        default=None,
        description="Which of the two leads to keep. Defaults to the better-evidenced one: "
        "higher quality score, then more identifiers, then the earlier row.",
    )
    note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _survivor_only_when_confirming(self) -> DuplicateDecisionRequest:
        if self.survivor_id is not None and self.decision != "confirmed_duplicate":
            raise ValueError("survivor_id is only meaningful when confirming a duplicate")
        return self


class DuplicateDecisionResponse(BaseModel):
    candidate: DuplicateCandidateOut
    merged_lead_id: uuid.UUID | None = Field(
        default=None, description="The lead now pointing at a survivor, if the merge was applied"
    )
    survivor_id: uuid.UUID | None = None
    unmerged: bool = Field(
        default=False, description="True when this decision reversed an earlier merge"
    )
