"""Business/person name normalisation.

``Name`` in this dataset is a Facebook Page name, not a person's name. It is a business
(``Life Insurance Corporation Of India-LIC``, ``Pumo Technovation Poonamallee``) about a third of
the time and a personal brand (``Amulya Kamarapu``, ``geet india``) the rest. There is no
separate person field and no job title anywhere in the file, so the lead/company split has to be
*derived* — and the derivation must be honest about not knowing, which is why
:class:`~app.models.enums.EntityKind` has an ``UNKNOWN`` member that gets used.

The normalised form exists for deduplication: it strips legal suffixes, punctuation and case so
that ``PUMO Technovation Malumichampatti`` and ``Pumo Technovation Malumichampatti`` collide.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Final

from app.ingestion.normalizers.result import Issue, NormalizationResult, clean_scalar
from app.models.enums import EntityKind, ValidationSeverity

# Meta anonymises some advertisers; six rows carry a synthetic name. They are real leads with
# real contact details, so they are flagged rather than dropped.
_PLACEHOLDER_RE: Final = re.compile(r"^\s*advertiser\s*\d+\s*$", re.I)

_LEGAL_SUFFIXES: Final[tuple[str, ...]] = (
    "private limited",
    "pvt ltd",
    "pvt. ltd.",
    "pvt limited",
    "p ltd",
    "limited",
    "ltd",
    "llp",
    "inc",
    "incorporated",
    "corporation",
    "corp",
    "co",
    "company",
)

_BUSINESS_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "academy",
        "associates",
        "ayurveda",
        "care",
        "center",
        "centre",
        "clinic",
        "college",
        "consultancy",
        "consultants",
        "consulting",
        "corp",
        "diagnostics",
        "enterprises",
        "foundation",
        "group",
        "gym",
        "healthcare",
        "hospital",
        "institute",
        "insurance",
        "international",
        "labs",
        "limited",
        "llp",
        "ltd",
        "pvt",
        "school",
        "services",
        "solutions",
        "studio",
        "technologies",
        "technovation",
        "trust",
        "university",
        "ventures",
        "wellness",
    }
)

_PERSON_HONORIFICS: Final[frozenset[str]] = frozenset(
    {"dr", "dr.", "mr", "mrs", "ms", "prof", "acharya", "pandit", "guru", "vaidya", "ca"}
)

_PUNCT_RE: Final = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE: Final = re.compile(r"\s+")
_MAX_LENGTH: Final = 512


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_name(raw: Any, *, field_name: str = "name") -> NormalizationResult[str]:
    """Return a display name plus a comparison key and an entity-kind guess."""
    cleaned = clean_scalar(raw)
    if cleaned is None:
        return NormalizationResult.empty()

    display = _WS_RE.sub(" ", cleaned)

    issues: list[Issue] = []
    is_placeholder = bool(_PLACEHOLDER_RE.match(display))
    if is_placeholder:
        issues.append(
            Issue(
                field=field_name,
                code="name_placeholder",
                message="advertiser name is an anonymised Meta placeholder",
                severity=ValidationSeverity.WARNING,
                value_raw=display,
            )
        )

    if len(display) > _MAX_LENGTH:
        display = display[:_MAX_LENGTH]
        issues.append(
            Issue(
                field=field_name,
                code="name_truncated",
                message=f"name exceeded {_MAX_LENGTH} characters and was truncated",
                severity=ValidationSeverity.INFO,
            )
        )

    folded = _strip_accents(display).lower()
    folded = _PUNCT_RE.sub(" ", folded)
    folded = _WS_RE.sub(" ", folded).strip()

    for suffix in _LEGAL_SUFFIXES:
        if folded.endswith(f" {suffix}"):
            folded = folded[: -len(suffix) - 1].strip()
            break

    normalized = folded or display.lower()
    tokens = set(normalized.split())
    entity_kind = _classify(tokens, normalized)

    return NormalizationResult(
        value=normalized,
        raw=display,
        confidence=0.2 if is_placeholder else 1.0,
        method="fold+strip_legal_suffix",
        issues=tuple(issues),
        attributes={
            "display_name": display,
            "is_placeholder": is_placeholder,
            "entity_kind": entity_kind.value,
            "token_count": len(normalized.split()),
        },
    )


def _classify(tokens: set[str], normalized: str) -> EntityKind:
    """Guess whether the name denotes a business or a person.

    Deliberately conservative: a two-token name with no business or honorific marker
    (``Amulya Kamarapu``) is *probably* a person but is not asserted as one — this is an
    inference, and inferences that cannot be checked are marked ``UNKNOWN`` so nothing
    downstream treats a guess as an observation.
    """
    if tokens & _BUSINESS_TOKENS:
        return EntityKind.BUSINESS
    if tokens & _PERSON_HONORIFICS:
        return EntityKind.PERSON
    if len(normalized.split()) >= 4:
        return EntityKind.BUSINESS
    return EntityKind.UNKNOWN
