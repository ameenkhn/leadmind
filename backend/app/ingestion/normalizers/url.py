"""URL normalisation, and the distinction between a website and a link that merely looks like one.

82.1% of rows carry a ``Website``, but 69 of those are not websites: 26 ``linktr.ee`` pages,
19 ``threads.com`` profiles, 11 ``wa.me`` chat links, plus Telegram, YouTube, Amazon and Google
share links. Treating them as owned domains would inflate every "has a website" statistic and
would send the Phase 3 crawler at pages that tell us nothing about the business.

The useful predicate is therefore not "is a URL present" but ``is_owned_domain``: a host the
lead controls and that can be crawled for evidence. Measured on this dataset: 2 070 URLs present,
2 001 owned.
"""

from __future__ import annotations

import re
from typing import Any, Final
from urllib.parse import urlsplit, urlunsplit

import tldextract

from app.ingestion.normalizers.result import Issue, NormalizationResult, clean_scalar
from app.models.enums import IdentifierKind, ValidationSeverity

# Offline extractor: the bundled public-suffix snapshot is used and no network call is made at
# import or at runtime. A pipeline that silently reaches the internet to parse a string is a
# pipeline that breaks in CI.
_extract: Final = tldextract.TLDExtract(suffix_list_urls=(), fallback_to_snapshot=True)

SOCIAL_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "facebook.com",
        "fb.com",
        "fb.me",
        "instagram.com",
        "instagr.am",
        "linkedin.com",
        "lnkd.in",
        "youtube.com",
        "youtu.be",
        "threads.com",
        "threads.net",
        "twitter.com",
        "x.com",
        "pinterest.com",
        "snapchat.com",
        "tiktok.com",
    }
)

MESSAGING_HOSTS: Final[frozenset[str]] = frozenset(
    {"wa.me", "whatsapp.com", "api.whatsapp.com", "t.me", "telegram.me", "m.me"}
)

AGGREGATOR_HOSTS: Final[frozenset[str]] = frozenset(
    {"linktr.ee", "linktree.com", "beacons.ai", "bio.link", "taplink.cc", "carrd.co", "about.me"}
)

SHORTENER_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "bit.ly",
        "tinyurl.com",
        "goo.gl",
        "rb.gy",
        "cutt.ly",
        "shorturl.at",
        "share.google",
        "g.co",
        "rebrand.ly",
    }
)

MARKETPLACE_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "amazon.in",
        "amazon.com",
        "flipkart.com",
        "play.google.com",
        "apps.apple.com",
        "justdial.com",
        "indiamart.com",
        "practo.com",
        "urbanpro.com",
        "sulekha.com",
    }
)

FORM_HOSTS: Final[frozenset[str]] = frozenset(
    {"forms.gle", "docs.google.com", "calendly.com", "zcal.co", "typeform.com", "zoho.to"}
)

_NON_OWNED: Final[dict[str, frozenset[str]]] = {
    "social": SOCIAL_HOSTS,
    "messaging": MESSAGING_HOSTS,
    "aggregator": AGGREGATOR_HOSTS,
    "shortener": SHORTENER_HOSTS,
    "marketplace": MARKETPLACE_HOSTS,
    "form": FORM_HOSTS,
}

_TRACKING_PARAM_RE: Final = re.compile(r"^(utm_|fbclid|gclid|mc_[ce]id|igshid|ref_?src)", re.I)

_HANDLE_PATTERNS: Final[dict[IdentifierKind, tuple[re.Pattern[str], ...]]] = {
    IdentifierKind.FACEBOOK: (re.compile(r"^/(?:pg/|pages/[^/]+/)?(?P<handle>[^/?#]+)", re.I),),
    IdentifierKind.INSTAGRAM: (re.compile(r"^/(?P<handle>[^/?#]+)", re.I),),
    IdentifierKind.YOUTUBE: (
        re.compile(r"^/(?:channel|c|user)/(?P<handle>[^/?#]+)", re.I),
        re.compile(r"^/@(?P<handle>[^/?#]+)", re.I),
    ),
    IdentifierKind.LINKEDIN: (re.compile(r"^/(?:company|in|school)/(?P<handle>[^/?#]+)", re.I),),
}

_NUMERIC_HANDLE_RE: Final = re.compile(r"^\d{8,}$")


