"""Email domain verification tests.

Run against a stub resolver rather than real DNS. Tests that depend on the internet fail when
someone's wifi drops and teach you nothing about your own code; the one thing worth testing here
is the mapping from DNS outcome to recorded status, and that mapping is pure logic.

The case these tests exist for: an NXDOMAIN and a resolver timeout must not produce the same
record. The first is a measurement, the second is the absence of one, and folding them together
would let a network blip silently mark thousands of good leads undeliverable.
"""

from __future__ import annotations

import pytest

from app.ingestion.quality.rubric import VerificationSignals
from app.verification.email_domain import classify_provider, verify_domain
from app.verification.resolver import MxRecord, StaticResolver
from app.verification.types import MailProvider, VerificationStatus

GOOGLE_MX = [
    MxRecord(1, "aspmx.l.google.com"),
    MxRecord(5, "alt1.aspmx.l.google.com"),
]


def resolver(**records: list[MxRecord] | None) -> StaticResolver:
    return StaticResolver(dict(records))


class TestProviderClassification:
    @pytest.mark.parametrize(
        ("hosts", "expected"),
        [
            (("aspmx.l.google.com",), MailProvider.GOOGLE),
            (("example-com.mail.protection.outlook.com",), MailProvider.MICROSOFT),
            (("mx.zoho.in",), MailProvider.ZOHO),
            (("smtp.secureserver.net",), MailProvider.GODADDY),
            (("mx1.hostinger.in",), MailProvider.HOSTINGER),
            (("mail.example.com",), MailProvider.SELF_HOSTED),
            (("mx.some-unknown-host.net",), MailProvider.OTHER),
            ((), MailProvider.NONE),
        ],
    )
    def test_classifies_from_mx_hostnames(
        self, hosts: tuple[str, ...], expected: MailProvider
    ) -> None:
        assert classify_provider(hosts, "example.com") is expected

    def test_microsoft_suffix_wins_over_bare_outlook(self) -> None:
        """Longest-suffix-first, so `outlook.com` cannot shadow the protection hostname."""
        assert (
            classify_provider(("acme.mail.protection.outlook.com",), "acme.com")
            is MailProvider.MICROSOFT
        )


class TestVerifyDomain:
    async def test_mx_present_is_verified(self) -> None:
        result = await verify_domain("acme.com", resolver(**{"acme.com": GOOGLE_MX}))
        assert result.status is VerificationStatus.VERIFIED
        assert result.has_mx
        assert result.accepts_mail
        assert result.provider is MailProvider.GOOGLE
        assert result.mx_hosts == ("aspmx.l.google.com", "alt1.aspmx.l.google.com")

    async def test_domain_without_mx_is_unreachable(self) -> None:
        result = await verify_domain("acme.com", resolver(**{"acme.com": []}))
        assert result.status is VerificationStatus.UNREACHABLE
        assert result.has_mx is False
        assert result.accepts_mail is False
        assert "no MX" in (result.error or "")

    async def test_nxdomain_is_unreachable_not_unknown(self) -> None:
        """The domain is not registered. That is a fact, and it is actionable."""
        result = await verify_domain("gone.com", resolver(**{"gone.com": None}))
        assert result.status is VerificationStatus.UNREACHABLE
        assert result.error == "NXDOMAIN"

    async def test_resolver_failure_is_unknown_not_unreachable(self) -> None:
        """The distinction this whole module exists to preserve."""
        result = await verify_domain("acme.com", resolver())  # no stub -> lookup error
        assert result.status is VerificationStatus.UNKNOWN
        assert result.status.is_measurement is False
        assert result.has_mx is False

    async def test_freemail_and_disposable_are_carried_through(self) -> None:
        verified = await verify_domain("gmail.com", resolver(**{"gmail.com": GOOGLE_MX}))
        assert verified.is_freemail is True

        throwaway = await verify_domain("mailinator.com", resolver(**{"mailinator.com": GOOGLE_MX}))
        assert throwaway.is_disposable is True

    async def test_domain_is_normalised_before_lookup(self) -> None:
        stub = resolver(**{"acme.com": GOOGLE_MX})
        result = await verify_domain("  ACME.com.  ", stub)
        assert result.domain == "acme.com"
        assert stub.calls == ["acme.com"]

    async def test_records_are_sorted_by_preference(self) -> None:
        unsorted = [MxRecord(20, "b.example.com"), MxRecord(10, "a.example.com")]
        result = await verify_domain("acme.com", resolver(**{"acme.com": unsorted}))
        assert result.mx_hosts == ("a.example.com", "b.example.com")

    async def test_payload_is_json_serialisable(self) -> None:
        import json

        result = await verify_domain("acme.com", resolver(**{"acme.com": GOOGLE_MX}))
        assert json.loads(json.dumps(result.as_payload()))["provider"] == "google"


class TestStatusSemantics:
    def test_only_verified_and_unreachable_count_as_measurements(self) -> None:
        assert VerificationStatus.VERIFIED.is_measurement
        assert VerificationStatus.UNREACHABLE.is_measurement
        assert not VerificationStatus.UNKNOWN.is_measurement
        assert not VerificationStatus.SKIPPED.is_measurement


class TestSignalMapping:
    def test_empty_payload_means_unmeasured(self) -> None:
        signals = VerificationSignals.from_mapping(None)
        assert signals.mailbox_domain_status is None
        assert signals.mailbox_accepts_mail is False

    def test_round_trips_a_runner_payload(self) -> None:
        signals = VerificationSignals.from_mapping(
            {
                "mailbox_domain_status": "verified",
                "mailbox_accepts_mail": True,
                "mail_provider": "google",
                "website_status": "unreachable",
                "website_is_live": False,
                "website_is_parked": True,
            }
        )
        assert signals.mailbox_domain_status is VerificationStatus.VERIFIED
        assert signals.website_status is VerificationStatus.UNREACHABLE
        assert signals.website_is_parked is True
