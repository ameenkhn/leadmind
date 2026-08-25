"""Controlled taxonomies: verticals and locations.

Both exist because the source data's own values are unusable as-is. ``FB_Category`` has 240
distinct values across the three sheets; ``City`` has 417 values of which 259 appear exactly
once and many are address fragments (``Nagar``, ``Road``, ``Vihar``) rather than places.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Category(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A vertical in LeadMind's own controlled taxonomy."""

    __tablename__ = "categories"

    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_slug: Mapped[str | None] = mapped_column(String(64), nullable=True)

    aliases: Mapped[list[CategoryAlias]] = relationship(
        back_populates="category", cascade="all, delete-orphan"
    )


class CategoryAlias(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A raw source value (usually a Facebook page category) mapped to a controlled vertical."""

    __tablename__ = "category_aliases"
    __table_args__ = (UniqueConstraint("alias_normalized", name="uq_category_aliases_alias"),)

    alias_raw: Mapped[str] = mapped_column(String(256), nullable=False)
    alias_normalized: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("categories.id", ondelete="CASCADE"), nullable=False
    )

    category: Mapped[Category] = relationship(back_populates="aliases")


class Location(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A gazetteer entry. Resolution against this table is what makes ``City`` trustworthy."""

    __tablename__ = "locations"

    slug: Mapped[str] = mapped_column(String(96), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str | None] = mapped_column(String(128), nullable=True)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False, default="IN")

    aliases: Mapped[list[LocationAlias]] = relationship(
        back_populates="location", cascade="all, delete-orphan"
    )


class LocationAlias(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Alternate spellings (``Bengaluru`` → ``Bangalore``, ``Gurgaon`` → ``Gurugram``)."""

    __tablename__ = "location_aliases"
    __table_args__ = (UniqueConstraint("alias_normalized", name="uq_location_aliases_alias"),)

    alias_raw: Mapped[str] = mapped_column(String(128), nullable=False)
    alias_normalized: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    location_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("locations.id", ondelete="CASCADE"), nullable=False
    )

    location: Mapped[Location] = relationship(back_populates="aliases")
