"""Batch verification: what to check, how much of it to skip, and how to record the answer.

The orchestration is where the cost lives. Three decisions shape it:

**Check the distinct thing, not the referencing row.** 2 520 addresses share 1 128 domains.
Deduplicating before dispatch turns 2 520 DNS queries into 1 128.

**Honour a TTL.** A verification is a statement about a moment. Anything still inside its TTL is
served from the cache, so a second run costs nothing; anything past it is re-checked. TTLs differ
by outcome on purpose: a confirmed MX record is stable for weeks, a dead domain deserves
re-checking sooner in case it was a blip, and an ``UNKNOWN`` — which is an absence of measurement,
not a measurement — expires almost immediately so an outage does not freeze into a cached
non-answer.

**Bound concurrency globally and per host.** DNS gets a plain semaphore. HTTP additionally gets
per-host limits, because the targets are small businesses' web hosting.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models import Lead, LeadIdentifier
from app.models.enums import IdentifierKind
from app.models.verification import DomainVerificationRecord, UrlVerificationRecord
from app.verification.email_domain import verify_domain
from app.verification.net import HostLimiter
from app.verification.resolver import DnsPythonResolver, MxResolver
from app.verification.types import DomainVerification, UrlVerification, VerificationStatus
from app.verification.website import WebsiteVerifier

logger = get_logger(__name__)

TTL_BY_STATUS: Final[dict[VerificationStatus, dt.timedelta]] = {
    VerificationStatus.VERIFIED: dt.timedelta(days=30),
    VerificationStatus.UNREACHABLE: dt.timedelta(days=7),
    # Deliberately short: an UNKNOWN means the network failed us, not that we learned anything.
    VerificationStatus.UNKNOWN: dt.timedelta(hours=6),
    VerificationStatus.SKIPPED: dt.timedelta(days=30),
}


def expiry_for(status: VerificationStatus, checked_at: dt.datetime) -> dt.datetime:
    return checked_at + TTL_BY_STATUS[status]


@dataclass(slots=True)
class VerificationReport:
    kind: str
    candidates: int = 0
    """Distinct things that could be checked."""

    served_from_cache: int = 0
    checked: int = 0
    by_status: Counter[str] = field(default_factory=Counter)
    extra: dict[str, int] = field(default_factory=dict)
    duration_seconds: float = 0.0
    leads_affected: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "candidates": self.candidates,
            "served_from_cache": self.served_from_cache,
            "checked": self.checked,
            "by_status": dict(self.by_status),
            "leads_affected": self.leads_affected,
            **self.extra,
            "duration_seconds": round(self.duration_seconds, 2),
        }


# ---------------------------------------------------------------------------------------
# Email domains
# ---------------------------------------------------------------------------------------


def email_domains_with_counts(session: Session) -> list[tuple[str, int]]:
    """Every distinct email domain in the database, with how many leads use it."""
    rows = session.execute(
        select(
            func.lower(func.split_part(LeadIdentifier.value_normalized, "@", 2)).label("domain"),
            func.count().label("address_count"),
        )
        .where(LeadIdentifier.kind == IdentifierKind.EMAIL)
        .group_by("domain")
        .order_by(func.count().desc())
    ).all()
    return [(str(row.domain), int(row.address_count)) for row in rows if row.domain]


def _fresh(record: DomainVerificationRecord | UrlVerificationRecord, now: dt.datetime) -> bool:
    return record.expires_at > now


async def _verify_domains(
    domains: Sequence[str], resolver: MxResolver, *, concurrency: int
) -> list[DomainVerification]:
    semaphore = asyncio.Semaphore(concurrency)

    async def one(domain: str) -> DomainVerification:
        async with semaphore:
            return await verify_domain(domain, resolver)

    return list(await asyncio.gather(*(one(domain) for domain in domains)))


def verify_email_domains(
    session: Session,
    *,
    resolver: MxResolver | None = None,
    # Measured, not guessed: at 32 the sandbox resolver returned 122 UNKNOWNs against 20 at 8,
    # for identical wall-clock time. Past a point, more parallel DNS buys failures, not speed.
    concurrency: int = 8,
    limit: int | None = None,
    force: bool = False,
) -> VerificationReport:
    """Verify every email domain in the database, skipping anything still fresh."""
    started = dt.datetime.now(dt.UTC)
    report = VerificationReport(kind="email_domains")

    counts = dict(email_domains_with_counts(session))
    report.candidates = len(counts)

    existing = {
        record.domain: record for record in session.scalars(select(DomainVerificationRecord)).all()
    }
    now = dt.datetime.now(dt.UTC)

    pending: list[str] = []
    for domain in counts:
        record = existing.get(domain)
        if record is not None and _fresh(record, now) and not force:
            report.served_from_cache += 1
            report.by_status[record.status.value] += 1
            continue
        pending.append(domain)

    if limit is not None:
        pending = pending[:limit]

    if pending:
        active_resolver = resolver or DnsPythonResolver()
        results = asyncio.run(_verify_domains(pending, active_resolver, concurrency=concurrency))
        for result in results:
            _store_domain(session, existing, result, counts.get(result.domain, 0))
            report.checked += 1
            report.by_status[result.status.value] += 1

    session.flush()
    report.leads_affected = _leads_with_verified_mailbox(session)
    report.extra = {
        "addresses_covered": sum(counts.values()),
        "queries_saved_by_domain_dedup": sum(counts.values()) - len(counts),
    }
    report.duration_seconds = (dt.datetime.now(dt.UTC) - started).total_seconds()
    logger.info("email_verification_complete", **report.as_dict())
    return report


def _store_domain(
    session: Session,
    existing: dict[str, DomainVerificationRecord],
    result: DomainVerification,
    address_count: int,
) -> None:
    record = existing.get(result.domain)
    expires = expiry_for(result.status, result.checked_at)
    if record is None:
        record = DomainVerificationRecord(domain=result.domain)
        session.add(record)
        existing[result.domain] = record
    record.status = result.status
    record.has_mx = result.has_mx
    record.provider = result.provider
    record.is_freemail = result.is_freemail
    record.is_disposable = result.is_disposable
    record.address_count = address_count
    record.latency_ms = result.latency_ms
    record.details = result.as_payload()
    record.error = result.error
    record.checked_at = result.checked_at
    record.expires_at = expires


def _leads_with_verified_mailbox(session: Session) -> int:
    return int(
        session.scalar(
            select(func.count(func.distinct(LeadIdentifier.lead_id)))
            .select_from(LeadIdentifier)
            .join(
                DomainVerificationRecord,
                DomainVerificationRecord.domain
                == func.lower(func.split_part(LeadIdentifier.value_normalized, "@", 2)),
            )
            .where(
                LeadIdentifier.kind == IdentifierKind.EMAIL,
                DomainVerificationRecord.status == VerificationStatus.VERIFIED,
                DomainVerificationRecord.has_mx.is_(True),
            )
        )
        or 0
    )


# ---------------------------------------------------------------------------------------
# Websites
# ---------------------------------------------------------------------------------------


def owned_website_urls(session: Session) -> list[str]:
    """Every owned-domain website URL in the database.

    Link-aggregator, social and messaging URLs are excluded at ingest time by
    ``is_owned_domain``; checking them would measure ``linktr.ee``'s uptime, not the lead's.
    """
    rows = session.scalars(
        select(LeadIdentifier.value_normalized)
        .where(
            LeadIdentifier.kind == IdentifierKind.WEBSITE,
            LeadIdentifier.attributes["is_owned_domain"].astext == "true",
        )
        .distinct()
    ).all()
    return [str(url) for url in rows]


async def _verify_urls(
    urls: Sequence[str],
    *,
    concurrency: int,
    per_host: int,
    timeout: float,
    allow_private: bool,
) -> list[UrlVerification]:
    semaphore = asyncio.Semaphore(concurrency)
    limiter = HostLimiter(per_host=per_host)

    async with WebsiteVerifier(
        timeout=timeout, limiter=limiter, allow_private=allow_private
    ) as verifier:

        async def one(url: str) -> UrlVerification:
            async with semaphore:
                return await verifier.verify(url)

        return list(await asyncio.gather(*(one(url) for url in urls)))


def verify_websites(
    session: Session,
    *,
    concurrency: int = 16,
    per_host: int = 2,
    timeout: float = 12.0,
    limit: int | None = None,
    force: bool = False,
    allow_private: bool = False,
) -> VerificationReport:
    """Verify every owned website, skipping anything still fresh."""
    from urllib.parse import urlsplit

    started = dt.datetime.now(dt.UTC)
    report = VerificationReport(kind="websites")

    urls = owned_website_urls(session)
    report.candidates = len(urls)

    existing = {
        record.url: record for record in session.scalars(select(UrlVerificationRecord)).all()
    }
    now = dt.datetime.now(dt.UTC)

    pending: list[str] = []
    for url in urls:
        record = existing.get(url)
        if record is not None and _fresh(record, now) and not force:
            report.served_from_cache += 1
            report.by_status[record.status.value] += 1
            continue
        pending.append(url)

    if limit is not None:
        pending = pending[:limit]

    if pending:
        results = asyncio.run(
            _verify_urls(
                pending,
                concurrency=concurrency,
                per_host=per_host,
                timeout=timeout,
                allow_private=allow_private,
            )
        )
        live = parked = 0
        for result in results:
            record = existing.get(result.url)
            if record is None:
                record = UrlVerificationRecord(
                    url=result.url, host=urlsplit(result.url).hostname or ""
                )
                session.add(record)
                existing[result.url] = record
            record.status = result.status
            record.status_code = result.status_code
            record.is_live = result.is_live
            record.is_parked = result.is_parked
            record.final_url = result.final_url
            record.redirect_count = result.redirect_count
            record.title = result.title
            record.latency_ms = result.latency_ms
            record.details = result.as_payload()
            record.error = result.error
            record.checked_at = result.checked_at
            record.expires_at = expiry_for(result.status, result.checked_at)

            report.checked += 1
            report.by_status[result.status.value] += 1
            live += int(result.is_live)
            parked += int(result.is_parked)
        report.extra = {"live": live, "parked": parked}

    session.flush()
    report.leads_affected = _leads_with_live_website(session)
    report.duration_seconds = (dt.datetime.now(dt.UTC) - started).total_seconds()
    logger.info("website_verification_complete", **report.as_dict())
    return report


def _leads_with_live_website(session: Session) -> int:
    return int(
        session.scalar(
            select(func.count(func.distinct(LeadIdentifier.lead_id)))
            .select_from(LeadIdentifier)
            .join(
                UrlVerificationRecord,
                UrlVerificationRecord.url == LeadIdentifier.value_normalized,
            )
            .where(
                LeadIdentifier.kind == IdentifierKind.WEBSITE,
                UrlVerificationRecord.is_live.is_(True),
            )
        )
        or 0
    )


# ---------------------------------------------------------------------------------------
# Lookups used by the scoring rubric
# ---------------------------------------------------------------------------------------


def domain_status_map(session: Session) -> dict[str, DomainVerificationRecord]:
    return {r.domain: r for r in session.scalars(select(DomainVerificationRecord)).all()}


def url_status_map(session: Session) -> dict[str, UrlVerificationRecord]:
    return {r.url: r for r in session.scalars(select(UrlVerificationRecord)).all()}


def lead_verification_signals(
    session: Session, lead_ids: Iterable[UUID] | None = None
) -> dict[UUID, dict[str, object]]:
    """Per-lead verification facts, shaped for the scoring rubric.

    Returned as plain data rather than ORM objects so the rubric stays a pure function of its
    inputs and remains testable without a database.
    """
    domains = domain_status_map(session)
    urls = url_status_map(session)

    query = select(LeadIdentifier).where(
        LeadIdentifier.kind.in_((IdentifierKind.EMAIL, IdentifierKind.WEBSITE))
    )
    if lead_ids is not None:
        query = query.where(LeadIdentifier.lead_id.in_(list(lead_ids)))

    signals: dict[UUID, dict[str, object]] = {}
    for identifier in session.scalars(query).all():
        entry = signals.setdefault(
            identifier.lead_id,
            {
                "mailbox_domain_status": VerificationStatus.UNKNOWN.value,
                "mailbox_accepts_mail": False,
                "mail_provider": None,
                "website_status": VerificationStatus.UNKNOWN.value,
                "website_is_live": False,
                "website_is_parked": False,
            },
        )
        if identifier.kind is IdentifierKind.EMAIL:
            domain = identifier.value_normalized.rsplit("@", 1)[-1]
            record = domains.get(domain)
            if record is not None:
                entry["mailbox_domain_status"] = record.status.value
                entry["mailbox_accepts_mail"] = record.accepts_mail
                entry["mail_provider"] = record.provider.value
        else:
            record_url = urls.get(identifier.value_normalized)
            if record_url is not None:
                entry["website_status"] = record_url.status.value
                entry["website_is_live"] = record_url.is_live
                entry["website_is_parked"] = record_url.is_parked
    return signals


def lead_count(session: Session) -> int:
    return int(session.scalar(select(func.count()).select_from(Lead)) or 0)
