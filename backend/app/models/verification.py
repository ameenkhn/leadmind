"""Verification caches.

Both tables are caches keyed on the thing checked rather than on the lead that referenced it,
because the mapping is many-to-one and heavily skewed: 2 520 addresses share 1 128 domains, of
which ``gmail.com`` alone accounts for 1 269 addresses. Keying per lead would multiply the work
by that skew and re-do it on every run.

``expires_at`` makes staleness explicit. A verification is a statement about a moment; without a
TTL the system would keep presenting a year-old DNS answer as current fact.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, CheckConstraint, Enum, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONBDict, TimestampMixin, UUIDPrimaryKeyMixin
from app.verification.types import MailProvider, VerificationStatus


class DomainVerificationRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Whether an email domain accepts mail, cached per domain."""

    __tablename__ = "domain_verifications"
    __table_args__ = (
        Index("ix_domain_verifications_status", "status"),
        Index("ix_domain_verifications_expires_at", "expires_at"),
        CheckConstraint("latency_ms >= 0", name="latency_non_negative"),
    )

    domain: Mapped[str] = mapped_column(String(253), unique=True, nullable=False)
    status: Mapped[VerificationStatus] = mapped_column(
        Enum(VerificationStatus, name="verification_status", native_enum=True), nullable=False
    )
    has_mx: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    provider: Mapped[MailProvider] = mapped_column(
        Enum(MailProvider, name="mail_provider", native_enum=True),
        nullable=False,
        default=MailProvider.NONE,
    )
    is_freemail: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_disposable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    address_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """How many leads' addresses sit on this domain — the cache's leverage, made visible."""

    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    details: Mapped[JSONBDict] = mapped_column(nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(nullable=False)

    @property
    def accepts_mail(self) -> bool:
        return self.status is VerificationStatus.VERIFIED and self.has_mx


class UrlVerificationRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Whether a website answers, cached per normalised URL."""

    __tablename__ = "url_verifications"
    __table_args__ = (
        Index("ix_url_verifications_status", "status"),
        Index("ix_url_verifications_expires_at", "expires_at"),
        Index("ix_url_verifications_host", "host"),
    )

    url: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    host: Mapped[str] = mapped_column(String(253), nullable=False)
    status: Mapped[VerificationStatus] = mapped_column(
        Enum(VerificationStatus, name="verification_status", native_enum=True), nullable=False
    )
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_live: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_parked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    final_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    redirect_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    details: Mapped[JSONBDict] = mapped_column(nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(nullable=False)
