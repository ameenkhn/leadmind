"""Data quality rubric tests.

The property under test is not "the number is right" — there is no ground truth for a
completeness score. It is that the number is *explained*, *reproducible*, and *not a lead score*.
"""

from __future__ import annotations

import pytest

from app.ingestion.quality.rubric import VerificationSignals, get_rubric, score_lead
from app.ingestion.validators.rules import CONTACT_KINDS
from app.models.enums import IdentifierKind
from app.verification.types import VerificationStatus


class TestRubricConfig:
    def test_rubric_loads_and_is_versioned(self) -> None:
        rubric = get_rubric()
        assert rubric.version == "1.1"
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
            if detail["measured"]:
                assert 0 <= detail["value"] <= 1
            else:
                # An unmeasured factor still has to say why it could not be measured.
                assert detail["value"] is None

    def test_contributions_sum_to_the_base_score(self, prepared) -> None:  # type: ignore[no-untyped-def]
        """The denominator is the weight actually evaluated, not the rubric total.

        This is what makes an unmeasured factor neutral rather than a penalty.
        """
        for lead in prepared.leads[:200]:
            result = score_lead(lead)
            earned = sum(d["contribution"] for d in result.factors["factors"].values())
            available = result.factors["weight_available"]
            assert result.factors["base_score"] == pytest.approx(earned / available * 100, abs=0.05)

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


class TestUnmeasuredFactors:
    """Phase 1b added factors that cannot be scored until verification has run."""

    def test_unverified_factors_drop_out_rather_than_scoring_zero(self, prepared) -> None:  # type: ignore[no-untyped-def]
        """Otherwise every lead is punished for work the operator has not done yet."""
        lead = next(entry for entry in prepared.leads if entry.company_domain)
        result = score_lead(lead)
        factors = result.factors["factors"]
        assert factors["mailbox_verified"]["measured"] is False
        assert factors["website_live"]["measured"] is False
        assert result.factors["weight_available"] < get_rubric().total_weight
        assert result.factors["factors_evaluated"] == result.factors["factors_total"] - 2

    def test_verified_mailbox_raises_the_score(self, prepared) -> None:  # type: ignore[no-untyped-def]
        lead = next(entry for entry in prepared.leads if entry.company_domain)
        unmeasured = score_lead(lead)
        verified = score_lead(
            lead,
            signals=VerificationSignals(
                mailbox_domain_status=VerificationStatus.VERIFIED,
                mailbox_accepts_mail=True,
                mail_provider="google",
            ),
        )
        assert verified.score > unmeasured.score
        assert verified.factors["factors"]["mailbox_verified"]["value"] == 1.0

    def test_dead_mailbox_domain_lowers_the_score_and_names_the_reason(self, prepared) -> None:  # type: ignore[no-untyped-def]
        lead = next(entry for entry in prepared.leads if entry.company_domain)
        dead = score_lead(
            lead,
            signals=VerificationSignals(
                mailbox_domain_status=VerificationStatus.UNREACHABLE,
                mailbox_accepts_mail=False,
            ),
        )
        assert dead.score < score_lead(lead).score
        assert "mailbox_domain_dead" in dead.factors["penalties"]

    def test_resolver_failure_is_treated_as_unmeasured_not_as_a_dead_domain(self, prepared) -> None:  # type: ignore[no-untyped-def]
        """The distinction the whole verification layer exists to preserve."""
        lead = next(entry for entry in prepared.leads if entry.company_domain)
        baseline = score_lead(lead)
        blip = score_lead(
            lead, signals=VerificationSignals(mailbox_domain_status=VerificationStatus.UNKNOWN)
        )
        assert blip.score == baseline.score
        assert "mailbox_domain_dead" not in blip.factors["penalties"]

    def test_parked_website_is_penalised(self, prepared) -> None:  # type: ignore[no-untyped-def]
        lead = next(entry for entry in prepared.leads if entry.company_domain)
        parked = score_lead(
            lead,
            signals=VerificationSignals(
                website_status=VerificationStatus.VERIFIED,
                website_is_live=False,
                website_is_parked=True,
            ),
        )
        assert "website_dead" in parked.factors["penalties"]

    def test_a_lead_with_no_website_is_not_marked_down_for_liveness(self, prepared) -> None:  # type: ignore[no-untyped-def]
        lead = next(entry for entry in prepared.leads if not entry.company_domain)
        result = score_lead(lead)
        detail = result.factors["factors"]["website_live"]
        assert detail["measured"] is False
        assert "no owned website" in detail["reason"]


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
