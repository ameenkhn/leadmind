"""Response-envelope and schema semantics. No database, no HTTP."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.common import Page
from app.schemas.duplicates import DuplicateDecisionRequest
from app.schemas.leads import VerificationOut
from app.verification.types import VerificationStatus

LEAD_A = "11111111-1111-1111-1111-111111111111"


class TestPage:
    def test_page_arithmetic(self) -> None:
        page = Page[int].build([1, 2, 3], total=73, page=2, page_size=25)
        assert page.pages == 3
        assert page.has_next is True
        assert page.has_previous is True

    def test_last_page_has_no_next(self) -> None:
        page = Page[int].build([1], total=51, page=3, page_size=25)
        assert page.pages == 3
        assert page.has_next is False

    def test_empty_result_is_zero_pages_not_one(self) -> None:
        """A client that renders `pages` must not be told there is a page with nothing on it."""
        page = Page[int].build([], total=0, page=1, page_size=25)
        assert page.pages == 0
        assert page.has_next is False
        assert page.has_previous is False


class TestVerificationOut:
    """The three states must stay three states.

    Nothing to check, not yet checked, and checked-and-failed are different claims. Any two of
    them collapsed into one is a lead wrongly marked dead or wrongly marked fine.
    """

    def test_no_email_means_null_not_unknown(self) -> None:
        out = VerificationOut.from_signals(None, has_email=False, has_website=False)
        assert out.mailbox_status is None
        assert out.website_status is None

    def test_email_present_but_unmeasured_is_unknown(self) -> None:
        out = VerificationOut.from_signals({}, has_email=True, has_website=False)
        assert out.mailbox_status is VerificationStatus.UNKNOWN
        assert out.mailbox_accepts_mail is False
        assert out.website_status is None

    def test_measured_status_is_carried_through(self) -> None:
        out = VerificationOut.from_signals(
            {
                "mailbox_domain_status": "verified",
                "mailbox_accepts_mail": True,
                "mail_provider": "google",
            },
            has_email=True,
            has_website=False,
        )
        assert out.mailbox_status is VerificationStatus.VERIFIED
        assert out.mailbox_accepts_mail is True
        assert out.mail_provider == "google"

    def test_unreachable_is_not_unknown(self) -> None:
        out = VerificationOut.from_signals(
            {"mailbox_domain_status": "unreachable"}, has_email=True, has_website=False
        )
        assert out.mailbox_status is VerificationStatus.UNREACHABLE
        assert out.mailbox_status is not VerificationStatus.UNKNOWN

    def test_website_signals_are_suppressed_without_a_website(self) -> None:
        """A stale signal must not survive the lead losing the identifier it described."""
        out = VerificationOut.from_signals(
            {"website_status": "verified", "website_is_live": True},
            has_email=True,
            has_website=False,
        )
        assert out.website_status is None
        assert out.website_is_live is False


class TestDecisionRequest:
    def test_reviewer_is_required(self) -> None:
        with pytest.raises(ValidationError):
            DuplicateDecisionRequest(decision="rejected_distinct", reviewer="")

    def test_survivor_is_rejected_when_not_confirming(self) -> None:
        """Naming a survivor while rejecting the pair is a contradiction, not a preference."""
        with pytest.raises(ValidationError):
            DuplicateDecisionRequest(
                decision="rejected_distinct", reviewer="qa", survivor_id=LEAD_A
            )

    def test_pending_is_a_valid_decision(self) -> None:
        request = DuplicateDecisionRequest(decision="pending", reviewer="qa")
        assert request.decision == "pending"

    def test_unknown_decision_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DuplicateDecisionRequest(decision="probably", reviewer="qa")  # type: ignore[arg-type]
