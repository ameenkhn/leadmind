"""Vertical classification from Facebook page categories.

Maps 275 long-tailed source labels onto 15 controlled verticals via an explicit alias table.
No fuzzy matching, no LLM: the mapping is a business decision that should be reviewable in a
diff, not re-derived probabilistically on every run. Anything unmapped becomes ``unclassified``,
which is a real answer that lowers confidence rather than a silent default.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import yaml

from app.core.config import get_settings
from app.core.errors import ConfigurationError
from app.ingestion.normalizers.city import normalize_key
from app.ingestion.normalizers.result import Issue, NormalizationResult, clean_scalar
from app.models.enums import ValidationSeverity

UNCLASSIFIED: Final = "unclassified"


@dataclass(frozen=True, slots=True)
class Vertical:
    slug: str
    label: str


@dataclass(frozen=True, slots=True)
class Taxonomy:
    version: str
    verticals: dict[str, Vertical]
    alias_to_slug: dict[str, str]

    def resolve(self, key: str) -> str | None:
        return self.alias_to_slug.get(key)

    @property
    def alias_count(self) -> int:
        return len(self.alias_to_slug)


def load_taxonomy(path: Path | None = None) -> Taxonomy:
    source = path or get_settings().config_file("taxonomy.yaml")
    data: dict[str, Any] = yaml.safe_load(source.read_text(encoding="utf-8"))

    verticals = {
        str(entry["slug"]): Vertical(slug=str(entry["slug"]), label=str(entry["label"]))
        for entry in data.get("verticals", [])
    }
    if UNCLASSIFIED not in verticals:
        raise ConfigurationError(
            f"taxonomy must define a {UNCLASSIFIED!r} vertical", file=str(source)
        )

    alias_to_slug: dict[str, str] = {}
    for slug, aliases in (data.get("aliases") or {}).items():
        if slug not in verticals:
            raise ConfigurationError(
                "alias group references an undeclared vertical", slug=slug, file=str(source)
            )
        for alias in aliases or []:
            key = normalize_key(str(alias))
            existing = alias_to_slug.get(key)
            if existing is not None and existing != slug:
                raise ConfigurationError(
                    "alias mapped to two verticals",
                    alias=alias,
                    first=existing,
                    second=slug,
                    file=str(source),
                )
            alias_to_slug[key] = slug
        # The vertical's own label and slug always resolve to itself.
        alias_to_slug.setdefault(normalize_key(verticals[slug].label), slug)
        alias_to_slug.setdefault(normalize_key(slug.replace("_", " ")), slug)

    return Taxonomy(
        version=str(data.get("version", "0")),
        verticals=verticals,
        alias_to_slug=alias_to_slug,
    )


@lru_cache(maxsize=4)
def get_taxonomy(path: Path | None = None) -> Taxonomy:
    return load_taxonomy(path)


def normalize_category(
    fb_category: Any,
    niche: Any = None,
    *,
    field_name: str = "fb_category",
    taxonomy: Taxonomy | None = None,
) -> NormalizationResult[str]:
    """Resolve a vertical slug from the page category, falling back to the curated niche.

    ``FB_Category`` is preferred because it is present on 90.4% of rows against the curated
    ``Niche``'s 36%; the niche is the tie-breaker when the page category is missing or unmapped.
    """
    tax = taxonomy or get_taxonomy()

    fb_raw = clean_scalar(fb_category)
    niche_raw = clean_scalar(niche)

    if fb_raw is None and niche_raw is None:
        return NormalizationResult.empty()

    for source_name, raw_value, confidence in (
        ("fb_category", fb_raw, 0.9),
        ("niche", niche_raw, 0.8),
    ):
        if raw_value is None:
            continue
        slug = tax.resolve(normalize_key(raw_value))
        if slug is not None and slug != UNCLASSIFIED:
            return NormalizationResult(
                value=slug,
                raw=raw_value,
                confidence=confidence,
                method=f"alias_map[{source_name}]",
                attributes={
                    "label": tax.verticals[slug].label,
                    "matched_on": source_name,
                    "fb_category_raw": fb_raw,
                    "niche_raw": niche_raw,
                },
            )

    known_but_generic = any(
        raw is not None and tax.resolve(normalize_key(raw)) == UNCLASSIFIED
        for raw in (fb_raw, niche_raw)
    )
    code = "category_generic" if known_but_generic else "category_unmapped"
    message = (
        "source category is too generic to place in a vertical"
        if known_but_generic
        else "source category is not present in the alias map"
    )

    return NormalizationResult(
        value=UNCLASSIFIED,
        raw=fb_raw or niche_raw,
        confidence=0.0,
        method="alias_map[miss]",
        issues=(
            Issue(
                field=field_name,
                code=code,
                message=message,
                severity=ValidationSeverity.INFO,
                value_raw=fb_raw or niche_raw,
            ),
        ),
        attributes={
            "label": tax.verticals[UNCLASSIFIED].label,
            "matched_on": None,
            "fb_category_raw": fb_raw,
            "niche_raw": niche_raw,
        },
    )
