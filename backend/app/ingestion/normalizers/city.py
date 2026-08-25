"""Location resolution against a gazetteer.

``.strip().title()`` would be enough if ``City`` held cities. It does not: it holds whatever
token an address parser happened to grab, so 24 leads are in "Nagar", 9 are on "Road", and one
is at "Dental Hospital". Resolution therefore returns a *confidence* alongside a location, and
"unresolved" is a normal, frequent, correct answer.

Deliberately absent: fuzzy matching against the city list. Edit distance would happily map
"Kunj" to "Kanpur" and invent a location the data never contained.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import yaml

from app.core.config import get_settings
from app.core.errors import ConfigurationError
from app.ingestion.normalizers.result import Issue, NormalizationResult, clean_scalar
from app.models.enums import ValidationSeverity

_WS_RE: Final = re.compile(r"\s+")

CONFIDENCE_EXACT: Final = 1.0
CONFIDENCE_ALIAS: Final = 0.9
CONFIDENCE_LOCALITY: Final = 0.75
CONFIDENCE_STATE: Final = 0.5


@dataclass(frozen=True, slots=True)
class ResolvedLocation:
    """A gazetteer hit."""

    slug: str
    name: str
    state: str | None
    country_code: str
    granularity: str
    match_kind: str


@dataclass(frozen=True, slots=True)
class Gazetteer:
    """Immutable lookup tables built once from ``config/gazetteer.yaml``."""

    version: str
    country_code: str
    by_key: dict[str, ResolvedLocation]
    match_kinds: dict[str, str]
    address_fragments: frozenset[str]
    non_place_tokens: frozenset[str]

    def lookup(self, key: str) -> tuple[ResolvedLocation, str] | None:
        location = self.by_key.get(key)
        if location is None:
            return None
        return location, self.match_kinds[key]

    @property
    def city_count(self) -> int:
        return len({loc.slug for loc in self.by_key.values() if loc.granularity == "city"})


def normalize_key(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = re.sub(r"[^\w\s-]", " ", stripped.lower())
    return _WS_RE.sub(" ", cleaned).strip()


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", normalize_key(value)).strip("-")


def _register(
    by_key: dict[str, ResolvedLocation],
    kinds: dict[str, str],
    key: str,
    location: ResolvedLocation,
    match_kind: str,
    *,
    path: Path,
) -> None:
    if not key:
        return
    existing = by_key.get(key)
    if existing is not None and existing.slug != location.slug:
        raise ConfigurationError(
            "gazetteer key maps to two different locations",
            key=key,
            first=existing.slug,
            second=location.slug,
            file=str(path),
        )
    by_key[key] = location
    kinds[key] = match_kind


def load_gazetteer(path: Path | None = None) -> Gazetteer:
    settings = get_settings()
    source = path or settings.config_file("gazetteer.yaml")
    data: dict[str, Any] = yaml.safe_load(source.read_text(encoding="utf-8"))

    country = str(data.get("country_code", "IN"))
    by_key: dict[str, ResolvedLocation] = {}
    kinds: dict[str, str] = {}
    city_by_name: dict[str, ResolvedLocation] = {}

    for entry in data.get("cities", []):
        name = str(entry["name"])
        location = ResolvedLocation(
            slug=slugify(name),
            name=name,
            state=entry.get("state"),
            country_code=country,
            granularity="city",
            match_kind="exact",
        )
        city_by_name[normalize_key(name)] = location
        _register(by_key, kinds, normalize_key(name), location, "exact", path=source)
        for alias in entry.get("aliases", []) or []:
            _register(by_key, kinds, normalize_key(str(alias)), location, "alias", path=source)

    for locality, parent in (data.get("localities") or {}).items():
        parent_location = city_by_name.get(normalize_key(str(parent)))
        if parent_location is None:
            raise ConfigurationError(
                "locality references a city not present in the gazetteer",
                locality=locality,
                parent=parent,
                file=str(source),
            )
        _register(
            by_key, kinds, normalize_key(str(locality)), parent_location, "locality", path=source
        )

    for entry in data.get("states", []):
        name = str(entry["name"])
        location = ResolvedLocation(
            slug=slugify(f"state {name}"),
            name=name,
            state=name,
            country_code=country,
            granularity="state",
            match_kind="exact",
        )
        key = normalize_key(name)
        if key not in by_key:  # a city of the same name (Delhi, Chandigarh, Goa) wins
            _register(by_key, kinds, key, location, "state", path=source)
        for alias in entry.get("aliases", []) or []:
            alias_key = normalize_key(str(alias))
            if alias_key not in by_key:
                _register(by_key, kinds, alias_key, location, "state", path=source)

    return Gazetteer(
        version=str(data.get("version", "0")),
        country_code=country,
        by_key=by_key,
        match_kinds=kinds,
        address_fragments=frozenset(
            normalize_key(str(t)) for t in data.get("address_fragments", [])
        ),
        non_place_tokens=frozenset(normalize_key(str(t)) for t in data.get("non_place_tokens", [])),
    )


@lru_cache(maxsize=4)
def get_gazetteer(path: Path | None = None) -> Gazetteer:
    return load_gazetteer(path)


_MATCH_CONFIDENCE: Final[dict[str, float]] = {
    "exact": CONFIDENCE_EXACT,
    "alias": CONFIDENCE_ALIAS,
    "locality": CONFIDENCE_LOCALITY,
    "state": CONFIDENCE_STATE,
}


def normalize_city(
    raw: Any, *, field_name: str = "city", gazetteer: Gazetteer | None = None
) -> NormalizationResult[ResolvedLocation]:
    """Resolve a raw ``City`` value, or explain why it could not be resolved."""
    cleaned = clean_scalar(raw)
    if cleaned is None:
        return NormalizationResult.empty()

    original = _WS_RE.sub(" ", cleaned)

    gaz = gazetteer or get_gazetteer()
    key = normalize_key(original)

    if key in gaz.address_fragments:
        return NormalizationResult(
            value=None,
            raw=original,
            confidence=0.0,
            method="gazetteer",
            issues=(
                Issue(
                    field=field_name,
                    code="city_address_fragment",
                    message=(
                        f"{original!r} is an address fragment, not a place; the source address "
                        "was sliced mid-phrase"
                    ),
                    severity=ValidationSeverity.WARNING,
                    value_raw=original,
                ),
            ),
            attributes={"reason": "address_fragment"},
        )

    if key in gaz.non_place_tokens:
        return NormalizationResult(
            value=None,
            raw=original,
            confidence=0.0,
            method="gazetteer",
            issues=(
                Issue(
                    field=field_name,
                    code="city_not_a_place",
                    message=f"{original!r} is not a location",
                    severity=ValidationSeverity.WARNING,
                    value_raw=original,
                ),
            ),
            attributes={"reason": "non_place_token"},
        )

    hit = gaz.lookup(key)
    if hit is None:
        return NormalizationResult(
            value=None,
            raw=original,
            confidence=0.0,
            method="gazetteer",
            issues=(
                Issue(
                    field=field_name,
                    code="city_unresolved",
                    message=f"{original!r} is not in the gazetteer; kept verbatim, unresolved",
                    severity=ValidationSeverity.INFO,
                    value_raw=original,
                ),
            ),
            attributes={"reason": "not_in_gazetteer"},
        )

    location, match_kind = hit
    confidence = _MATCH_CONFIDENCE[match_kind]
    issues: list[Issue] = []
    if location.granularity == "state":
        issues.append(
            Issue(
                field=field_name,
                code="city_resolved_to_state_only",
                message=f"{original!r} is a state, so location is coarse-grained",
                severity=ValidationSeverity.INFO,
                value_raw=original,
            )
        )

    return NormalizationResult(
        value=location,
        raw=original,
        confidence=confidence,
        method=f"gazetteer[{match_kind}]",
        issues=tuple(issues),
        attributes={
            "slug": location.slug,
            "granularity": location.granularity,
            "match_kind": match_kind,
            "state": location.state,
        },
    )
