"""Statistics endpoints, and the HTTP contract everything else relies on."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

API = "/api/v1"


class TestCorpusStats:
    def test_reconciliation_is_served_from_the_run_that_produced_it(
        self, client: TestClient
    ) -> None:
        """The identity that proves ingestion lost nothing, checkable against a live system."""
        body = client.get(f"{API}/stats").json()
        recon = body["reconciliation"]
        assert recon["rows_read"] == 2520
        assert recon["rows_merged"] == 169
        assert recon["leads_total"] == 2351
        assert recon["reconciles"] is True
        assert recon["rows_read"] == recon["leads_total"] + recon["rows_merged"]
        assert recon["source_sha256"]
        assert recon["code_version"]

    def test_counts_match_the_pipeline_report(self, client: TestClient) -> None:
        body = client.get(f"{API}/stats").json()
        assert body["leads"] == 2351
        assert body["companies"] == 1826
        assert body["companies_multi_branch"] == 28
        assert body["identifiers"] == 11287
        assert body["metric_observations"] == 2314
        assert body["leads_merged_by_review"] == 0

    def test_identifier_channels_are_broken_out(self, client: TestClient) -> None:
        body = client.get(f"{API}/stats").json()
        assert sum(body["identifiers_by_kind"].values()) == body["identifiers"]
        assert body["identifiers_by_kind"]["email"] > 0

    def test_validation_issues_are_counted_not_hidden(self, client: TestClient) -> None:
        """Nothing was dropped for failing validation; the reasons are countable."""
        body = client.get(f"{API}/stats").json()
        assert body["validation_issues"] > 0
        assert "profile_numeric_id_only" in body["validation_issues_by_code"]

    def test_weak_labels_are_kept_separate_from_gold(self, client: TestClient) -> None:
        body = client.get(f"{API}/stats").json()
        assert body["eval_labels_by_source"]["weak_relevance"] == 900
        assert "human_gold" not in body["eval_labels_by_source"]


class TestQualityStats:
    def test_distribution_covers_every_scored_lead(self, client: TestClient) -> None:
        body = client.get(f"{API}/stats/quality").json()
        assert body["rubric_version"] == "1.1"
        assert body["scored_leads"] == 2351
        assert sum(body["histogram"].values()) == 2351

    def test_percentiles_are_ordered(self, client: TestClient) -> None:
        body = client.get(f"{API}/stats/quality").json()
        assert body["p10"] <= body["median"] <= body["p90"]

    def test_factor_coverage_is_reported_alongside_the_scores(self, client: TestClient) -> None:
        """Two leads with the same score but different factor counts are different claims."""
        body = client.get(f"{API}/stats/quality").json()
        assert body["factors_evaluated"]
        assert sum(body["factors_evaluated"].values()) == 2351

    def test_unknown_rubric_version_reports_zero_rather_than_guessing(
        self, client: TestClient
    ) -> None:
        body = client.get(f"{API}/stats/quality", params={"rubric_version": "0.1"}).json()
        assert body["scored_leads"] == 0
        assert body["mean"] is None


class TestVerificationStats:
    def test_coverage_lists_both_checks(self, client: TestClient) -> None:
        body = client.get(f"{API}/stats/verification").json()
        kinds = {row["kind"] for row in body["coverage"]}
        assert kinds == {"email_domains", "websites"}

    def test_unreachable_is_reported_separately_from_unknown(self, client: TestClient) -> None:
        body = client.get(f"{API}/stats/verification").json()
        domains = next(row for row in body["coverage"] if row["kind"] == "email_domains")
        assert domains["by_status"]["unreachable"] > 0
        assert domains["by_status"]["unknown"] > 0
        assert body["leads_proven_undeliverable"] > 0

    def test_freshness_is_visible(self, client: TestClient) -> None:
        """A verification is a statement about a moment; staleness has to be legible."""
        body = client.get(f"{API}/stats/verification").json()
        domains = next(row for row in body["coverage"] if row["kind"] == "email_domains")
        assert domains["fresh"] + domains["stale"] == domains["records"]


class TestReviewStats:
    def test_queue_depth_matches_the_queue(self, client: TestClient) -> None:
        body = client.get(f"{API}/stats/review").json()
        listed = client.get(f"{API}/duplicates", params={"status": "pending"}).json()["total"]
        assert body["pending"] == listed == 73
        assert body["decided"] == 0


class TestContract:
    def test_liveness_does_not_depend_on_the_database(self, client: TestClient) -> None:
        body = client.get("/healthz").json()
        assert body["status"] == "ok"
        assert body["version"]

    def test_readiness_checks_the_schema_revision(self, client: TestClient) -> None:
        response = client.get("/readyz")
        assert response.status_code == 200
        body = response.json()
        assert body["database"] == "reachable"
        assert body["schema_current"] is True
        assert body["schema_revision"] == body["expected_revision"]

    def test_every_response_carries_a_request_id(self, client: TestClient) -> None:
        response = client.get(f"{API}/stats")
        assert response.headers["X-Request-ID"]

    def test_an_inbound_request_id_is_honoured(self, client: TestClient) -> None:
        """So a trace survives a proxy or a frontend that already generates one."""
        response = client.get(f"{API}/stats", headers={"X-Request-ID": "trace-abc"})
        assert response.headers["X-Request-ID"] == "trace-abc"

    def test_errors_use_problem_json_with_the_request_id(self, client: TestClient) -> None:
        response = client.get(f"{API}/companies/00000000-0000-0000-0000-000000000000")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/problem+json")
        body = response.json()
        assert body["status"] == 404
        assert body["instance"].endswith("00000000-0000-0000-0000-000000000000")
        assert body["request_id"] == response.headers["X-Request-ID"]

    def test_validation_failures_name_the_offending_parameter(self, client: TestClient) -> None:
        response = client.get(f"{API}/leads", params={"mailbox_status": "maybe"})
        assert response.status_code == 422
        body = response.json()
        assert body["errors"]
        assert body["errors"][0]["location"] == ["query", "mailbox_status"]

    def test_unknown_route_is_still_a_problem_document(self, client: TestClient) -> None:
        response = client.get(f"{API}/nope")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/problem+json")

    def test_openapi_schema_is_generated(self, client: TestClient) -> None:
        schema = client.get("/openapi.json").json()
        assert schema["info"]["title"] == "LeadMind API"
        assert f"{API}/leads" in schema["paths"]
        assert f"{API}/duplicates/{{candidate_id}}/decision" in schema["paths"]

    def test_probes_are_not_versioned(self, client: TestClient) -> None:
        """An orchestrator's health check should not need to know the API version."""
        schema = client.get("/openapi.json").json()
        assert "/healthz" in schema["paths"]
        assert f"{API}/healthz" not in schema["paths"]
