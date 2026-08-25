"""Domain enumerations.

These are native PostgreSQL enums. The trade-off: adding a value needs a migration, which is
noisier than a free-text column but makes an invalid value impossible to write. For a pipeline
whose entire value proposition is trustworthy data, that trade is worth making.
"""

from __future__ import annotations

import enum


class IdentifierKind(enum.StrEnum):
    """Channels a lead can be reached or identified by.

    Modelled as rows in ``lead_identifiers`` rather than eight sparse columns on ``leads``:
    the source data fills between 12% and 100% of them, dedup needs to query across all of
    them, and a ninth channel should be a row, not a migration.
    """

    EMAIL = "email"
    PHONE = "phone"
    WHATSAPP = "whatsapp"
    WEBSITE = "website"
    FACEBOOK = "facebook"
    INSTAGRAM = "instagram"
    YOUTUBE = "youtube"
    LINKEDIN = "linkedin"


class EntityKind(enum.StrEnum):
    """Whether a lead's name denotes a person, a business, or cannot be told apart."""

    PERSON = "person"
    BUSINESS = "business"
    UNKNOWN = "unknown"


class MetricKind(enum.StrEnum):
    """Metrics captured as dated observations rather than overwritten columns."""

    FOLLOWERS = "followers"


class ValidationSeverity(enum.StrEnum):
    """How badly a validation failure damages the record.

    ``ERROR`` never means "drop the row" — it means the field is unusable and the reason is
    recorded. Silently discarding source rows is how a pipeline stops being auditable.
    """

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class DuplicateMethod(enum.StrEnum):
    """How two records were found to be potentially the same lead.

    The first three are exact and safe to auto-merge. The last two are *not*: a shared website
    is routinely a franchise relationship (five Pumo Technovation branches share one corporate
    domain and are five real prospects), and a high name similarity is a hypothesis.
    """

    EXACT_EMAIL = "exact_email"
    EXACT_PHONE = "exact_phone"
    EXACT_FACEBOOK = "exact_facebook"
    SHARED_WEBSITE = "shared_website"
    FUZZY_NAME = "fuzzy_name"

    @property
    def is_auto_mergeable(self) -> bool:
        return self in _AUTO_MERGE_METHODS


_AUTO_MERGE_METHODS = frozenset(
    {
        DuplicateMethod.EXACT_EMAIL,
        DuplicateMethod.EXACT_PHONE,
        DuplicateMethod.EXACT_FACEBOOK,
    }
)


class DuplicateStatus(enum.StrEnum):
    PENDING = "pending"
    CONFIRMED_DUPLICATE = "confirmed_duplicate"
    REJECTED_DISTINCT = "rejected_distinct"


class LabelSource(enum.StrEnum):
    """Provenance of an evaluation label.

    ``WEAK_RELEVANCE`` is the ``Relevance`` column that ships with sheet Day_1 (High 573 /
    Medium 290 / Low 37). Its provenance is unknown, so it seeds the evaluation set but must
    never be mixed with hand-verified gold labels when reporting metrics.
    """

    WEAK_RELEVANCE = "weak_relevance"
    HUMAN_GOLD = "human_gold"


class IngestStatus(enum.StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DRY_RUN = "dry_run"
