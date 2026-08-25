"""Core entities: companies, leads, identifiers, source queries, metric observations."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    Float,
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
from app.models.enums import EntityKind, IdentifierKind, MetricKind


class Company(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An organisation, keyed on its owned domain.

    One company can own many leads. That is not an edge case here: five ``Pumo Technovation``
    branches share ``pumotechnovation.com`` and are five distinct prospects with distinct
    inboxes, phone numbers and cities. Collapsing them would silently destroy four leads.
    """

    __tablename__ = "companies"

    primary_domain: Mapped[str] = mapped_column(String(253), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    website_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Enrichment fills these in Phase 3. They are deliberately nullable and empty at ingest:
    # the source file contains no firmographics whatsoever.
    industry: Mapped[str | None] = mapped_column(String(128), nullable=True)
    company_size: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    leads: Mapped[list[Lead]] = relationship(back_populates="company")

    @property
    def branch_count(self) -> int:
        return len(self.leads)


class Lead(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One advertiser identity — in this dataset, one Facebook page."""

    __tablename__ = "leads"
    __table_args__ = (
        Index("ix_leads_normalized_name", "normalized_name"),
        Index("ix_leads_company_id", "company_id"),
    )

    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="SET NULL"), nullable=True
    )

    display_name: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(512), nullable=False)
    entity_kind: Mapped[EntityKind] = mapped_column(
        Enum(EntityKind, name="entity_kind", native_enum=True),
        nullable=False,
        default=EntityKind.UNKNOWN,
    )
    is_placeholder_name: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    location_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("locations.id", ondelete="SET NULL"), nullable=True
    )
    location_raw: Mapped[str | None] = mapped_column(String(128), nullable=True)
    location_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    fb_category_raw: Mapped[str | None] = mapped_column(String(256), nullable=True)
    niche_raw: Mapped[str | None] = mapped_column(String(128), nullable=True)

    first_seen_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)

    company: Mapped[Company | None] = relationship(back_populates="leads")
    identifiers: Mapped[list[LeadIdentifier]] = relationship(
        back_populates="lead", cascade="all, delete-orphan"
    )
    source_queries: Mapped[list[LeadSourceQuery]] = relationship(
        back_populates="lead", cascade="all, delete-orphan"
    )
    metrics: Mapped[list[MetricObservation]] = relationship(
        back_populates="lead", cascade="all, delete-orphan"
    )

    def identifier(self, kind: IdentifierKind) -> LeadIdentifier | None:
        for ident in self.identifiers:
            if ident.kind == kind and ident.is_primary:
                return ident
        return next((i for i in self.identifiers if i.kind == kind), None)


class LeadIdentifier(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A normalised contact or profile handle belonging to a lead."""

    __tablename__ = "lead_identifiers"
    __table_args__ = (
        UniqueConstraint("lead_id", "kind", "value_normalized", name="uq_lead_identifier"),
        Index("ix_lead_identifiers_kind_value", "kind", "value_normalized"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    )

    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[IdentifierKind] = mapped_column(
        Enum(IdentifierKind, name="identifier_kind", native_enum=True), nullable=False
    )
    value_raw: Mapped[str] = mapped_column(Text, nullable=False)
    value_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    attributes: Mapped[JSONBDict] = mapped_column(nullable=False, default=dict)

    lead: Mapped[Lead] = relationship(back_populates="identifiers")


class LeadSourceQuery(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A search query that surfaced this lead.

    A child table rather than a column, because it is genuinely many-to-one: among the 168 leads
    that appear in both Day_1 and Day_3, ``Matched_Query`` agrees only 36.9% of the time — the
    same business was found by two different queries on two different days.
    """

    __tablename__ = "lead_source_queries"
    __table_args__ = (UniqueConstraint("lead_id", "query", "source", name="uq_lead_source_query"),)

    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    query: Mapped[str] = mapped_column(String(256), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_is_inferred: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    observed_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)

    lead: Mapped[Lead] = relationship(back_populates="source_queries")


class MetricObservation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A measurement attributed to one observation batch.

    Follower counts disagree in 38.1% of the cross-sheet duplicate pairs because the two scrapes
    happened days apart. That disagreement is a growth signal, not dirt — overwriting a single
    ``leads.followers`` column on merge would throw away the only longitudinal data in the file.

    ``batch`` identifies *which* observation this is (here, the sheet name); it is part of the
    uniqueness key so re-ingesting the same workbook updates rather than duplicates. ``observed_at``
    is nullable on purpose: the workbook carries no scrape dates, and inventing one so a growth
    rate could be computed would fabricate the very number it was meant to measure. Supplying real
    dates in ``config/sources.yaml`` fills it in and switches the growth feature on.
    """

    __tablename__ = "metric_observations"
    __table_args__ = (
        UniqueConstraint("lead_id", "metric", "batch", name="uq_metric_observation"),
        Index("ix_metric_observations_lead_metric", "lead_id", "metric"),
    )

    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    metric: Mapped[MetricKind] = mapped_column(
        Enum(MetricKind, name="metric_kind", native_enum=True), nullable=False
    )
    value: Mapped[int] = mapped_column(Integer, nullable=False)
    value_raw: Mapped[str] = mapped_column(String(32), nullable=False)
    batch: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    observed_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)

    lead: Mapped[Lead] = relationship(back_populates="metrics")
