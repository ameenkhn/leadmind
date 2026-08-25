"""Follower-count parsing.

The column arrives half as integers and half as abbreviated strings (``1.4K``, ``173K``,
``12M``). All 2 314 non-null values parse; the interesting part is what the numbers look like
afterwards: median 1 000, p95 129 000, max 12 000 000, and 25.6% below 100.

Two consequences the rest of the system depends on:

* The distribution spans seven orders of magnitude, so the *feature* is ``log1p(followers)``.
  Feeding raw counts to any scoring formula lets one 12M outlier dominate every weight.
* An abbreviated value has lost precision — ``1.4K`` is somewhere in [1350, 1449]. The parsed
  integer is an estimate and is marked as one.
"""

from __future__ import annotations

import math
import re
from typing import Any, Final

from app.ingestion.normalizers.result import Issue, NormalizationResult
from app.models.enums import ValidationSeverity

_SUFFIXES: Final[dict[str, int]] = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
_PATTERN: Final = re.compile(r"^(?P<number>\d+(?:\.\d+)?)\s*(?P<suffix>[KMB])?$", re.I)

# Above this, a follower count on an Indian SMB page is more likely a scrape artefact than a
# real audience. Flagged, never silently discarded.
_IMPLAUSIBLE_CEILING: Final = 500_000_000


def normalize_followers(raw: Any, *, field_name: str = "followers") -> NormalizationResult[int]:
    """Parse an integer or a K/M/B-abbreviated string into an exact count."""
    if raw is None:
        return NormalizationResult.empty()

    if isinstance(raw, bool):
        return NormalizationResult.empty(method="rejected_bool")

    original = str(raw).strip()
    if not original:
        return NormalizationResult.empty()

    if isinstance(raw, int):
        return _result(raw, original, exact=True, method="int")

    if isinstance(raw, float):
        if math.isnan(raw) or math.isinf(raw):
            return NormalizationResult.empty(method="rejected_nan")
        return _result(int(raw), original, exact=float(raw).is_integer(), method="float")

    cleaned = original.replace(",", "").replace(" ", "")
    match = _PATTERN.match(cleaned)
    if match is None:
        return NormalizationResult(
            value=None,
            raw=original,
            confidence=0.0,
            method="regex",
            issues=(
                Issue(
                    field=field_name,
                    code="followers_unparseable",
                    message="value is neither an integer nor a K/M/B-abbreviated count",
                    severity=ValidationSeverity.WARNING,
                    value_raw=original,
                ),
            ),
        )

    number = float(match.group("number"))
    suffix = (match.group("suffix") or "").upper()
    multiplier = _SUFFIXES.get(suffix, 1)
    value = round(number * multiplier)
    return _result(value, original, exact=not suffix, method=f"regex[{suffix or 'plain'}]")


def _result(value: int, original: str, *, exact: bool, method: str) -> NormalizationResult[int]:
    issues: list[Issue] = []
    if value < 0:
        return NormalizationResult(
            value=None,
            raw=original,
            confidence=0.0,
            method=method,
            issues=(
                Issue(
                    field="followers",
                    code="followers_negative",
                    message="follower count cannot be negative",
                    severity=ValidationSeverity.ERROR,
                    value_raw=original,
                ),
            ),
        )
    if value > _IMPLAUSIBLE_CEILING:
        issues.append(
            Issue(
                field="followers",
                code="followers_implausible",
                message=f"follower count {value} exceeds the plausibility ceiling",
                severity=ValidationSeverity.WARNING,
                value_raw=original,
            )
        )

    return NormalizationResult(
        value=value,
        raw=original,
        confidence=1.0 if exact else 0.85,
        method=method,
        issues=tuple(issues),
        attributes={
            "is_exact": exact,
            "log1p": round(math.log1p(value), 6),
        },
    )
