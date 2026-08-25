"""Data quality scores, duplicate review queue, and evaluation labels."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    CheckConstraint,
    Enum,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONBDict, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import DuplicateMethod, DuplicateStatus, LabelSource


class DataQualityScore(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A 0–100 completeness-and-reliability score with its reasons attached.

    This is emphatically *not* a lead score. It answers "how much do we actually know about this
    record", which is a prerequisite for — and independent of — "is this lead worth contacting".
    """

    __tablename__ = "data_quality_scores"
    __table_args__ = (
        UniqueConstraint("lead_id", "rubric_version", name="uq_data_quality_lead_rubric"),
        CheckConstraint("score >= 0 AND score <= 100", name="score_range"),
    )

    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    score: Mapped[float] = mapped_column(Float, nullable=False)
    rubric_version: Mapped[str] = mapped_column(String(32), nullable=False)
    factors: Mapped[JSONBDict] = mapped_column(nullable=False, default=dict)
    computed_at: Mapped[dt.datetime] = mapped_column(nullable=False)


class DuplicateCandidate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A *possible* duplicate awaiting human judgement.

    Exact-key matches never reach this table — they are merged during ingest. What lands here is
    everything uncertain: shared websites (often franchises) and high name similarity. Merging
    those automatically is how a pipeline quietly deletes real prospects.
    """

    __tablename__ = "duplicate_candidates"
    __table_args__ = (
        UniqueConstraint("lead_a_id", "lead_b_id", "method", name="uq_duplicate_candidate_pair"),
        CheckConstraint("lead_a_id <> lead_b_id", name="distinct_leads"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        Index("ix_duplicate_candidates_status", "status"),
    )

    lead_a_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    lead_b_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    method: Mapped[DuplicateMethod] = mapped_column(
        Enum(DuplicateMethod, name="duplicate_method", native_enum=True), nullable=False
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[DuplicateStatus] = mapped_column(
        Enum(DuplicateStatus, name="duplicate_status", native_enum=True),
        nullable=False,
        default=DuplicateStatus.PENDING,
    )
    evidence: Mapped[JSONBDict] = mapped_column(nullable=False, default=dict)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)


class EvalLabel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A label for the evaluation set, always carrying its provenance.

    ``label_source`` exists so weak labels (the shipped ``Relevance`` column) and hand-verified
    gold labels can never be silently averaged together in a metric.
    """

    __tablename__ = "eval_labels"
    __table_args__ = (
        UniqueConstraint("lead_id", "dimension", "label_source", name="uq_eval_label"),
    )

    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dimension: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    label_source: Mapped[LabelSource] = mapped_column(
        Enum(LabelSource, name="label_source", native_enum=True), nullable=False
    )
    labeller: Mapped[str | None] = mapped_column(String(128), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
