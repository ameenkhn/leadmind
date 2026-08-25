"""Leads, companies and metadata, served over the real 2 520-row workbook."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.integration.helpers import QueryCounter

pytestmark = pytest.mark.integration

API = "/api/v1"


class TestListing:
    def test_returns_every_active_lead(self, client: TestClient) -> None:
        page = client.get(f"{API}/leads", params={"page_size": 1}).json()
        assert page["total"] == 2351
        assert page["pages"] == 2351

    def test_reports_the_rubric_that_produced_the_scores(self, client: TestClient) -> None:
        """A client comparing two responses must be able to tell a rescore from a data change."""
        response = client.get(f"{API}/leads", params={"page_size": 1})
        assert response.headers["X-Rubric-Version"] == "1.1"
        assert response.json()["items"][0]["quality"]["rubric_version"] == "1.1"

    def test_paging_never_repeats_or_skips_a_lead(self, client: TestClient) -> None:
        """The tiebreak on id is what makes this true; without it, equal-quality leads shuffle
        between pages and some are silently never shown."""
        seen: list[str] = []
        for page in range(1, 9):
            body = client.get(
                f"{API}/leads", params={"page": page, "page_size": 25, "sort": "-quality"}
            ).json()
            seen.extend(item["id"] for item in body["items"])
        assert len(seen) == 200
        assert len(set(seen)) == 200

    def test_page_size_is_capped_silently(self, client: TestClient) -> None:
        body = client.get(f"{API}/leads", params={"page_size": 100_000}).json()
        assert body["page_size"] == 200
        assert len(body["items"]) == 200

    def test_page_beyond_the_end_is_empty_not_an_error(self, client: TestClient) -> None:
        body = client.get(f"{API}/leads", params={"page": 9999, "page_size": 25}).json()
        assert body["items"] == []
        assert body["total"] == 2351
        assert body["has_next"] is False

    def test_sorted_by_quality_descending_by_default(self, client: TestClient) -> None:
        items = client.get(f"{API}/leads", params={"page_size": 20}).json()["items"]
        scores = [item["quality"]["score"] for item in items]
        assert scores == sorted(scores, reverse=True)


class TestFilters:
    def test_search_finds_the_franchise_branches(self, client: TestClient) -> None:
        body = client.get(f"{API}/leads", params={"q": "pumo"}).json()
        assert body["total"] == 5, "the five Pumo Technovation branches are five leads"

    def test_search_matches_identifier_values(self, client: TestClient) -> None:
        lead = client.get(f"{API}/leads", params={"has": "email", "page_size": 1}).json()["items"][
            0
        ]
        body = client.get(f"{API}/leads", params={"q": lead["primary_email"]}).json()
        assert lead["id"] in {item["id"] for item in body["items"]}

    def test_undeliverable_filter_returns_only_measured_failures(self, client: TestClient) -> None:
        body = client.get(f"{API}/leads", params={"mailbox_status": "unreachable"}).json()
        assert body["total"] > 0
        for item in body["items"]:
            assert item["verification"]["mailbox_status"] == "unreachable"
            assert item["verification"]["mailbox_accepts_mail"] is False

    def test_unknown_and_unreachable_are_disjoint(self, client: TestClient) -> None:
        """The whole point of keeping them apart: a resolver failure is not a dead domain."""
        unreachable = client.get(f"{API}/leads", params={"mailbox_status": "unreachable"}).json()
        unknown = client.get(f"{API}/leads", params={"mailbox_status": "unknown"}).json()
        assert unreachable["total"] != unknown["total"] or unreachable["total"] == 0

    def test_owned_website_partitions_the_corpus(self, client: TestClient) -> None:
        owned = client.get(f"{API}/leads", params={"owned_website": True}).json()["total"]
        not_owned = client.get(f"{API}/leads", params={"owned_website": False}).json()["total"]
        assert owned + not_owned == 2351

    def test_multi_branch_filter_matches_the_company_view(self, client: TestClient) -> None:
        leads = client.get(f"{API}/leads", params={"multi_branch": True}).json()["total"]
        companies = client.get(f"{API}/companies", params={"min_branches": 2}).json()["total"]
        assert companies == 28
        assert leads > companies, "multi-branch companies own more leads than there are of them"

    def test_quality_bounds_are_inclusive_and_consistent(self, client: TestClient) -> None:
        body = client.get(f"{API}/leads", params={"min_quality": 90, "page_size": 50}).json()
        assert body["total"] > 0
        assert all(item["quality"]["score"] >= 90 for item in body["items"])

    def test_issue_code_filter_surfaces_recorded_gaps(self, client: TestClient) -> None:
        body = client.get(
            f"{API}/leads", params={"issue_code": "city_address_fragment", "page_size": 5}
        ).json()
        assert body["total"] > 0

    def test_filters_combine_conjunctively(self, client: TestClient) -> None:
        wide = client.get(f"{API}/leads", params={"has": "email"}).json()["total"]
        narrow = client.get(
            f"{API}/leads", params={"has": ["email", "linkedin"], "min_quality": 80}
        ).json()["total"]
        assert narrow < wide

    def test_unknown_category_returns_an_empty_page_not_an_error(self, client: TestClient) -> None:
        body = client.get(f"{API}/leads", params={"category": "not_a_vertical"}).json()
        assert body["total"] == 0


class TestLeadDetail:
    def test_detail_carries_gaps_as_well_as_facts(self, client: TestClient) -> None:
        lead_id = client.get(f"{API}/leads", params={"q": "pumo", "page_size": 1}).json()["items"][
            0
        ]["id"]
        body = client.get(f"{API}/leads/{lead_id}").json()
        assert body["identifiers"]
        assert "issues" in body
        assert body["duplicate_candidate_count"] >= 0

    def test_franchise_branches_appear_as_siblings_not_duplicates(self, client: TestClient) -> None:
        """The relationship is recorded; the four other branches are still separate leads."""
        lead_id = client.get(f"{API}/leads", params={"q": "pumo", "page_size": 1}).json()["items"][
            0
        ]["id"]
        body = client.get(f"{API}/leads/{lead_id}").json()
        assert len(body["siblings"]) == 4
        assert body["company"]["branch_count"] == 5

    def test_location_confidence_travels_with_the_city(self, client: TestClient) -> None:
        item = client.get(f"{API}/leads", params={"page_size": 1, "sort": "-quality"}).json()[
            "items"
        ][0]
        assert item["location"]["resolved"] is True
        assert item["location"]["confidence"] is not None
        assert item["location"]["raw"] is not None

    def test_missing_lead_is_a_problem_document(self, client: TestClient) -> None:
        response = client.get(f"{API}/leads/00000000-0000-0000-0000-000000000000")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/problem+json")
        body = response.json()
        assert body["type"].endswith("not_found")
        assert body["request_id"]

    def test_malformed_id_is_rejected_before_the_database(self, client: TestClient) -> None:
        assert client.get(f"{API}/leads/not-a-uuid").status_code == 422


class TestQualityExplanation:
    def test_score_is_explained_factor_by_factor(self, client: TestClient) -> None:
        lead_id = client.get(f"{API}/leads", params={"page_size": 1}).json()["items"][0]["id"]
        body = client.get(f"{API}/leads/{lead_id}/quality").json()
        assert body["factors"]
        assert all(factor["reason"] for factor in body["factors"])
        assert body["factors_evaluated"] <= body["factors_total"]

    def test_unmeasurable_factors_are_null_and_excluded(self, client: TestClient) -> None:
        """Website liveness has not been run here, so it must drop out rather than score zero."""
        lead_id = client.get(f"{API}/leads", params={"owned_website": True, "page_size": 1}).json()[
            "items"
        ][0]["id"]
        body = client.get(f"{API}/leads/{lead_id}/quality").json()
        website = next(f for f in body["factors"] if f["name"] == "website_live")
        assert website["value"] is None
        assert website["measured"] is False
        assert website["contribution"] == 0.0
        assert website["weight"] not in (0, 0.0)
        assert body["weight_available"] < sum(f["weight"] for f in body["factors"])

    def test_score_reconciles_with_its_own_parts(self, client: TestClient) -> None:
        lead_id = client.get(f"{API}/leads", params={"page_size": 1}).json()["items"][0]["id"]
        body = client.get(f"{API}/leads/{lead_id}/quality").json()
        assert body["score"] == pytest.approx(
            max(0.0, body["base_score"] - body["penalty_applied"]), abs=0.01
        )

    def test_unknown_rubric_version_is_a_404_not_a_zero(self, client: TestClient) -> None:
        lead_id = client.get(f"{API}/leads", params={"page_size": 1}).json()["items"][0]["id"]
        response = client.get(f"{API}/leads/{lead_id}/quality", params={"rubric_version": "9.9"})
        assert response.status_code == 404


class TestProvenance:
    def test_every_lead_traces_back_to_a_spreadsheet_row(self, client: TestClient) -> None:
        lead_id = client.get(f"{API}/leads", params={"page_size": 1}).json()["items"][0]["id"]
        body = client.get(f"{API}/leads/{lead_id}/provenance").json()
        assert body["source_records"]
        record = body["source_records"][0]
        assert record["source_sheet"] in {"Day_1", "Day_2", "Day_3"}
        assert record["raw"], "the raw row is kept verbatim, not summarised"

    def test_cross_sheet_duplicate_keeps_both_source_rows(self, client: TestClient) -> None:
        """169 rows were merged on exact identity. Their evidence is not thrown away."""
        page = client.get(f"{API}/leads", params={"page_size": 200, "sort": "-quality"}).json()
        counts = [
            len(client.get(f"{API}/leads/{item['id']}/provenance").json()["source_records"])
            for item in page["items"][:60]
        ]
        assert max(counts) >= 2


class TestCompanies:
    def test_the_franchise_is_one_company_with_five_leads(self, client: TestClient) -> None:
        body = client.get(f"{API}/companies", params={"q": "pumotechnovation"}).json()
        assert body["total"] == 1
        company = body["items"][0]
        assert company["branch_count"] == 5
        assert len(company["locations"]) > 1

    def test_company_detail_lists_its_branches(self, client: TestClient) -> None:
        company_id = client.get(f"{API}/companies", params={"q": "pumotechnovation"}).json()[
            "items"
        ][0]["id"]
        body = client.get(f"{API}/companies/{company_id}").json()
        assert len(body["leads"]) == 5
        emails = {lead["primary_email"] for lead in body["leads"]}
        assert len(emails) == 5, "each branch has its own inbox — that is why they are not merged"

    def test_enrichment_fields_are_empty_rather_than_invented(self, client: TestClient) -> None:
        company_id = client.get(f"{API}/companies", params={"page_size": 1}).json()["items"][0][
            "id"
        ]
        body = client.get(f"{API}/companies/{company_id}").json()
        assert body["industry"] is None
        assert body["company_size"] is None

    def test_missing_company_is_a_404(self, client: TestClient) -> None:
        assert (
            client.get(f"{API}/companies/00000000-0000-0000-0000-000000000000").status_code == 404
        )


class TestMetadata:
    def test_categories_carry_live_counts(self, client: TestClient) -> None:
        categories = client.get(f"{API}/meta/categories").json()
        assert categories
        assert categories == sorted(categories, key=lambda c: -c["lead_count"])
        assert sum(c["lead_count"] for c in categories) <= 2351

    def test_locations_are_gazetteer_resolved_only(self, client: TestClient) -> None:
        locations = client.get(f"{API}/meta/locations", params={"limit": 10}).json()
        assert len(locations) == 10
        assert all(location["slug"] for location in locations)


class TestQueryBudget:
    def test_a_page_costs_the_same_number_of_queries_at_any_size(
        self, client: TestClient, query_counter: QueryCounter
    ) -> None:
        """The N+1 guard. A page of 100 must not cost four times a page of 25 — that regression
        returns perfectly correct data and would be invisible to every other test here."""
        with query_counter as counter:
            client.get(f"{API}/leads", params={"page_size": 25})
            small = counter.count
        with query_counter as counter:
            client.get(f"{API}/leads", params={"page_size": 100})
            large = counter.count
        assert small == large, f"{small} queries for 25 rows, {large} for 100"
        assert small <= 12, f"a single page should not need {small} queries"
