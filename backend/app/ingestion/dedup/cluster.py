"""Deduplication: what gets merged, what gets queued, and why the difference matters.

Two tiers, and the boundary between them is the whole design.

**Auto-merge — exact identity keys.** A normalised email address, an E.164 phone number, or a
canonical Facebook profile URL. Sharing one of these means being the same advertiser. On this
dataset that collapses 2 520 rows into 2 352 leads: 168 duplicate pairs, every one of them
spanning Day_1 and Day_3 (Day_2 is disjoint from both).

**Queue for review — resemblance, not identity.** A shared website host or a high name
similarity. These look like duplicates and frequently are not. The clearest case in this file:

    Pumo Technovation Kanchipuram      pumotechkanchipuram@gmail.com
    Pumo Technovation Malumichampatti  pumotechnovationmalumichampatt@gmail.com
    Pumo Technovation Tirupati         pumotechnovationtirupati@gmail.com
    Pumo Technovation Bommasandra      pumotechnovationbommasandra@gmail.com
    Pumo Technovation Poonamallee      pumotechpoonamallee@gmail.com

Five franchise branches on one corporate domain, each with its own inbox, phone number and city
— five real prospects. A pipeline that merges on shared website deletes four of them and never
says so. So a shared host creates a *company relationship*, and a review-queue row, and nothing
else.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from app.core.config import get_settings
from app.core.logging import get_logger
from app.ingestion.dedup.union_find import UnionFind
from app.ingestion.normalizers.record import NormalizedRecord
from app.models.enums import DuplicateMethod, IdentifierKind

logger = get_logger(__name__)

# Keys strong enough to assert identity. Deliberately excludes website: see module docstring.
AUTO_MERGE_KEYS: tuple[tuple[IdentifierKind, DuplicateMethod], ...] = (
    (IdentifierKind.EMAIL, DuplicateMethod.EXACT_EMAIL),
    (IdentifierKind.PHONE, DuplicateMethod.EXACT_PHONE),
    (IdentifierKind.FACEBOOK, DuplicateMethod.EXACT_FACEBOOK),
)

# Above this cluster size a shared host is a platform or a franchise, not a duplicate group.
# Generating pairs for such a group would flood the review queue quadratically.
MAX_HOST_GROUP_FOR_PAIRS = 12


@dataclass(frozen=True, slots=True)
class CandidatePair:
    """A possible duplicate for a human to judge. Never applied automatically."""

    left_index: int
    right_index: int
    method: DuplicateMethod
    confidence: float
    evidence: dict[str, object]


@dataclass(slots=True)
class Cluster:
    """A set of source records that are the same lead."""

    key: str
    record_indexes: list[int] = field(default_factory=list)
    matched_by: set[DuplicateMethod] = field(default_factory=set)

    @property
    def size(self) -> int:
        return len(self.record_indexes)


@dataclass(slots=True)
class DedupResult:
    clusters: list[Cluster]
    candidates: list[CandidatePair]
    merged_row_count: int

    @property
    def lead_count(self) -> int:
        return len(self.clusters)


def _identity_keys(record: NormalizedRecord) -> list[tuple[str, DuplicateMethod]]:
    keys: list[tuple[str, DuplicateMethod]] = []
    for kind, method in AUTO_MERGE_KEYS:
        value = record.identifier_value(kind)
        if value:
            keys.append((f"{kind.value}:{value}", method))
    return keys


def build_clusters(records: list[NormalizedRecord]) -> list[Cluster]:
    """Group records that share at least one exact identity key."""
    forest: UnionFind[str] = UnionFind()
    methods_by_key: dict[str, DuplicateMethod] = {}

    for index, record in enumerate(records):
        row_node = f"row:{index}"
        forest.add(row_node)
        for key, method in _identity_keys(record):
            methods_by_key[key] = method
            forest.union(row_node, key)

    clusters: list[Cluster] = []
    for root, members in forest.groups().items():
        row_indexes = sorted(
            int(node.split(":", 1)[1]) for node in members if node.startswith("row:")
        )
        if not row_indexes:
            continue
        matched: set[DuplicateMethod] = set()
        if len(row_indexes) > 1:
            matched = {
                methods_by_key[node]
                for node in members
                if not node.startswith("row:") and node in methods_by_key
            }
        clusters.append(Cluster(key=str(root), record_indexes=row_indexes, matched_by=matched))

    clusters.sort(key=lambda c: c.record_indexes[0])
    return clusters


def find_candidates(
    records: list[NormalizedRecord],
    clusters: list[Cluster],
    *,
    fuzzy_threshold: int | None = None,
) -> list[CandidatePair]:
    """Find pairs of *distinct* clusters that resemble each other, for human review."""
    threshold = (
        fuzzy_threshold if fuzzy_threshold is not None else get_settings().fuzzy_name_threshold
    )
    cluster_of: dict[int, int] = {}
    for cluster_index, cluster in enumerate(clusters):
        for record_index in cluster.record_indexes:
            cluster_of[record_index] = cluster_index

    representative: dict[int, int] = {
        cluster_index: cluster.record_indexes[0] for cluster_index, cluster in enumerate(clusters)
    }

    candidates: list[CandidatePair] = []
    seen: set[tuple[int, int, DuplicateMethod]] = set()

    def emit(
        left_cluster: int,
        right_cluster: int,
        method: DuplicateMethod,
        confidence: float,
        evidence: dict[str, object],
    ) -> None:
        if left_cluster == right_cluster:
            return
        low, high = sorted((left_cluster, right_cluster))
        signature = (low, high, method)
        if signature in seen:
            return
        seen.add(signature)
        candidates.append(
            CandidatePair(
                left_index=representative[low],
                right_index=representative[high],
                method=method,
                confidence=confidence,
                evidence=evidence,
            )
        )

    # --- shared owned website host -------------------------------------------------
    by_host: dict[str, set[int]] = defaultdict(set)
    for record_index, record in enumerate(records):
        host = record.website_host
        if host:
            by_host[host].add(cluster_of[record_index])

    for host, host_clusters in by_host.items():
        if len(host_clusters) < 2:
            continue
        ordered = sorted(host_clusters)
        if len(ordered) > MAX_HOST_GROUP_FOR_PAIRS:
            logger.info(
                "host_group_too_large_for_pairwise_review",
                host=host,
                distinct_leads=len(ordered),
            )
            continue
        for position, left in enumerate(ordered):
            for right in ordered[position + 1 :]:
                emit(
                    left,
                    right,
                    DuplicateMethod.SHARED_WEBSITE,
                    confidence=0.4,
                    evidence={"host": host, "leads_sharing_host": len(ordered)},
                )

    # --- fuzzy name, blocked on the first token to keep this near-linear -------------
    by_block: dict[str, list[int]] = defaultdict(list)
    for cluster_index, cluster in enumerate(clusters):
        name = records[cluster.record_indexes[0]].normalized_name
        token = name.split(" ", 1)[0] if name else ""
        if len(token) >= 3:
            by_block[token].append(cluster_index)

    for block, cluster_indexes in by_block.items():
        if len(cluster_indexes) < 2 or len(cluster_indexes) > MAX_HOST_GROUP_FOR_PAIRS:
            continue
        for position, left in enumerate(cluster_indexes):
            left_name = records[representative[left]].normalized_name
            for right in cluster_indexes[position + 1 :]:
                right_name = records[representative[right]].normalized_name
                score = fuzz.token_set_ratio(left_name, right_name)
                if score >= threshold:
                    emit(
                        left,
                        right,
                        DuplicateMethod.FUZZY_NAME,
                        confidence=round(score / 100, 3),
                        evidence={
                            "block": block,
                            "score": score,
                            "left_name": left_name,
                            "right_name": right_name,
                        },
                    )

    return candidates


def deduplicate(
    records: list[NormalizedRecord], *, fuzzy_threshold: int | None = None
) -> DedupResult:
    clusters = build_clusters(records)
    candidates = find_candidates(records, clusters, fuzzy_threshold=fuzzy_threshold)
    merged = len(records) - len(clusters)
    logger.info(
        "dedup_complete",
        rows=len(records),
        leads=len(clusters),
        merged_rows=merged,
        review_candidates=len(candidates),
    )
    return DedupResult(clusters=clusters, candidates=candidates, merged_row_count=merged)
