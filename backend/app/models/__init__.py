"""SQLAlchemy models. Importing this package registers every table on ``Base.metadata``."""

from __future__ import annotations

from app.db.base import Base
from app.models.enums import (
    DuplicateMethod,
    DuplicateStatus,
    EntityKind,
    IdentifierKind,
    IngestStatus,
    LabelSource,
    MetricKind,
    ValidationSeverity,
)
from app.models.lead import (
    Company,
    Lead,
    LeadIdentifier,
    LeadSourceQuery,
    MetricObservation,
)
from app.models.provenance import IngestRun, LeadSourceRecord, ValidationIssue
from app.models.quality import DataQualityScore, DuplicateCandidate, EvalLabel
from app.models.taxonomy import Category, CategoryAlias, Location, LocationAlias

__all__ = [
    "Base",
    "Category",
    "CategoryAlias",
    "Company",
    "DataQualityScore",
    "DuplicateCandidate",
    "DuplicateMethod",
    "DuplicateStatus",
    "EntityKind",
    "EvalLabel",
    "IdentifierKind",
    "IngestRun",
    "IngestStatus",
    "LabelSource",
    "Lead",
    "LeadIdentifier",
    "LeadSourceQuery",
    "LeadSourceRecord",
    "Location",
    "LocationAlias",
    "MetricKind",
    "MetricObservation",
    "ValidationIssue",
    "ValidationSeverity",
]
