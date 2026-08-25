"""Collapse a cluster of source rows into one lead.

The merge rules exist because the 169 duplicate pairs do not agree with themselves. Measured
across those pairs: Facebook URL and city agree 99.4% of the time, name 98.8%, website 89.3%,
phone 86.3% — but follower count only 61.9% and matched query only 36.9%.

So the merge is not "take the newest row and discard the rest":

* **Scalar fields** take the highest-confidence value, tie-broken by earliest sheet.
* **Follower counts are never overwritten.** Each source row contributes its own dated
  observation, because the disagreement *is* the signal.
* **Matched queries accumulate.** The same business found by two different searches tells you
  two things about it, not one thing twice.
* **Identifiers accumulate.** A row missing a LinkedIn URL does not erase one another row has.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from app.ingestion.dedup.cluster import Cluster
from app.ingestion.normalizers.category import UNCLASSIFIED
from app.ingestion.normalizers.city import ResolvedLocation
from app.ingestion.normalizers.record import NormalizedRecord
from app.ingestion.normalizers.result import Issue
from app.models.enums import EntityKind, IdentifierKind


@dataclass(frozen=True, slots=True)
class MergedIdentifier:
    kind: IdentifierKind
    value: str
    raw: str
    confidence: float
    is_primary: bool
    attributes: dict[str, object]


@dataclass(frozen=True, slots=True)
class FollowerObservation:
    value: int
    raw: str
    source: str
    sheet_sequence: int
    observed_at: dt.datetime | None


@dataclass(frozen=True, slots=True)
class SourceQuery:
    query: str
    source: str
    source_is_inferred: bool


@dataclass(slots=True)
class MergedLead:
    """One lead, assembled from one or more source rows."""

    display_name: str
    normalized_name: str
    entity_kind: EntityKind
    is_placeholder_name: bool
    location: ResolvedLocation | None
    location_raw: str | None
    location_confidence: float | None
    vertical_slug: str | None
    fb_category_raw: str | None
    niche_raw: str | None
    identifiers: list[MergedIdentifier]
    follower_observations: list[FollowerObservation]
    source_queries: list[SourceQuery]
    relevance_labels: list[str]
    company_domain: str | None
    record_indexes: list[int]
    issues: list[Issue] = field(default_factory=list)

    def identifier(self, kind: IdentifierKind) -> MergedIdentifier | None:
        return next((i for i in self.identifiers if i.kind is kind and i.is_primary), None)

    def has(self, kind: IdentifierKind) -> bool:
        return any(i.kind is kind for i in self.identifiers)

    @property
    def source_row_count(self) -> int:
        return len(self.record_indexes)

    @property
    def latest_followers(self) -> int | None:
        if not self.follower_observations:
            return None
        return max(self.follower_observations, key=lambda o: o.sheet_sequence).value


def merge_cluster(cluster: Cluster, records: list[NormalizedRecord]) -> MergedLead:
    """Assemble one :class:`MergedLead` from the source rows in ``cluster``."""
    members = [records[index] for index in cluster.record_indexes]
    members.sort(key=lambda r: (r.source_row.sheet_sequence, r.source_row.row_no))

    name_source = max(
        members,
        key=lambda r: (
            not r.is_placeholder_name,
            r.name.confidence,
            len(r.display_name),
            -r.source_row.sheet_sequence,
        ),
    )

    located = [r for r in members if r.location.ok]
    location_source = max(located, key=lambda r: r.location.confidence) if located else None

    categorised = [r for r in members if r.category.value and r.category.value != UNCLASSIFIED]
    category_source = max(categorised, key=lambda r: r.category.confidence) if categorised else None

    identifiers = _merge_identifiers(members)
    follower_observations = [
        FollowerObservation(
            value=record.followers.value,
            raw=record.followers.raw or str(record.followers.value),
            source=record.source_row.sheet,
            sheet_sequence=record.source_row.sheet_sequence,
            observed_at=None,
        )
        for record in members
        if record.followers.value is not None
    ]

    queries: dict[tuple[str, str], SourceQuery] = {}
    for record in members:
        if not record.matched_query:
            continue
        source = record.source or "unknown"
        key = (record.matched_query, source)
        queries.setdefault(
            key,
            SourceQuery(
                query=record.matched_query,
                source=source,
                source_is_inferred=record.source_is_inferred,
            ),
        )

    relevance = sorted({r.relevance for r in members if r.relevance})
    company_domain = next((r.website_host for r in members if r.website_host), None)

    fallback_category = next((r for r in members if r.category.value), None)

    return MergedLead(
        display_name=name_source.display_name,
        normalized_name=name_source.normalized_name,
        entity_kind=name_source.entity_kind,
        is_placeholder_name=all(r.is_placeholder_name for r in members),
        location=location_source.location.value if location_source else None,
        location_raw=next((r.location.raw for r in members if r.location.raw), None),
        location_confidence=location_source.location.confidence if location_source else None,
        vertical_slug=(
            category_source.category.value
            if category_source
            else (fallback_category.category.value if fallback_category else None)
        ),
        fb_category_raw=next(
            (
                str(r.category.attributes.get("fb_category_raw"))
                for r in members
                if r.category.attributes.get("fb_category_raw")
            ),
            None,
        ),
        niche_raw=next(
            (
                str(r.category.attributes.get("niche_raw"))
                for r in members
                if r.category.attributes.get("niche_raw")
            ),
            None,
        ),
        identifiers=identifiers,
        follower_observations=follower_observations,
        source_queries=list(queries.values()),
        relevance_labels=relevance,
        company_domain=company_domain,
        record_indexes=list(cluster.record_indexes),
        issues=[issue for record in members for issue in record.issues],
    )


def _merge_identifiers(members: list[NormalizedRecord]) -> list[MergedIdentifier]:
    """Union every identifier across the cluster, keeping the first of each kind as primary."""
    seen: dict[tuple[IdentifierKind, str], MergedIdentifier] = {}
    primary_claimed: set[IdentifierKind] = set()

    for record in members:
        for identifier in record.identifiers:
            value = identifier.value
            if value is None:
                continue
            key = (identifier.kind, value)
            if key in seen:
                continue
            is_primary = identifier.kind not in primary_claimed
            primary_claimed.add(identifier.kind)
            seen[key] = MergedIdentifier(
                kind=identifier.kind,
                value=value,
                raw=identifier.result.raw or value,
                confidence=identifier.result.confidence,
                is_primary=is_primary,
                attributes=dict(identifier.result.attributes),
            )

    return sorted(seen.values(), key=lambda i: (i.kind.value, not i.is_primary, i.value))
