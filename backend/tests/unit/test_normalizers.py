"""Field normalizer tests.

Every case below is a value that actually appears in ``Outbound_Leads.xlsx`` or a failure mode
the dataset analysis identified. Synthetic happy-path cases are deliberately in the minority.
"""

from __future__ import annotations

import math

import pytest

from app.ingestion.normalizers.category import UNCLASSIFIED, normalize_category
from app.ingestion.normalizers.city import normalize_city
from app.ingestion.normalizers.email import normalize_email
from app.ingestion.normalizers.followers import normalize_followers
from app.ingestion.normalizers.name import normalize_name
from app.ingestion.normalizers.phone import normalize_phone
from app.ingestion.normalizers.result import clean_scalar
from app.ingestion.normalizers.url import normalize_url
from app.models.enums import EntityKind, IdentifierKind, ValidationSeverity


class TestCleanScalar:
    def test_float_nan_becomes_none(self) -> None:
        """``str(nan)`` is ``"nan"``, which normalises and stores perfectly happily."""
        assert clean_scalar(float("nan")) is None

    @pytest.mark.parametrize("value", [None, "", "   ", float("inf")])
    def test_absent_values(self, value: object) -> None:
        assert clean_scalar(value) is None


class TestEmail:
    def test_lowercases_and_trims(self) -> None:
        result = normalize_email("  Hari@LiveLongWealth.IN ")
        assert result.value == "hari@livelongwealth.in"
        assert result.raw == "Hari@LiveLongWealth.IN"

    def test_corrects_known_typo_domain(self) -> None:
        """`gamil.com` appears on three rows and would bounce silently."""
        result = normalize_email("someone@gamil.com")
        assert result.value == "someone@gmail.com"
        assert result.confidence < 1.0
        assert any(i.code == "email_typo_domain_corrected" for i in result.issues)

    def test_flags_lookalike_without_changing_it(self) -> None:
        result = normalize_email("someone@gmaill.com")
        assert result.value == "someone@gmaill.com"
        assert any(i.code == "email_domain_lookalike" for i in result.issues)

    def test_classifies_freemail_and_role_account(self) -> None:
        result = normalize_email("info@gmail.com")
        assert result.attributes["is_freemail"] is True
        assert result.attributes["is_role_based"] is True

    def test_corporate_personal_mailbox(self) -> None:
        result = normalize_email("hari@livelongwealth.in")
        assert result.attributes["is_freemail"] is False
        assert result.attributes["is_role_based"] is False

    def test_deliverability_is_never_asserted(self) -> None:
        """Syntactic validity must not be reported as reachability."""
        assert normalize_email("x@example.com").attributes["deliverability"] == "unverified"

    def test_invalid_syntax_is_an_error_not_an_exception(self) -> None:
        result = normalize_email("not-an-email")
        assert result.value is None
        assert result.issues[0].severity is ValidationSeverity.ERROR


class TestPhone:
    def test_indian_mobile_to_e164(self) -> None:
        result = normalize_phone("+91 9010041297")
        assert result.value == "+919010041297"
        assert result.attributes["is_valid"] is True

    def test_local_format_uses_default_region(self) -> None:
        assert normalize_phone("9010041297").value == "+919010041297"

    def test_impossible_number_rejected(self) -> None:
        result = normalize_phone("+91 12")
        assert result.value is None
        assert result.issues[0].code == "phone_impossible"

    def test_reachability_is_never_asserted(self) -> None:
        assert normalize_phone("+91 9010041297").attributes["reachability"] == "unverified"


class TestUrl:
    def test_canonicalises_scheme_www_and_trailing_slash(self) -> None:
        result = normalize_url("http://www.aashaayurveda.com/")
        assert result.value == "https://aashaayurveda.com"
        assert result.attributes["is_owned_domain"] is True

    def test_strips_tracking_parameters(self) -> None:
        result = normalize_url("https://example.com/x?utm_source=fb&id=7&fbclid=abc")
        assert result.value == "https://example.com/x?id=7"

    @pytest.mark.parametrize(
        ("url", "category"),
        [
            ("https://wa.me/message/3WSBTN6TSP4LP1", "messaging"),
            ("https://linktr.ee/someone", "aggregator"),
            ("https://www.threads.com/@someone", "social"),
            ("https://t.me/somechannel", "messaging"),
            ("https://share.google/abc", "shortener"),
            ("https://amazon.in/dp/B01", "marketplace"),
            ("https://forms.gle/abc", "form"),
        ],
    )
    def test_non_owned_hosts_are_not_websites(self, url: str, category: str) -> None:
        """69 rows carry one of these in the Website column. None of them is a website."""
        result = normalize_url(url)
        assert result.attributes["is_owned_domain"] is False
        assert result.attributes["non_owned_category"] == category
        assert any(i.code.startswith("website_not_owned_") for i in result.issues)

    def test_url_with_no_tld_is_rejected(self) -> None:
        """Row Day_1#581 carries `http://www.bellsoverseas/`."""
        result = normalize_url("http://www.bellsoverseas/")
        assert result.value is None
        assert result.issues[0].code == "url_missing_host"

    def test_numeric_facebook_page_flagged(self) -> None:
        result = normalize_url(
            "https://www.facebook.com/61585557833996", kind=IdentifierKind.FACEBOOK
        )
        assert result.attributes["handle_is_numeric_id"] is True

    def test_vanity_facebook_handle_extracted(self) -> None:
        result = normalize_url(
            "https://www.facebook.com/AashaayurvedaMaharashtra", kind=IdentifierKind.FACEBOOK
        )
        assert result.attributes["handle"] == "aashaayurvedamaharashtra"
        assert result.attributes["handle_is_numeric_id"] is False

    @pytest.mark.parametrize(
        ("url", "handle"),
        [
            ("https://youtube.com/@money4meindia", "money4meindia"),
            ("https://youtube.com/channel/UCzWMAvPncII9yCLVPkNXP7A", "uczwmavpncii9yclvpknxp7a"),
            ("https://linkedin.com/company/nismc", "nismc"),
            ("https://linkedin.com/in/sagar-rajput-a4465278", "sagar-rajput-a4465278"),
        ],
    )
    def test_social_handles(self, url: str, handle: str) -> None:
        kind = IdentifierKind.YOUTUBE if "youtube" in url else IdentifierKind.LINKEDIN
        assert normalize_url(url, kind=kind).attributes["handle"] == handle


