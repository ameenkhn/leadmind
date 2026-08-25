"""Email normalisation and classification.

All 2 520 source addresses are syntactically valid, so syntax checking alone would report a
clean bill of health on a column where 51.8% are free mailboxes, 30.8% are role accounts nobody
answers personally, and five are typo domains that will bounce silently. Those distinctions are
what this module produces; deliverability (MX/SMTP) is a network check and belongs to Phase 1b.
"""

from __future__ import annotations

import re
from typing import Any, Final

from rapidfuzz import fuzz

from app.ingestion.normalizers.result import Issue, NormalizationResult, clean_scalar
from app.models.enums import ValidationSeverity

_EMAIL_RE: Final = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$")

FREEMAIL_DOMAINS: Final[frozenset[str]] = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "yahoo.in",
        "yahoo.co.in",
        "yahoo.co.uk",
        "ymail.com",
        "rocketmail.com",
        "hotmail.com",
        "outlook.com",
        "live.com",
        "msn.com",
        "aol.com",
        "icloud.com",
        "me.com",
        "protonmail.com",
        "proton.me",
        "rediffmail.com",
        "zoho.com",
        "zohomail.in",
        "mail.com",
        "gmx.com",
        "yandex.com",
    }
)

ROLE_LOCAL_PARTS: Final[frozenset[str]] = frozenset(
    {
        "info",
        "contact",
        "admin",
        "support",
        "sales",
        "hello",
        "hi",
        "enquiry",
        "enquiries",
        "inquiry",
        "office",
        "care",
        "help",
        "team",
        "mail",
        "service",
        "services",
        "customercare",
        "marketing",
        "booking",
        "bookings",
        "reception",
        "query",
        "queries",
        "connect",
    }
)

DISPOSABLE_DOMAINS: Final[frozenset[str]] = frozenset(
    {
        "mailinator.com",
        "tempmail.com",
        "guerrillamail.com",
        "10minutemail.com",
        "yopmail.com",
        "trashmail.com",
        "sharklasers.com",
        "throwawaymail.com",
    }
)

# Typo domains observed in this dataset. A near-miss on a very common domain is a bounce
# waiting to happen; correcting it is safe, but the original is preserved and the correction
# is recorded rather than applied silently.
_TYPO_CORRECTIONS: Final[dict[str, str]] = {
    "gamil.com": "gmail.com",
    "gmial.com": "gmail.com",
    "gmai.com": "gmail.com",
    "gmail.om": "gmail.com",
    "gmail.co": "gmail.com",
    "gnail.com": "gmail.com",
    "gmail.cm": "gmail.com",
    "yahooo.com": "yahoo.com",
    "yaho.com": "yahoo.com",
    "hotmal.com": "hotmail.com",
    "outlok.com": "outlook.com",
    "rediffmail.co": "rediffmail.com",
}

_TYPO_SIMILARITY_FLOOR: Final = 88
_MAJOR_DOMAINS: Final = ("gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "rediffmail.com")


def _suspected_typo(domain: str) -> str | None:
    """Flag a domain that is suspiciously close to a very common one without matching it."""
    if domain in FREEMAIL_DOMAINS:
        return None
    for candidate in _MAJOR_DOMAINS:
        if fuzz.ratio(domain, candidate) >= _TYPO_SIMILARITY_FLOOR:
            return candidate
    return None


def normalize_email(raw: Any, *, field_name: str = "email") -> NormalizationResult[str]:
    """Lowercase, trim, correct known typo domains, and classify the mailbox."""
    original = clean_scalar(raw)
    if original is None:
        return NormalizationResult.empty()

    candidate = original.lower().replace(" ", "")
    candidate = candidate.strip(".,;:<>()[]\"'")
    issues: list[Issue] = []

    if not _EMAIL_RE.match(candidate):
        return NormalizationResult(
            value=None,
            raw=original,
            confidence=0.0,
            method="regex",
            issues=(
                Issue(
                    field=field_name,
                    code="email_invalid_syntax",
                    message="address does not match the expected local@domain.tld shape",
                    severity=ValidationSeverity.ERROR,
                    value_raw=original,
                ),
            ),
        )

    local, _, domain = candidate.partition("@")
    confidence = 1.0
    method = "regex"

    corrected = _TYPO_CORRECTIONS.get(domain)
    if corrected is not None:
        issues.append(
            Issue(
                field=field_name,
                code="email_typo_domain_corrected",
                message=f"domain {domain!r} corrected to {corrected!r}",
                severity=ValidationSeverity.WARNING,
                value_raw=original,
            )
        )
        domain = corrected
        candidate = f"{local}@{domain}"
        confidence = 0.75
        method = "regex+typo_correction"
    elif (near := _suspected_typo(domain)) is not None:
        issues.append(
            Issue(
                field=field_name,
                code="email_domain_lookalike",
                message=f"domain {domain!r} closely resembles {near!r}; left unchanged",
                severity=ValidationSeverity.WARNING,
                value_raw=original,
            )
        )
        confidence = 0.6

    is_freemail = domain in FREEMAIL_DOMAINS
    is_role = local.split("+")[0] in ROLE_LOCAL_PARTS
    is_disposable = domain in DISPOSABLE_DOMAINS

    if is_disposable:
        issues.append(
            Issue(
                field=field_name,
                code="email_disposable_domain",
                message=f"{domain!r} is a disposable mailbox provider",
                severity=ValidationSeverity.ERROR,
                value_raw=original,
            )
        )
        confidence = min(confidence, 0.2)

    attributes: dict[str, Any] = {
        "domain": domain,
        "local_part": local,
        "is_freemail": is_freemail,
        "is_role_based": is_role,
        "is_disposable": is_disposable,
        # Deliverability is unknown until an MX/SMTP probe runs. Recorded explicitly so no
        # downstream consumer can mistake "syntactically fine" for "reachable".
        "deliverability": "unverified",
    }

    return NormalizationResult(
        value=candidate,
        raw=original,
        confidence=confidence,
        method=method,
        issues=tuple(issues),
        attributes=attributes,
    )
