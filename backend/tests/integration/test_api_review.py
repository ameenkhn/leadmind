"""The duplicate review queue, end to end.

These are the tests that matter most in Phase 2, because this is the only part of the API that
writes. The invariants being defended are the ones Phase 1 established and a review tool is most
likely to break: a franchise is not a duplicate, a merge deletes nothing, and every decision can
be undone.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DuplicateCandidate, Lead
from app.models.enums import DuplicateMethod, DuplicateStatus

pytestmark = pytest.mark.integration

API = "/api/v1"
REVIEWER = "integration-test"


def first_pending(client: TestClient, method: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"status": "pending", "page_size": 1, "sort": "-confidence"}
    if method:
        params["method"] = method
    body = client.get(f"{API}/duplicates", params=params).json()
    assert body["items"], "the queue should not be empty over the real dataset"
    item: dict[str, Any] = body["items"][0]
    return item


def decide(client: TestClient, candidate_id: str, **payload: Any) -> Any:
    return client.post(
        f"{API}/duplicates/{candidate_id}/decision",
        json={"reviewer": REVIEWER, **payload},
    )


class TestQueue:
    def test_queue_holds_only_what_was_never_auto_merged(self, client: TestClient) -> None:
        """Exact identity keys merge during ingest. Nothing exact should ever need a human."""
        body = client.get(f"{API}/duplicates", params={"page_size": 200}).json()
        assert body["total"] == 73
        methods = {item["method"] for item in body["items"]}
        assert methods <= {"shared_website", "fuzzy_name"}
        assert all(item["is_auto_mergeable"] is False for item in body["items"])

    def test_both_leads_are_embedded_so_a_reviewer_needs_one_request(
        self, client: TestClient
    ) -> None:
        candidate = first_pending(client)
        assert candidate["lead_a"]["display_name"]
        assert candidate["lead_b"]["display_name"]
        assert candidate["lead_a"]["id"] != candidate["lead_b"]["id"]

    def test_the_field_diff_is_precomputed(self, client: TestClient) -> None:
        candidate = first_pending(client)
        fields = {row["field"]: row for row in candidate["comparison"]}
        assert {"display_name", "email", "website", "company_domain"} <= set(fields)
        for row in candidate["comparison"]:
            if row["a"] is None or row["b"] is None:
                assert row["agrees"] is None, "a missing field is not a disagreement"

    def test_shared_website_pairs_disagree_on_contact_details(self, client: TestClient) -> None:
        """This is the franchise signal. Same domain, different inbox, different city — which is
        exactly why the pipeline refused to merge them."""
        candidate = first_pending(client, method="shared_website")
        fields = {row["field"]: row for row in candidate["comparison"]}
        assert fields["company_domain"]["agrees"] is True
        assert fields["email"]["agrees"] is not True

    def test_filters_narrow_the_queue(self, client: TestClient) -> None:
        fuzzy = client.get(f"{API}/duplicates", params={"method": "fuzzy_name"}).json()["total"]
        shared = client.get(f"{API}/duplicates", params={"method": "shared_website"}).json()[
            "total"
        ]
        assert fuzzy + shared == 73

    def test_a_lead_can_be_asked_for_its_own_pairs(self, client: TestClient) -> None:
        candidate = first_pending(client)
        body = client.get(f"{API}/duplicates", params={"lead_id": candidate["lead_a"]["id"]}).json()
        assert candidate["id"] in {item["id"] for item in body["items"]}

    def test_missing_candidate_is_a_404(self, client: TestClient) -> None:
        response = client.get(f"{API}/duplicates/00000000-0000-0000-0000-000000000000")
        assert response.status_code == 404


class TestConfirming:
    def test_confirming_links_rather_than_deletes(
        self, client: TestClient, corpus_session: Session
    ) -> None:
        candidate = first_pending(client, method="fuzzy_name")
        before = client.get(f"{API}/stats").json()["leads"]

        response = decide(client, candidate["id"], decision="confirmed_duplicate")
        assert response.status_code == 200
        body = response.json()
        loser_id = body["merged_lead_id"]
        survivor_id = body["survivor_id"]
        assert {loser_id, survivor_id} == {candidate["lead_a"]["id"], candidate["lead_b"]["id"]}

        loser = corpus_session.get(Lead, loser_id)
        assert loser is not None
        assert str(loser.merged_into_id) == survivor_id
        assert loser.merged_by == REVIEWER
        assert loser.identifiers, "the loser's identifiers are untouched"

        assert client.get(f"{API}/stats").json()["leads"] == before - 1
        decide(client, candidate["id"], decision="pending")

    def test_a_merged_lead_is_hidden_but_still_addressable(self, client: TestClient) -> None:
        candidate = first_pending(client, method="fuzzy_name")
        loser_id = decide(client, candidate["id"], decision="confirmed_duplicate").json()[
            "merged_lead_id"
        ]

        listed = client.get(f"{API}/leads", params={"page_size": 200}).json()
        assert loser_id not in {item["id"] for item in listed["items"]}

        detail = client.get(f"{API}/leads/{loser_id}")
        assert detail.status_code == 200
        assert detail.json()["merged"]["merged_into_id"]

        included = client.get(
            f"{API}/leads", params={"include_merged": True, "q": detail.json()["display_name"]}
        ).json()
        assert loser_id in {item["id"] for item in included["items"]}
        decide(client, candidate["id"], decision="pending")

    def test_survivor_can_be_chosen_explicitly(self, client: TestClient) -> None:
        candidate = first_pending(client, method="fuzzy_name")
        chosen = candidate["lead_b"]["id"]
        body = decide(
            client, candidate["id"], decision="confirmed_duplicate", survivor_id=chosen
        ).json()
        assert body["survivor_id"] == chosen
        assert body["merged_lead_id"] == candidate["lead_a"]["id"]
        decide(client, candidate["id"], decision="pending")

    def test_default_survivor_is_the_better_evidenced_lead(self, client: TestClient) -> None:
        candidate = first_pending(client, method="fuzzy_name")
        scores = {
            candidate["lead_a"]["id"]: candidate["lead_a"]["quality"]["score"],
            candidate["lead_b"]["id"]: candidate["lead_b"]["quality"]["score"],
        }
        body = decide(client, candidate["id"], decision="confirmed_duplicate").json()
        best = max(scores, key=lambda key: scores[key])
        assert body["survivor_id"] == best
        decide(client, candidate["id"], decision="pending")

    def test_confirming_twice_is_idempotent(self, client: TestClient) -> None:
        candidate = first_pending(client, method="fuzzy_name")
        first = decide(client, candidate["id"], decision="confirmed_duplicate").json()
        second = decide(client, candidate["id"], decision="confirmed_duplicate").json()
        assert first["merged_lead_id"] == second["merged_lead_id"]
        assert first["survivor_id"] == second["survivor_id"]
        assert client.get(f"{API}/stats").json()["leads_merged_by_review"] == 1
        decide(client, candidate["id"], decision="pending")

    def test_survivor_must_belong_to_the_pair(self, client: TestClient) -> None:
        candidate = first_pending(client)
        other = client.get(f"{API}/leads", params={"page_size": 200}).json()["items"][-1]["id"]
        response = decide(
            client, candidate["id"], decision="confirmed_duplicate", survivor_id=other
        )
        assert response.status_code == 422
        assert response.json()["type"].endswith("invalid_request")


class TestUndo:
    def test_pending_reverses_the_merge_completely(
        self, client: TestClient, corpus_session: Session
    ) -> None:
        candidate = first_pending(client, method="fuzzy_name")
        before = client.get(f"{API}/stats").json()["leads"]
        loser_id = decide(client, candidate["id"], decision="confirmed_duplicate").json()[
            "merged_lead_id"
        ]

        undo = decide(client, candidate["id"], decision="pending", note="not the same business")
        assert undo.status_code == 200
        assert undo.json()["unmerged"] is True
        assert undo.json()["candidate"]["status"] == "pending"

        corpus_session.expire_all()
        loser = corpus_session.get(Lead, loser_id)
        assert loser is not None
        assert loser.merged_into_id is None
        assert loser.merged_at is None
        assert loser.merged_by is None
        assert client.get(f"{API}/stats").json()["leads"] == before

    def test_rejecting_records_the_judgement_without_touching_the_leads(
        self, client: TestClient
    ) -> None:
        candidate = first_pending(client, method="shared_website")
        before = client.get(f"{API}/stats").json()["leads"]
        response = decide(
            client, candidate["id"], decision="rejected_distinct", note="separate branches"
        )
        assert response.status_code == 200
        body = response.json()
        assert body["merged_lead_id"] is None
        assert body["candidate"]["status"] == "rejected_distinct"
        assert body["candidate"]["resolved_by"] == REVIEWER
        assert body["candidate"]["resolution_note"] == "separate branches"
        assert client.get(f"{API}/stats").json()["leads"] == before
        decide(client, candidate["id"], decision="pending")

    def test_reject_after_confirm_undoes_the_merge(self, client: TestClient) -> None:
        candidate = first_pending(client, method="fuzzy_name")
        decide(client, candidate["id"], decision="confirmed_duplicate")
        response = decide(client, candidate["id"], decision="rejected_distinct")
        assert response.json()["unmerged"] is True
        assert client.get(f"{API}/stats").json()["leads_merged_by_review"] == 0
        decide(client, candidate["id"], decision="pending")


class TestConflicts:
    def test_a_lead_cannot_be_merged_into_two_survivors(
        self, client: TestClient, corpus_session: Session
    ) -> None:
        """The reviewer is told to undo the earlier decision rather than having it silently
        overwritten — an overwrite would strand the first survivor's evidence."""
        shared = corpus_session.execute(
            select(DuplicateCandidate.lead_a_id, DuplicateCandidate.id)
            .where(DuplicateCandidate.status == DuplicateStatus.PENDING)
            .order_by(DuplicateCandidate.lead_a_id)
        ).all()
        by_lead: dict[Any, list[Any]] = {}
        for lead_id, candidate_id in shared:
            by_lead.setdefault(lead_id, []).append(candidate_id)
        overlapping = next((ids for ids in by_lead.values() if len(ids) > 1), None)
        if overlapping is None:
            pytest.skip("no lead in this corpus appears in two pending pairs")

        first, second = overlapping[0], overlapping[1]
        shared_lead = next(lead for lead, ids in by_lead.items() if ids == overlapping)
        assert (
            decide(
                client, str(first), decision="confirmed_duplicate", survivor_id=str(shared_lead)
            ).status_code
            == 200
        )
        response = decide(
            client, str(second), decision="confirmed_duplicate", survivor_id=str(shared_lead)
        )
        # Either it merges the second loser into the same survivor, or it refuses because that
        # loser already points somewhere else. Both are correct; a silent re-point is not.
        assert response.status_code in (200, 409)
        if response.status_code == 409:
            assert response.json()["type"].endswith("conflict")
        decide(client, str(first), decision="pending")
        decide(client, str(second), decision="pending")


