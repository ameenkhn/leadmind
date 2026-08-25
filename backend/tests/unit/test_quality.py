"""Data quality rubric tests.

The property under test is not "the number is right" — there is no ground truth for a
completeness score. It is that the number is *explained*, *reproducible*, and *not a lead score*.
"""

from __future__ import annotations

import pytest

from app.ingestion.quality.rubric import get_rubric, score_lead
from app.ingestion.validators.rules import CONTACT_KINDS
from app.models.enums import IdentifierKind


class TestRubricConfig:
    def test_rubric_loads_and_is_versioned(self) -> None:
        rubric = get_rubric()
        assert rubric.version == "1.0"
        assert rubric.total_weight == pytest.approx(100.0)

    def test_penalties_are_capped(self) -> None:
        """No single defect may zero out an otherwise rich record."""
        rubric = get_rubric()
        assert rubric.max_total_penalty < sum(rubric.penalties.values())


class TestScoring:
    def test_score_is_bounded(self, prepared) -> None:  # type: ignore[no-untyped-def]
        for lead in prepared.leads:
            assert 0 <= score_lead(lead).score <= 100

    def test_every_factor_carries_a_reason(self, prepared) -> None:  # type: ignore[no-untyped-def]
        """The 'Why 87?' panel reads stored facts; it must never have a blank to show."""
        result = score_lead(prepared.leads[0])
        rubric = get_rubric()
        factors = result.factors["factors"]
        assert set(factors) == set(rubric.weights)
        for detail in factors.values():
            assert detail["reason"]
            assert 0 <= detail["value"] <= 1

    def test_contributions_sum_to_the_base_score(self, prepared) -> None:  # type: ignore[no-untyped-def]
        rubric = get_rubric()
        for lead in prepared.leads[:200]:
            result = score_lead(lead)
            earned = sum(d["contribution"] for d in result.factors["factors"].values())
            assert result.factors["base_score"] == pytest.approx(
                earned / rubric.total_weight * 100, abs=0.05
            )

    def test_scoring_is_deterministic(self, prepared) -> None:  # type: ignore[no-untyped-def]
        for lead in prepared.leads[:100]:
            assert score_lead(lead).score == score_lead(lead).score

    def test_rubric_version_travels_with_the_score(self, prepared) -> None:  # type: ignore[no-untyped-def]
        assert score_lead(prepared.leads[0]).rubric_version == get_rubric().version

    def test_thin_records_score_low_without_being_dropped(self, prepared) -> None:  # type: ignore[no-untyped-def]
        thin = [
            lead
            for lead in prepared.leads
            if any(issue.code == "thin_record" for issue in lead.issues)
        ]
        assert thin, "expected thin records in this dataset"
        scores = [score_lead(lead).score for lead in thin]
        assert max(scores) < 60
        # They are still leads: nothing was discarded.
        assert all(lead.display_name for lead in thin)

    def test_a_complete_record_scores_near_the_top(self, prepared) -> None:  # type: ignore[no-untyped-def]
        best = max(prepared.leads, key=lambda lead: score_lead(lead).score)
        result = score_lead(best)
        assert result.score > 95
        assert result.factors["penalty_applied"] == 0

    def test_score_measures_completeness_not_desirability(self, prepared) -> None:  # type: ignore[no-untyped-def]
        """Data quality and lead quality must be independently distributed.

        If a rich profile always meant a good prospect, the two numbers would be redundant and
        the system would be quietly scoring popularity. Here: leads with a large audience are
        found across the whole quality range, and vice versa.
        """
        big_audience = [lead for lead in prepared.leads if (lead.latest_followers or 0) > 50_000]
        scores = [score_lead(lead).score for lead in big_audience]
        assert min(scores) < 70 < max(scores)


class TestSeparationOfConcerns:
    def test_validators_never_reject_a_record(self, prepared) -> None:  # type: ignore[no-untyped-def]
        """Every source row survived validation as a lead or a merge into one."""
        assert len(prepared.leads) + prepared.rows_merged == len(prepared.rows)

    def test_records_with_errors_still_become_leads(self, prepared) -> None:  # type: ignore[no-untyped-def]
        with_errors = [
            lead
            for lead in prepared.leads
            if any(issue.severity.value == "error" for issue in lead.issues)
        ]
        assert with_errors, "expected at least one record with an error-level issue"
        for lead in with_errors:
            assert lead.display_name

    def test_contact_kinds_cover_the_channels_that_can_reach_a_person(self) -> None:
        assert set(CONTACT_KINDS) == {
            IdentifierKind.EMAIL,
            IdentifierKind.PHONE,
            IdentifierKind.WHATSAPP,
        }
