"""Record-level validation.

Field-level checks already ran inside the normalizers. What is left is everything that can only
be judged by looking at the whole record: is there any way to contact this lead at all, does the
email domain agree with the website, is this record so sparse that researching it is hopeless.

Two rules govern this module:

1. **Nothing is dropped.** Not one of these rules rejects a row. A record that fails every check
   still becomes a lead — it just carries the evidence of why it is weak. A pipeline that
   silently discards rows cannot be reconciled against its input, and reconciliation is the only
   proof that ingestion worked.
2. **Weakness is not the same as low quality of fit.** "We know almost nothing about this lead"
   feeds *data confidence*; whether the lead is worth contacting is a separate judgement made
   later with different inputs.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

from app.ingestion.normalizers.email import FREEMAIL_DOMAINS
from app.ingestion.normalizers.record import NormalizedRecord
from app.ingestion.normalizers.result import Issue
from app.models.enums import IdentifierKind, ValidationSeverity

Rule = Callable[[NormalizedRecord], Iterator[Issue]]

CONTACT_KINDS: tuple[IdentifierKind, ...] = (
    IdentifierKind.EMAIL,
    IdentifierKind.PHONE,
    IdentifierKind.WHATSAPP,
)

RESEARCHABLE_KINDS: tuple[IdentifierKind, ...] = (
    IdentifierKind.WEBSITE,
    IdentifierKind.LINKEDIN,
    IdentifierKind.INSTAGRAM,
    IdentifierKind.YOUTUBE,
)

THIN_FOLLOWER_CEILING = 100


def rule_has_contact_channel(record: NormalizedRecord) -> Iterator[Issue]:
    if not any(record.identifier_value(kind) for kind in CONTACT_KINDS):
        yield Issue(
            field="record",
            code="no_contact_channel",
            message="record has no usable email, phone or WhatsApp number",
            severity=ValidationSeverity.ERROR,
        )


def rule_has_identity(record: NormalizedRecord) -> Iterator[Issue]:
    if not record.name.ok:
        yield Issue(
            field="name",
            code="no_name",
            message="record has no name at all",
            severity=ValidationSeverity.ERROR,
        )


def rule_email_domain_matches_website(record: NormalizedRecord) -> Iterator[Issue]:
    """A corporate address on the company's own domain is the strongest reachability signal.

    Only meaningful when the mailbox is not a free provider — half this dataset is on gmail.com,
    where a mismatch says nothing at all.
    """
    domain = record.identifier_attr(IdentifierKind.EMAIL, "domain")
    host = record.website_host
    if not domain or not host or domain in FREEMAIL_DOMAINS:
        return
    if domain != host and not domain.endswith(f".{host}"):
        yield Issue(
            field="email",
            code="email_domain_website_mismatch",
            message=f"corporate email domain {domain!r} does not match website host {host!r}",
            severity=ValidationSeverity.INFO,
            value_raw=domain,
        )


def rule_researchable(record: NormalizedRecord) -> Iterator[Issue]:
    """Flag records the knowledge layer will have nothing to say about.

    Measured on this dataset: 260 leads have no owned website, no LinkedIn and fewer than 100
    followers. Any RAG answer about them would be built on nothing, so they are marked here and
    their data-confidence is capped rather than being allowed to look like a researched lead.
    """
    if record.has_owned_website:
        return
    if any(record.identifier_value(kind) for kind in RESEARCHABLE_KINDS[1:]):
        return
    followers = record.followers.value or 0
    if followers >= THIN_FOLLOWER_CEILING:
        return
    yield Issue(
        field="record",
        code="thin_record",
        message=(
            "no owned website, no LinkedIn/Instagram/YouTube profile and a negligible "
            "audience: nothing exists to research or cite"
        ),
        severity=ValidationSeverity.WARNING,
    )


def rule_zero_audience(record: NormalizedRecord) -> Iterator[Issue]:
    if record.followers.value == 0:
        yield Issue(
            field="followers",
            code="zero_followers",
            message="page reports zero followers, suggesting an abandoned or brand-new page",
            severity=ValidationSeverity.INFO,
            value_raw=record.followers.raw,
        )


def rule_unverified_identity(record: NormalizedRecord) -> Iterator[Issue]:
    """A numeric-ID page with a placeholder name has essentially no verifiable identity."""
    numeric = record.identifier_attr(IdentifierKind.FACEBOOK, "handle_is_numeric_id")
    if numeric and record.is_placeholder_name:
        yield Issue(
            field="record",
            code="unverifiable_identity",
            message="anonymised advertiser name on a page with no vanity handle",
            severity=ValidationSeverity.WARNING,
        )


DEFAULT_RULES: tuple[Rule, ...] = (
    rule_has_identity,
    rule_has_contact_channel,
    rule_email_domain_matches_website,
    rule_researchable,
    rule_zero_audience,
    rule_unverified_identity,
)


def validate_record(
    record: NormalizedRecord, *, rules: tuple[Rule, ...] = DEFAULT_RULES
) -> list[Issue]:
    """Run every rule and append the issues to the record. Never rejects."""
    found: list[Issue] = []
    for rule in rules:
        found.extend(rule(record))
    record.issues.extend(found)
    return found
