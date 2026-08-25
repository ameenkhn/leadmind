"""Assemble one normalised record from one source row.

This is the seam between "raw spreadsheet cell" and "domain object". It runs every field
normalizer, collects every issue they raised, and produces a structure the deduplicator and the
persistence layer can both work from. It makes no database calls and takes no decisions about
merging — those belong to later stages and are easier to test when they are not entangled here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.ingestion.normalizers.category import normalize_category
from app.ingestion.normalizers.city import ResolvedLocation, normalize_city
from app.ingestion.normalizers.email import normalize_email
from app.ingestion.normalizers.followers import normalize_followers
from app.ingestion.normalizers.name import normalize_name
from app.ingestion.normalizers.phone import normalize_phone
from app.ingestion.normalizers.result import Issue, NormalizationResult, clean_scalar
from app.ingestion.normalizers.url import normalize_url
from app.ingestion.readers.excel import SourceRow
from app.models.enums import EntityKind, IdentifierKind

_URL_FIELDS: tuple[tuple[str, IdentifierKind], ...] = (
    ("website", IdentifierKind.WEBSITE),
    ("facebook", IdentifierKind.FACEBOOK),
    ("instagram", IdentifierKind.INSTAGRAM),
    ("youtube", IdentifierKind.YOUTUBE),
    ("linkedin", IdentifierKind.LINKEDIN),
)


@dataclass(frozen=True, slots=True)
class NormalizedIdentifier:
    kind: IdentifierKind
    result: NormalizationResult[str]

    @property
    def value(self) -> str | None:
        return self.result.value


@dataclass(slots=True)
class NormalizedRecord:
    """One source row after normalisation, before deduplication."""

    source_row: SourceRow
    name: NormalizationResult[str]
    identifiers: list[NormalizedIdentifier]
    location: NormalizationResult[ResolvedLocation]
    category: NormalizationResult[str]
    followers: NormalizationResult[int]
    relevance: str | None
    source: str | None
    source_is_inferred: bool
    matched_query: str | None
    issues: list[Issue] = field(default_factory=list)

    # ---- convenience accessors -------------------------------------------------

    def identifier_value(self, kind: IdentifierKind) -> str | None:
        for identifier in self.identifiers:
            if identifier.kind is kind:
                return identifier.value
        return None

    def identifier_attr(self, kind: IdentifierKind, key: str) -> Any:
        for identifier in self.identifiers:
            if identifier.kind is kind:
                return identifier.result.attributes.get(key)
        return None

    @property
    def display_name(self) -> str:
        raw = self.name.raw
        return raw if raw else f"(unnamed {self.source_row.sheet}#{self.source_row.row_no})"

    @property
    def normalized_name(self) -> str:
        return self.name.value or self.display_name.lower()

    @property
    def entity_kind(self) -> EntityKind:
        value = self.name.attributes.get("entity_kind")
        return EntityKind(value) if value else EntityKind.UNKNOWN

    @property
    def is_placeholder_name(self) -> bool:
        return bool(self.name.attributes.get("is_placeholder"))

    @property
    def website_host(self) -> str | None:
        """The registrable domain of an *owned* website, or ``None``.

        Link-aggregator and social hosts return ``None`` here on purpose: they are shared by
        thousands of unrelated businesses, so using one as a company key would merge strangers.
        """
        if not self.identifier_attr(IdentifierKind.WEBSITE, "is_owned_domain"):
            return None
        host = self.identifier_attr(IdentifierKind.WEBSITE, "registrable_domain")
        return str(host) if host else None

    @property
    def has_owned_website(self) -> bool:
        return self.website_host is not None


def normalize_record(row: SourceRow) -> NormalizedRecord:
    """Run every field normalizer over one source row."""
    name = normalize_name(row.get("name"))
    email = normalize_email(row.get("email"))
    phone = normalize_phone(row.get("phone"))
    whatsapp = normalize_phone(row.get("whatsapp"), field_name="whatsapp")

    identifiers: list[NormalizedIdentifier] = [
        NormalizedIdentifier(IdentifierKind.EMAIL, email),
        NormalizedIdentifier(IdentifierKind.PHONE, phone),
        NormalizedIdentifier(IdentifierKind.WHATSAPP, whatsapp),
    ]
    for field_name, kind in _URL_FIELDS:
        identifiers.append(
            NormalizedIdentifier(
                kind, normalize_url(row.get(field_name), field_name=field_name, kind=kind)
            )
        )

    location = normalize_city(row.get("city"))
    category = normalize_category(row.get("fb_category"), row.get("niche"))
    followers = normalize_followers(row.get("followers"))

    issues: list[Issue] = []
    for result in (name, location, category, followers):
        issues.extend(result.issues)
    for identifier in identifiers:
        issues.extend(identifier.result.issues)

    return NormalizedRecord(
        source_row=row,
        name=name,
        identifiers=identifiers,
        location=location,
        category=category,
        followers=followers,
        relevance=clean_scalar(row.get("relevance")),
        source=clean_scalar(row.get("source")),
        source_is_inferred="source" in row.inferred_fields,
        matched_query=clean_scalar(row.get("matched_query")),
        issues=issues,
    )
