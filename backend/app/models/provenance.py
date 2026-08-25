"""Provenance and auditability: ingest runs, raw source records, validation issues."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONBDict, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import IngestStatus, ValidationSeverity


class IngestRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One execution of the ingest pipeline.

    ``code_version`` and ``rubric_version`` are stored so that a quality score can always be
    explained by the exact rules that produced it, even after those rules change.
    """

    __tablename__ = "ingest_runs"

    run_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    source_file: Mapped[str] = mapped_column(Text, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[IngestStatus] = mapped_column(
        Enum(IngestStatus, name="ingest_status", native_enum=True),
        nullable=False,
        default=IngestStatus.RUNNING,
    )
    started_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    finished_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    code_version: Mapped[str] = mapped_column(String(64), nullable=False)
    rubric_version: Mapped[str] = mapped_column(String(32), nullable=False)
    stats: Mapped[JSONBDict] = mapped_column(nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    source_records: Mapped[list[LeadSourceRecord]] = relationship(back_populates="ingest_run")


class LeadSourceRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The raw spreadsheet row, kept verbatim.

    Every normalised value elsewhere in the database can be traced back to the exact cell it
    came from. ``row_sha256`` over the raw payload makes re-ingestion idempotent: an unchanged
    row is recognised and skipped rather than duplicated.
    """

    __tablename__ = "lead_source_records"
    __table_args__ = (
        UniqueConstraint(
            "source_sha256", "source_sheet", "source_row_no", name="uq_lead_source_record_cell"
        ),
        Index("ix_lead_source_records_row_sha", "row_sha256"),
    )

    ingest_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ingest_runs.id", ondelete="CASCADE"), nullable=False
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="SET NULL"), nullable=True, index=True
    )

    source_file: Mapped[str] = mapped_column(Text, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_sheet: Mapped[str] = mapped_column(String(64), nullable=False)
    source_row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    source_serial: Mapped[str | None] = mapped_column(String(32), nullable=True)
    row_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    raw: Mapped[JSONBDict] = mapped_column(nullable=False)

    ingest_run: Mapped[IngestRun] = relationship(back_populates="source_records")


class ValidationIssue(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A recorded validation failure.

    Nothing is ever dropped silently. A row with an unusable field still becomes a lead; the
    field is marked invalid and the reason lands here, where it can be counted, filtered and
    surfaced in the UI as an explicit gap rather than an absence.
    """

    __tablename__ = "validation_issues"
    __table_args__ = (
        Index("ix_validation_issues_code", "code"),
        Index("ix_validation_issues_lead_id", "lead_id"),
    )

    source_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("lead_source_records.id", ondelete="CASCADE"),
        nullable=True,
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=True
    )
    field: Mapped[str] = mapped_column(String(64), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[ValidationSeverity] = mapped_column(
        Enum(ValidationSeverity, name="validation_severity", native_enum=True), nullable=False
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    value_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
