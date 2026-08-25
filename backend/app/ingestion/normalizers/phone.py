"""Phone normalisation to E.164.

Every one of the 2 520 source numbers is already ``+91 XXXXXXXXXX`` and exactly 12 digits, so
this normalizer has almost nothing to repair today. It exists anyway because the uniformity is a
property of one scrape, not of the pipeline, and because ``libphonenumber`` catches things a
length check cannot: valid-looking numbers in unassigned ranges, landlines vs mobiles, and
numbers that are well-formed but not dialable.
"""

from __future__ import annotations

from typing import Any, Final

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberType

from app.ingestion.normalizers.result import Issue, NormalizationResult, clean_scalar
from app.models.enums import ValidationSeverity

DEFAULT_REGION: Final = "IN"

_MOBILE_TYPES: Final = frozenset({PhoneNumberType.MOBILE, PhoneNumberType.FIXED_LINE_OR_MOBILE})


def normalize_phone(
    raw: Any, *, field_name: str = "phone", region: str = DEFAULT_REGION
) -> NormalizationResult[str]:
    """Parse to E.164 and classify line type."""
    original = clean_scalar(raw)
    if original is None:
        return NormalizationResult.empty()

    try:
        parsed = phonenumbers.parse(original, region)
    except NumberParseException as exc:
        return NormalizationResult(
            value=None,
            raw=original,
            confidence=0.0,
            method="libphonenumber",
            issues=(
                Issue(
                    field=field_name,
                    code="phone_unparseable",
                    message=f"could not parse phone number: {exc.error_type}",
                    severity=ValidationSeverity.ERROR,
                    value_raw=original,
                ),
            ),
        )

    issues: list[Issue] = []
    if not phonenumbers.is_possible_number(parsed):
        issues.append(
            Issue(
                field=field_name,
                code="phone_impossible",
                message="number length is not possible for its country code",
                severity=ValidationSeverity.ERROR,
                value_raw=original,
            )
        )
        return NormalizationResult(
            value=None,
            raw=original,
            confidence=0.0,
            method="libphonenumber",
            issues=tuple(issues),
        )

    is_valid = phonenumbers.is_valid_number(parsed)
    if not is_valid:
        issues.append(
            Issue(
                field=field_name,
                code="phone_invalid",
                message="number is possible but not assigned in any known range",
                severity=ValidationSeverity.WARNING,
                value_raw=original,
            )
        )

    e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    line_type = phonenumbers.number_type(parsed)

    return NormalizationResult(
        value=e164,
        raw=original,
        confidence=1.0 if is_valid else 0.5,
        method="libphonenumber",
        issues=tuple(issues),
        attributes={
            "country_code": parsed.country_code,
            "national_number": str(parsed.national_number),
            "is_valid": is_valid,
            "is_mobile": line_type in _MOBILE_TYPES,
            "line_type": PhoneNumberType.to_string(line_type)
            if hasattr(PhoneNumberType, "to_string")
            else str(line_type),
            "reachability": "unverified",
        },
    )
