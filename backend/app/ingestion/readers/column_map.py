"""Per-sheet column maps.

``Outbound_Leads.xlsx`` is not one table. The three sheets carry 17, 13 and 13 columns and they
are not the same 13. Worse, one column name means two different things:

* ``Day_1.Niche`` holds five curated business verticals
  (Health & Wellness, Life Coaching, Finance, Occult Healing, Disease Reversal).
* ``Day_3.Niche`` holds 131 Facebook page categories (Education website, Nutritionist,
  Yoga studio, …), 74 of which appear verbatim in ``Day_1.FB_Category``.

Mapping both to the same canonical field on the strength of a shared header would corrupt every
vertical feature downstream. Hence: explicit maps per sheet, and an unknown column is an error
rather than a shrug.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

# Canonical field names used by the rest of the pipeline.
F_SERIAL: Final = "serial"
F_NAME: Final = "name"
F_EMAIL: Final = "email"
F_PHONE: Final = "phone"
F_WHATSAPP: Final = "whatsapp"
F_WEBSITE: Final = "website"
F_FACEBOOK: Final = "facebook"
F_INSTAGRAM: Final = "instagram"
F_YOUTUBE: Final = "youtube"
F_LINKEDIN: Final = "linkedin"
F_FB_CATEGORY: Final = "fb_category"
F_NICHE: Final = "niche"
F_CITY: Final = "city"
F_FOLLOWERS: Final = "followers"
F_SOURCE: Final = "source"
F_MATCHED_QUERY: Final = "matched_query"
F_RELEVANCE: Final = "relevance"


@dataclass(frozen=True, slots=True)
class SheetSpec:
    """How one worksheet maps onto canonical fields."""

    sheet: str
    columns: MappingProxyType[str, str]
    sequence: int
    """Scrape order. The workbook carries no dates; this preserves relative ordering."""

    defaults: MappingProxyType[str, str] = field(default_factory=lambda: MappingProxyType({}))
    inferred_fields: frozenset[str] = frozenset()
    """Fields supplied by :attr:`defaults` rather than observed in the sheet."""

    @property
    def expected_headers(self) -> frozenset[str]:
        return frozenset(self.columns)


_DAY_1 = SheetSpec(
    sheet="Day_1",
    sequence=1,
    columns=MappingProxyType(
        {
            "S.No": F_SERIAL,
            "Niche": F_NICHE,  # curated vertical — genuinely a niche
            "Relevance": F_RELEVANCE,
            "Name": F_NAME,
            "Email": F_EMAIL,
            "Phone": F_PHONE,
            "WhatsApp": F_WHATSAPP,
            "Website": F_WEBSITE,
            "Facebook": F_FACEBOOK,
            "Instagram": F_INSTAGRAM,
            "YouTube": F_YOUTUBE,
            "LinkedIn": F_LINKEDIN,
            "FB_Category": F_FB_CATEGORY,
            "City": F_CITY,
            "Followers": F_FOLLOWERS,
            "Source": F_SOURCE,
            "Matched_Query": F_MATCHED_QUERY,
        }
    ),
)

_DAY_2 = SheetSpec(
    sheet="Day_2",
    sequence=2,
    columns=MappingProxyType(
        {
            "S.No.": F_SERIAL,
            "Name": F_NAME,
            "Email": F_EMAIL,
            "Phone": F_PHONE,
            "FB_Category": F_FB_CATEGORY,
            "Website": F_WEBSITE,
            "Facebook": F_FACEBOOK,
            "Instagram": F_INSTAGRAM,
            "YouTube": F_YOUTUBE,
            "LinkedIn": F_LINKEDIN,
            "City": F_CITY,
            "Followers": F_FOLLOWERS,
            "Source": F_SOURCE,
        }
    ),
)

_DAY_3 = SheetSpec(
    sheet="Day_3",
    sequence=3,
    columns=MappingProxyType(
        {
            "S.No.": F_SERIAL,
            "Name": F_NAME,
            # NOT a niche. 74 of its 131 values are verbatim Day_1 FB_Category values.
            "Niche": F_FB_CATEGORY,
            "Email": F_EMAIL,
            "Phone": F_PHONE,
            "Website": F_WEBSITE,
            "Facebook": F_FACEBOOK,
            "Instagram": F_INSTAGRAM,
            "YouTube": F_YOUTUBE,
            "LinkedIn": F_LINKEDIN,
            "City": F_CITY,
            "Followers": F_FOLLOWERS,
            "Matched_Query": F_MATCHED_QUERY,
        }
    ),
    # Day_3 has no Source column. Every other sheet is "Meta Ad Library" and the rows are
    # structurally identical, so the value is filled in — and flagged as inferred, never
    # presented as observed.
    defaults=MappingProxyType({F_SOURCE: "Meta Ad Library"}),
    inferred_fields=frozenset({F_SOURCE}),
)

SHEET_SPECS: Final[MappingProxyType[str, SheetSpec]] = MappingProxyType(
    {spec.sheet: spec for spec in (_DAY_1, _DAY_2, _DAY_3)}
)

ALL_CANONICAL_FIELDS: Final[frozenset[str]] = frozenset(
    {
        F_SERIAL,
        F_NAME,
        F_EMAIL,
        F_PHONE,
        F_WHATSAPP,
        F_WEBSITE,
        F_FACEBOOK,
        F_INSTAGRAM,
        F_YOUTUBE,
        F_LINKEDIN,
        F_FB_CATEGORY,
        F_NICHE,
        F_CITY,
        F_FOLLOWERS,
        F_SOURCE,
        F_MATCHED_QUERY,
        F_RELEVANCE,
    }
)
