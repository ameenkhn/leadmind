"""Email domain verification.

Phase 1 established that all 2 520 addresses are syntactically valid. That tells you nothing
about whether mail arrives. This module answers the next question that can be answered cheaply
and safely: **does this domain accept mail at all?**

## Why there is no SMTP callout here

The obvious next step — connect to the MX on port 25 and issue ``RCPT TO`` — is deliberately not
implemented, and that is an engineering decision rather than an omission:

* **It usually cannot run.** Outbound port 25 is blocked by essentially every cloud provider and
  most residential ISPs, including from both environments this project was built in. A feature
  that silently degrades to ``UNKNOWN`` everywhere is worse than no feature.
* **It damages the sender.** Repeated callouts from one IP get it greylisted and then
  blacklisted. The cost lands on the mail reputation of whoever runs the check.
* **It is frequently wrong anyway.** Catch-all domains accept every address, so a positive
  result means nothing; Gmail and Microsoft accept at RCPT and bounce later, so a positive
  result means nothing there either.

MX presence plus the Phase 1 classification (freemail, role account, disposable, typo domain)
captures most of the usable signal at none of the risk. If a project ever genuinely needs
mailbox-level verification, the right answer is a specialist provider with a warmed IP pool,
behind the same :class:`~app.verification.types.DomainVerification` interface — not a socket in
this repository.

## Why the check is per domain, not per address

2 520 addresses share 1 128 domains, and 1 269 of them are ``gmail.com`` alone. Checking per
address would be 2 520 DNS queries for 1 128 distinct answers, most of them the same answer 1 269
times. Per-domain checking with a persisted cache turns the whole run into roughly a thousand
queries, and the second run into none.
"""

from __future__ import annotations

import time
from typing import Final

from app.core.logging import get_logger
from app.ingestion.normalizers.email import DISPOSABLE_DOMAINS, FREEMAIL_DOMAINS
from app.verification.resolver import (
    MxLookupError,
    MxResolver,
    NoSuchDomainError,
)
from app.verification.types import DomainVerification, MailProvider, VerificationStatus

logger = get_logger(__name__)

# Matched against MX hostnames, longest-suffix-first so `outlook.com` does not shadow
# `mail.protection.outlook.com`.
_PROVIDER_SUFFIXES: Final[tuple[tuple[str, MailProvider], ...]] = (
    ("mail.protection.outlook.com", MailProvider.MICROSOFT),
    ("outlook.com", MailProvider.MICROSOFT),
    ("hotmail.com", MailProvider.MICROSOFT),
    ("google.com", MailProvider.GOOGLE),
    ("googlemail.com", MailProvider.GOOGLE),
    ("zoho.com", MailProvider.ZOHO),
    ("zoho.in", MailProvider.ZOHO),
    ("zohomail.com", MailProvider.ZOHO),
    ("yandex.net", MailProvider.YANDEX),
    ("pphosted.com", MailProvider.PROOFPOINT),
    ("mimecast.com", MailProvider.MIMECAST),
    ("secureserver.net", MailProvider.GODADDY),
    ("hostinger.com", MailProvider.HOSTINGER),
    ("hostinger.in", MailProvider.HOSTINGER),
    ("rediffmail.com", MailProvider.REDIFF),
    ("rediffmailpro.com", MailProvider.REDIFF),
)


def classify_provider(mx_hosts: tuple[str, ...], domain: str) -> MailProvider:
    """Infer who runs the mailbox from the MX hostnames."""
    if not mx_hosts:
        return MailProvider.NONE
    lowered = [host.lower().rstrip(".") for host in mx_hosts]
    for suffix, provider in _PROVIDER_SUFFIXES:
        if any(host == suffix or host.endswith(f".{suffix}") for host in lowered):
            return provider
    if all(host == domain or host.endswith(f".{domain}") for host in lowered):
        return MailProvider.SELF_HOSTED
    return MailProvider.OTHER


async def verify_domain(domain: str, resolver: MxResolver) -> DomainVerification:
    """Check whether one email domain accepts mail."""
    normalized = domain.strip().lower().rstrip(".")
    started = time.perf_counter()

    def elapsed() -> int:
        return int((time.perf_counter() - started) * 1000)

    is_freemail = normalized in FREEMAIL_DOMAINS
    is_disposable = normalized in DISPOSABLE_DOMAINS

    try:
        records = await resolver.mx(normalized)
    except NoSuchDomainError:
        # A measurement: the domain is not registered, so the address cannot receive mail.
        return DomainVerification(
            domain=normalized,
            status=VerificationStatus.UNREACHABLE,
            has_mx=False,
            provider=MailProvider.NONE,
            is_freemail=is_freemail,
            is_disposable=is_disposable,
            latency_ms=elapsed(),
            error="NXDOMAIN",
        )
    except MxLookupError as exc:
        # An absence of measurement. Never recorded as unreachable: a resolver timeout would
        # otherwise downgrade thousands of perfectly good leads during a network blip.
        logger.debug("mx_lookup_failed", domain=normalized, error=str(exc))
        return DomainVerification(
            domain=normalized,
            status=VerificationStatus.UNKNOWN,
            has_mx=False,
            provider=MailProvider.NONE,
            is_freemail=is_freemail,
            is_disposable=is_disposable,
            latency_ms=elapsed(),
            error=str(exc),
        )

    # Sorted here rather than in the resolver so the invariant holds for every implementation
    # of the protocol, not just the one that remembers to do it.
    ordered = sorted(records, key=lambda record: (record.preference, record.exchange))
    mx_hosts = tuple(record.exchange for record in ordered)
    has_mx = bool(mx_hosts)
    return DomainVerification(
        domain=normalized,
        status=VerificationStatus.VERIFIED if has_mx else VerificationStatus.UNREACHABLE,
        has_mx=has_mx,
        mx_hosts=mx_hosts,
        provider=classify_provider(mx_hosts, normalized),
        is_freemail=is_freemail,
        is_disposable=is_disposable,
        latency_ms=elapsed(),
        error=None if has_mx else "domain exists but publishes no MX record",
    )