def registrable_domain(host: str) -> str:
    parts = _extract(host)
    # `top_domain_under_public_suffix` replaced `registered_domain` in tldextract 5.x; both mean
    # "the domain someone can register", e.g. co.uk yields example.co.uk rather than co.uk.
    return parts.top_domain_under_public_suffix or host


def _canonical_host(netloc: str) -> str:
    host = netloc.split("@")[-1].split(":")[0].strip().lower().rstrip(".")
    return host.removeprefix("www.")


def _strip_tracking(query: str) -> str:
    if not query:
        return ""
    kept = [
        pair
        for pair in query.split("&")
        if pair and not _TRACKING_PARAM_RE.match(pair.split("=", 1)[0])
    ]
    return "&".join(kept)


def classify_host(host: str) -> str | None:
    """Return the non-owned category of a host, or ``None`` if the host looks lead-owned."""
    registrable = registrable_domain(host)
    for category, hosts in _NON_OWNED.items():
        if host in hosts or registrable in hosts:
            return category
    return None


def normalize_url(
    raw: Any,
    *,
    field_name: str = "website",
    kind: IdentifierKind = IdentifierKind.WEBSITE,
) -> NormalizationResult[str]:
    """Canonicalise a URL and decide whether it points at a domain the lead owns."""
    original = clean_scalar(raw)
    if original is None:
        return NormalizationResult.empty()

    candidate = original
    if "://" not in candidate:
        candidate = f"https://{candidate}"

    try:
        split = urlsplit(candidate)
    except ValueError:
        return NormalizationResult(
            value=None,
            raw=original,
            confidence=0.0,
            method="urlsplit",
            issues=(
                Issue(
                    field=field_name,
                    code="url_unparseable",
                    message="value could not be parsed as a URL",
                    severity=ValidationSeverity.ERROR,
                    value_raw=original,
                ),
            ),
        )

    host = _canonical_host(split.netloc)
    if not host or "." not in host:
        return NormalizationResult(
            value=None,
            raw=original,
            confidence=0.0,
            method="urlsplit",
            issues=(
                Issue(
                    field=field_name,
                    code="url_missing_host",
                    message="URL has no resolvable host component",
                    severity=ValidationSeverity.ERROR,
                    value_raw=original,
                ),
            ),
        )

    path = re.sub(r"/{2,}", "/", split.path).rstrip("/")
    normalized = urlunsplit(("https", host, path, _strip_tracking(split.query), ""))

    issues: list[Issue] = []
    non_owned_category = classify_host(host)
    is_owned = non_owned_category is None

    if kind is IdentifierKind.WEBSITE and not is_owned:
        issues.append(
            Issue(
                field=field_name,
                code=f"website_not_owned_{non_owned_category}",
                message=(
                    f"{host!r} is a {non_owned_category} link, not a domain the lead owns; "
                    "excluded from the crawl corpus and from 'has website' signals"
                ),
                severity=ValidationSeverity.WARNING,
                value_raw=original,
            )
        )

    handle = _extract_handle(kind, path)
    attributes: dict[str, Any] = {
        "host": host,
        "registrable_domain": registrable_domain(host),
        "path": path,
        "is_owned_domain": is_owned,
        "non_owned_category": non_owned_category,
        "handle": handle,
        "liveness": "unverified",
    }

    if handle is not None and _NUMERIC_HANDLE_RE.match(handle):
        # 912 Facebook URLs are bare numeric page IDs with no vanity handle — typically new
        # pages. Weak identity, and a mild maturity signal the ICP model uses later.
        attributes["handle_is_numeric_id"] = True
        issues.append(
            Issue(
                field=field_name,
                code="profile_numeric_id_only",
                message="profile has no vanity handle, only a numeric page ID",
                severity=ValidationSeverity.INFO,
                value_raw=original,
            )
        )
    elif handle is not None:
        attributes["handle_is_numeric_id"] = False

    return NormalizationResult(
        value=normalized,
        raw=original,
        confidence=1.0 if is_owned or kind is not IdentifierKind.WEBSITE else 0.4,
        method="urlsplit+tldextract",
        issues=tuple(issues),
        attributes=attributes,
    )


def _extract_handle(kind: IdentifierKind, path: str) -> str | None:
    patterns = _HANDLE_PATTERNS.get(kind)
    if not patterns or not path:
        return None
    for pattern in patterns:
        match = pattern.match(path)
        if match:
            return match.group("handle").lower()
    return None
