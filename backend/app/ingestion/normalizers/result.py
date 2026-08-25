"""The contract every normalizer honours.

A normalizer is a pure function: raw value in, :class:`NormalizationResult` out. It never raises
for bad input and never returns a bare value, because "this phone number is unusable" is
information the pipeline must keep, not an exception to swallow. Confidence and method travel
with the value so any downstream number can be explained.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from app.models.enums import ValidationSeverity

T = TypeVar("T")


def clean_scalar(raw: Any) -> str | None:
    """Collapse absent-ish values to ``None`` and everything else to a trimmed string.

    Guards against float ``nan``, which ``str()`` would otherwise turn into the string
    ``"nan"`` — a value that then normalises, validates and stores perfectly happily.
    Normalizers are called from the pipeline (which already cleans) and from tests and
    notebooks (which do not), so the guard lives here rather than at one call site.
    """
    if raw is None:
        return None
    if isinstance(raw, float) and (math.isnan(raw) or math.isinf(raw)):
        return None
    text = str(raw).strip()
    return text or None


@dataclass(frozen=True, slots=True)
class Issue:
    """A single thing that was wrong with, or notable about, a value."""

    field: str
    code: str
    message: str
    severity: ValidationSeverity = ValidationSeverity.WARNING
    value_raw: str | None = None


@dataclass(frozen=True, slots=True)
class NormalizationResult(Generic[T]):
    """The outcome of normalising one field."""

    value: T | None
    raw: str | None
    confidence: float
    method: str
    issues: tuple[Issue, ...] = ()
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.value is not None

    @property
    def has_error(self) -> bool:
        return any(i.severity is ValidationSeverity.ERROR for i in self.issues)

    @classmethod
    def empty(cls, method: str = "absent") -> NormalizationResult[T]:
        return cls(value=None, raw=None, confidence=0.0, method=method)

    def with_issue(self, issue: Issue) -> NormalizationResult[T]:
        return NormalizationResult(
            value=self.value,
            raw=self.raw,
            confidence=self.confidence,
            method=self.method,
            issues=(*self.issues, issue),
            attributes=self.attributes,
        )