class TestFollowers:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(3, 3), ("14", 14), ("1.4K", 1400), ("173K", 173_000), ("12M", 12_000_000), ("0", 0)],
    )
    def test_parses_every_shape_in_the_source(self, raw: object, expected: int) -> None:
        assert normalize_followers(raw).value == expected

    def test_abbreviated_values_are_marked_inexact(self) -> None:
        """`1.4K` is somewhere in [1350, 1449]; the stored integer is an estimate."""
        assert normalize_followers("1.4K").attributes["is_exact"] is False
        assert normalize_followers(1400).attributes["is_exact"] is True

    def test_log_scale_is_precomputed(self) -> None:
        assert normalize_followers("1K").attributes["log1p"] == pytest.approx(
            math.log1p(1000), abs=1e-5
        )

    def test_unparseable_is_a_warning_not_a_crash(self) -> None:
        result = normalize_followers("many")
        assert result.value is None
        assert result.issues[0].code == "followers_unparseable"


class TestName:
    def test_normalised_form_collapses_case_and_punctuation(self) -> None:
        left = normalize_name("PUMO Technovation Malumichampatti").value
        right = normalize_name("Pumo Technovation Malumichampatti").value
        assert left == right == "pumo technovation malumichampatti"

    def test_strips_legal_suffix(self) -> None:
        assert normalize_name("Acme Solutions Pvt Ltd").value == "acme solutions"

    def test_detects_anonymised_advertiser_placeholder(self) -> None:
        result = normalize_name("Advertiser 13887200")
        assert result.attributes["is_placeholder"] is True
        assert result.confidence < 0.5

    @pytest.mark.parametrize(
        ("raw", "kind"),
        [
            ("Life Insurance Corporation Of India-LIC", EntityKind.BUSINESS),
            ("Dr Ravi Raj", EntityKind.PERSON),
            ("Amulya Kamarapu", EntityKind.UNKNOWN),
        ],
    )
    def test_entity_kind_admits_uncertainty(self, raw: str, kind: EntityKind) -> None:
        """A two-token personal-looking name is a guess, and guesses stay UNKNOWN."""
        assert normalize_name(raw).attributes["entity_kind"] == kind.value


class TestCity:
    @pytest.mark.parametrize(
        ("raw", "expected"), [("Mumbai", "Mumbai"), ("Bengaluru", "Bangalore"), ("Cochin", "Kochi")]
    )
    def test_resolves_cities_and_aliases(self, raw: str, expected: str) -> None:
        result = normalize_city(raw)
        assert result.value is not None
        assert result.value.name == expected

    @pytest.mark.parametrize("raw", ["Nagar", "Vihar", "Road", "Colony", "Floor", "extension"])
    def test_address_fragments_are_rejected_not_guessed(self, raw: str) -> None:
        """153 rows carry one of these. Fuzzy matching would invent a city for each."""
        result = normalize_city(raw)
        assert result.value is None
        assert result.confidence == 0.0
        assert result.issues[0].code == "city_address_fragment"

    @pytest.mark.parametrize("raw", ["PNB", "Dental Hospital", "Diagnostics", "quality"])
    def test_non_places_are_rejected(self, raw: str) -> None:
        assert normalize_city(raw).issues[0].code == "city_not_a_place"

    def test_locality_resolves_to_parent_with_reduced_confidence(self) -> None:
        result = normalize_city("Koramangala")
        assert result.value is not None
        assert result.value.name == "Bangalore"
        assert result.confidence < 1.0

    def test_unknown_value_stays_unresolved(self) -> None:
        result = normalize_city("Nowherecity")
        assert result.value is None
        assert result.issues[0].code == "city_unresolved"


class TestCategory:
    def test_maps_facebook_category_to_vertical(self) -> None:
        assert normalize_category("Educational consultant").value == "education_training"

    def test_curated_niche_used_when_page_category_missing(self) -> None:
        result = normalize_category(None, "Occult Healing")
        assert result.value == "astrology_spiritual"
        assert result.attributes["matched_on"] == "niche"

    def test_page_category_wins_over_niche(self) -> None:
        result = normalize_category("Yoga studio", "Life Coaching")
        assert result.value == "wellness_fitness"
        assert result.attributes["matched_on"] == "fb_category"

    def test_generic_category_is_unclassified_not_guessed(self) -> None:
        result = normalize_category("Product/service")
        assert result.value == UNCLASSIFIED
        assert result.issues[0].code == "category_generic"

    def test_unknown_category_is_unclassified(self) -> None:
        result = normalize_category("Underwater Basket Weaving")
        assert result.value == UNCLASSIFIED
        assert result.issues[0].code == "category_unmapped"
