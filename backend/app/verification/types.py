"""Shared vocabulary for verification.

Phase 1 recorded every email as ``deliverability: unverified`` and every website as
``liveness: unverified``. Phase 1b replaces those placeholders with measurements — and keeps
``UNKNOWN`` as a first-class outcome, because a DNS timeout is not evidence of a dead domain and
must never be recorded as one.
"""

from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass, field
from typing import Any


class VerificationStatus(enum.StrEnum):
    """The outcome of a check.

    The distinction that matters is between ``UNREACHABLE`` and ``UNKNOWN``. The first is a
    measurement: the domain resolved and has no mail exchanger. The second is an absence of
    measurement: the resolver timed out, the network was down, the check was never run. Folding
    them together would let an outage silently downgrade thousands of good leads.
    """

    VERIFIED = "verified"
    UNREACHABLE = "unreachable"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"

    @property
    def is_measurement(self) -> bool:
        return self in (VerificationStatus.VERIFIED, VerificationStatus.UNREACHABLE)


class MailProvider(enum.StrEnum):
    """Who actually runs the mailbox, inferred from MX hostnames.

    Useful beyond curiosity: a domain on Google Workspace or Microsoft 365 is a business that
    pays for email, which is a mild but real digital-maturity signal for the ICP.
    """

    GOOGLE = "google"
    MICROSOFT = "microsoft"
    ZOHO = "zoho"
    YANDEX = "yandex"
    PROOFPOINT = "proofpoint"
    MIMECAST = "mimecast"
    GODADDY = "godaddy"
    HOSTINGER = "hostinger"
    REDIFF = "rediff"
    SELF_HOSTED = "self_hosted"
    OTHER = "other"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class DomainVerification:
    """The result of checking one email domain."""

    domain: str
    status: VerificationStatus
    has_mx: bool
    mx_hosts: tuple[str, ...] = ()
    provider: MailProvider = MailProvider.NONE
    is_freemail: bool = False
    is_disposable: bool = False
    latency_ms: int = 0
    error: str | None = None
    checked_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    @property
    def accepts_mail(self) -> bool:
        return self.status is VerificationStatus.VERIFIED and self.has_mx

    def as_payload(self) -> dict[str, Any]:
        return {
            "mx_hosts": list(self.mx_hosts),
            "provider": self.provider.value,
            "is_freemail": self.is_freemail,
            "is_disposable": self.is_disposable,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class UrlVerification:
    """The result of checking one website URL."""

    url: str
    status: VerificationStatus
    status_code: int | None = None
    final_url: str | None = None
    redirect_count: int = 0
    redirect_chain: tuple[str, ...] = ()
    content_type: str | None = None
    server: str | None = None
    title: str | None = None
    content_length: int | None = None
    is_parked: bool = False
    latency_ms: int = 0
    error: str | None = None
    checked_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    @property
    def is_live(self) -> bool:
        return (
            self.status is VerificationStatus.VERIFIED
            and self.status_code is not None
            and 200 <= self.status_code < 400
            and not self.is_parked
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "status_code": self.status_code,
            "final_url": self.final_url,
            "redirect_count": self.redirect_count,
            "redirect_chain": list(self.redirect_chain),
            "content_type": self.content_type,
            "server": self.server,
            "title": self.title,
            "content_length": self.content_length,
            "is_parked": self.is_parked,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }
