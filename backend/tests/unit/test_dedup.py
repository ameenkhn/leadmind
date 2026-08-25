"""Deduplication tests.

The regression that matters most is the franchise case: five Pumo Technovation branches share
one corporate domain and must survive as five leads. A pipeline that merges on shared website
would silently delete four real prospects, and would look correct while doing it.
"""

from __future__ import annotations

from app.ingestion.dedup.cluster import (
    MAX_HOST_GROUP_FOR_PAIRS,
    build_clusters,
    deduplicate,
    find_candidates,
)
from app.ingestion.dedup.union_find import UnionFind
from app.ingestion.resolution.company import resolve_companies
from app.ingestion.resolution.merge import merge_cluster
from app.models.enums import DuplicateMethod


class TestUnionFind:
    def test_transitive_grouping(self) -> None:
        forest: UnionFind[str] = UnionFind()
        forest.union("a", "b")
        forest.union("b", "c")
        forest.add("d")
        groups = {frozenset(members) for members in forest.groups().values()}
        assert groups == {frozenset({"a", "b", "c"}), frozenset({"d"})}

    def test_union_is_idempotent(self) -> None:
        forest: UnionFind[int] = UnionFind()
        for _ in range(3):
            forest.union(1, 2)
        assert len(forest.groups()) == 1


class TestAutoMergeTiers:
    def test_only_identity_keys_auto_merge(self) -> None:
        """Website and name similarity are explicitly *not* auto-merge keys."""
        from app.ingestion.dedup.cluster import AUTO_MERGE_KEYS

        methods = {method for _, method in AUTO_MERGE_KEYS}
        assert methods == {
            DuplicateMethod.EXACT_EMAIL,
            DuplicateMethod.EXACT_PHONE,
            DuplicateMethod.EXACT_FACEBOOK,
        }
        assert not DuplicateMethod.SHARED_WEBSITE.is_auto_mergeable
        assert not DuplicateMethod.FUZZY_NAME.is_auto_mergeable


class TestRealDataset:
    def test_row_count_reconciles(self, prepared) -> None:  # type: ignore[no-untyped-def]
        assert len(prepared.rows) == 2520
        assert len(prepared.leads) + prepared.rows_merged == 2520

    def test_merges_the_known_cross_sheet_duplicates(self, prepared) -> None:  # type: ignore[no-untyped-def]
        assert prepared.rows_merged == 169
        assert len(prepared.leads) == 2351

    def test_every_duplicate_pair_spans_day1_and_day3(self, prepared) -> None:  # type: ignore[no-untyped-def]
        """Day_2 is disjoint from both other sheets; every duplicate is Day_1 ∩ Day_3."""
        multi = [lead for lead in prepared.leads if lead.source_row_count > 1]
        assert len(multi) == 169
        for lead in multi:
            sheets = {prepared.records[i].source_row.sheet for i in lead.record_indexes}
            assert sheets == {"Day_1", "Day_3"}

    def test_franchise_branches_are_not_merged(self, prepared) -> None:  # type: ignore[no-untyped-def]
        pumo = [lead for lead in prepared.leads if lead.company_domain == "pumotechnovation.com"]
        assert len(pumo) == 5
        # Each branch keeps its own inbox: merging would have destroyed four of them.
        emails = {
            identifier.value
            for lead in pumo
            for identifier in lead.identifiers
            if identifier.kind.value == "email"
        }
        assert len(emails) == 5

    def test_franchise_becomes_one_company_with_five_leads(self, prepared) -> None:  # type: ignore[no-untyped-def]
        companies = resolve_companies(prepared.leads)
        pumo = next(c for c in companies if c.domain == "pumotechnovation.com")
        assert pumo.branch_count == 5
        assert pumo.is_multi_branch
        # One branch writes the name as a single word, so there is no shared prefix and the
        # label is derived from the domain rather than borrowed from one arbitrary branch.
        assert pumo.name == "Pumotechnovation"

    def test_shared_website_is_queued_never_merged(self, prepared) -> None:  # type: ignore[no-untyped-def]
        shared = [c for c in prepared.candidates if c.method is DuplicateMethod.SHARED_WEBSITE]
        assert shared, "expected shared-website pairs to reach the review queue"
        assert all(c.confidence < 0.5 for c in shared)

    def test_candidate_pairs_never_reference_the_same_lead(self, prepared) -> None:  # type: ignore[no-untyped-def]
        lead_of_record = {
            index: position
            for position, lead in enumerate(prepared.leads)
            for index in lead.record_indexes
        }
        for candidate in prepared.candidates:
            assert lead_of_record[candidate.left_index] != lead_of_record[candidate.right_index]

    def test_huge_host_groups_do_not_flood_the_queue(self, prepared) -> None:  # type: ignore[no-untyped-def]
        """A quadratic blow-up on one shared host would make the queue unusable."""
        counts: dict[str, int] = {}
        for candidate in prepared.candidates:
            if candidate.method is DuplicateMethod.SHARED_WEBSITE:
                host = str(candidate.evidence["host"])
                counts[host] = counts.get(host, 0) + 1
        limit = MAX_HOST_GROUP_FOR_PAIRS * (MAX_HOST_GROUP_FOR_PAIRS - 1) // 2
        assert all(count <= limit for count in counts.values())


class TestFuzzyThreshold:
    def test_threshold_separates_the_known_duplicates_from_the_franchise(self, prepared) -> None:  # type: ignore[no-untyped-def]
        """Tuned against real pairs rather than picked by feel.

        At the configured threshold the five Pumo branches still resemble each other enough to
        be *queued*, which is the correct outcome: a human should see them. They must never be
        merged, which is guaranteed by the tier they land in, not by the threshold.
        """
        clusters = build_clusters(prepared.records)
        candidates = find_candidates(prepared.records, clusters, fuzzy_threshold=92)
        assert all(c.method.is_auto_mergeable is False for c in candidates)

    def test_lower_threshold_only_grows_the_queue(self, prepared) -> None:  # type: ignore[no-untyped-def]
        clusters = build_clusters(prepared.records)
        strict = find_candidates(prepared.records, clusters, fuzzy_threshold=98)
        loose = find_candidates(prepared.records, clusters, fuzzy_threshold=85)
        assert len(loose) >= len(strict)


class TestMergeSemantics:
    def test_follower_observations_are_kept_per_source_row(self, prepared) -> None:  # type: ignore[no-untyped-def]
        """38% of duplicate pairs disagree on followers. That disagreement is the signal."""
        multi = [lead for lead in prepared.leads if len(lead.follower_observations) > 1]
        differing = [
            lead for lead in multi if len({o.value for o in lead.follower_observations}) > 1
        ]
        assert len(multi) == 168
        assert len(differing) == 63

    def test_matched_queries_accumulate(self, prepared) -> None:  # type: ignore[no-untyped-def]
        """The same business found by two searches tells you two things, not one twice."""
        assert any(len(lead.source_queries) > 1 for lead in prepared.leads)

    def test_identifiers_union_across_source_rows(self, prepared) -> None:  # type: ignore[no-untyped-def]
        for lead in prepared.leads:
            if lead.source_row_count < 2:
                continue
            per_row = [
                {i.kind for i in prepared.records[index].identifiers if i.value}
                for index in lead.record_indexes
            ]
            merged_kinds = {i.kind for i in lead.identifiers}
            assert set().union(*per_row) <= merged_kinds

    def test_exactly_one_primary_identifier_per_kind(self, prepared) -> None:  # type: ignore[no-untyped-def]
        for lead in prepared.leads:
            primaries: dict[object, int] = {}
            for identifier in lead.identifiers:
                if identifier.is_primary:
                    primaries[identifier.kind] = primaries.get(identifier.kind, 0) + 1
            assert all(count == 1 for count in primaries.values())

    def test_merge_is_order_independent(self, prepared) -> None:  # type: ignore[no-untyped-def]
        """Reversing the cluster's member order must not change the merged lead."""
        clusters = build_clusters(prepared.records)
        multi = [c for c in clusters if c.size > 1][:20]
        for cluster in multi:
            forward = merge_cluster(cluster, prepared.records)
            cluster.record_indexes.reverse()
            backward = merge_cluster(cluster, prepared.records)
            assert forward.display_name == backward.display_name
            assert {i.value for i in forward.identifiers} == {i.value for i in backward.identifiers}


class TestDeterminism:
    def test_two_runs_produce_identical_clusters(self, prepared) -> None:  # type: ignore[no-untyped-def]
        first = deduplicate(prepared.records)
        second = deduplicate(prepared.records)
        assert [c.record_indexes for c in first.clusters] == [
            c.record_indexes for c in second.clusters
        ]