class TestDecisionsAsData:
    def test_confirm_rate_is_measured_from_reviewed_pairs(self, client: TestClient) -> None:
        """The point of recording rejections: this is the detector's observed precision, and the
        number the fuzzy-name threshold should be tuned against."""
        fuzzy = client.get(
            f"{API}/duplicates",
            params={"method": "fuzzy_name", "status": "pending", "page_size": 4},
        ).json()["items"]
        assert len(fuzzy) >= 3

        decide(client, fuzzy[0]["id"], decision="confirmed_duplicate")
        decide(client, fuzzy[1]["id"], decision="rejected_distinct")
        decide(client, fuzzy[2]["id"], decision="rejected_distinct")

        stats = client.get(f"{API}/stats/review").json()
        by_method = {row["method"]: row for row in stats["by_method"]}
        fuzzy_stats = by_method[DuplicateMethod.FUZZY_NAME.value]
        assert fuzzy_stats["confirmed_duplicate"] == 1
        assert fuzzy_stats["rejected_distinct"] == 2
        assert fuzzy_stats["confirm_rate"] == pytest.approx(1 / 3, abs=1e-4)
        assert stats["reviewers"][REVIEWER] == 3
        assert stats["decided"] == 3

        for candidate in fuzzy[:3]:
            decide(client, candidate["id"], decision="pending")
        assert client.get(f"{API}/stats/review").json()["pending"] == 73

    def test_undecided_methods_report_no_rate_rather_than_zero(self, client: TestClient) -> None:
        """A method nobody has reviewed has an unknown precision, not a precision of zero."""
        stats = client.get(f"{API}/stats/review").json()
        for row in stats["by_method"]:
            if row["confirmed_duplicate"] + row["rejected_distinct"] == 0:
                assert row["confirm_rate"] is None
