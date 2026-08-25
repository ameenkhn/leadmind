"""The reconciliation report.

Every source row must be accounted for. This structure is what proves it: rows read, rows
merged, leads written, and a full breakdown of issues by code. If ``rows_read`` does not equal
``leads_written + rows_merged_into_existing``, something was lost and the run should not be
trusted — which is exactly the assertion the golden test makes.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class IngestReport:
    source_file: str = ""
    source_sha256: str = ""
    dry_run: bool = False

    rows_read: int = 0
    rows_per_sheet: dict[str, int] = field(default_factory=dict)
    rows_merged: int = 0
    """Source rows folded into another row by an exact identity match."""

    leads_total: int = 0
    leads_created: int = 0
    leads_updated: int = 0

    companies_total: int = 0
    companies_multi_branch: int = 0

    identifiers_written: int = 0
    metric_observations_written: int = 0
    source_queries_written: int = 0
    eval_labels_written: int = 0

    duplicate_candidates: dict[str, int] = field(default_factory=dict)
    issues_by_code: Counter[str] = field(default_factory=Counter)
    issues_by_severity: Counter[str] = field(default_factory=Counter)

    quality_mean: float = 0.0
    quality_median: float = 0.0
    quality_histogram: dict[str, int] = field(default_factory=dict)

    duration_seconds: float = 0.0

    @property
    def reconciles(self) -> bool:
        """Every row read is either a lead or was merged into one."""
        return self.rows_read == self.leads_total + self.rows_merged

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "source_sha256": self.source_sha256,
            "dry_run": self.dry_run,
            "rows_read": self.rows_read,
            "rows_per_sheet": self.rows_per_sheet,
            "rows_merged": self.rows_merged,
            "leads_total": self.leads_total,
            "leads_created": self.leads_created,
            "leads_updated": self.leads_updated,
            "companies_total": self.companies_total,
            "companies_multi_branch": self.companies_multi_branch,
            "identifiers_written": self.identifiers_written,
            "metric_observations_written": self.metric_observations_written,
            "source_queries_written": self.source_queries_written,
            "eval_labels_written": self.eval_labels_written,
            "duplicate_candidates": self.duplicate_candidates,
            "issues_by_code": dict(self.issues_by_code.most_common()),
            "issues_by_severity": dict(self.issues_by_severity),
            "quality_mean": self.quality_mean,
            "quality_median": self.quality_median,
            "quality_histogram": self.quality_histogram,
            "reconciles": self.reconciles,
            "duration_seconds": round(self.duration_seconds, 2),
        }
