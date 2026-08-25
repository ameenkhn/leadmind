"""Ingest tests that need their own transaction.

Kept in a separate module from the golden tests on purpose. Those hold one long-lived
uncommitted transaction for the whole module; a second connection writing the same unique keys
would block on it until the transaction ended. Splitting the files lets pytest tear the shared
transaction down before these run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ingestion.pipeline import ingest
from app.models import (
    Company,
    DataQualityScore,
    DuplicateCandidate,
    EvalLabel,
    IngestRun,
    Lead,
    LeadIdentifier,
    LeadSourceQuery,
    LeadSourceRecord,
    MetricObservation,
    ValidationIssue,
)
from app.models.enums import IngestStatus

pytestmark = pytest.mark.integration

COUNTED_MODELS = (
    Lead,
    Company,
    LeadIdentifier,
    MetricObservation,
    LeadSourceQuery,
    LeadSourceRecord,
    DataQualityScore,
    DuplicateCandidate,
    EvalLabel,
    ValidationIssue,
)


def _snapshot(session: Session) -> dict[str, int]:
    return {
        model.__name__: int(session.scalar(select(func.count()).select_from(model)) or 0)
        for model in COUNTED_MODELS
    }


class TestIdempotency:
    def test_second_run_changes_nothing(self, db_session: Session, workbook_path: Path) -> None:
        first = ingest(workbook_path, db_session)
        after_first = _snapshot(db_session)

        second = ingest(workbook_path, db_session)
        after_second = _snapshot(db_session)

        assert first.leads_created == 2351
        assert second.leads_created == 0
        assert second.leads_updated == 2351
        assert after_first == after_second

    def test_both_runs_are_recorded(self, db_session: Session, workbook_path: Path) -> None:
        ingest(workbook_path, db_session)
        ingest(workbook_path, db_session)
        runs = db_session.scalars(select(IngestRun).order_by(IngestRun.started_at)).all()
        assert len(runs) == 2
        assert all(run.status is IngestStatus.SUCCEEDED for run in runs)
        assert runs[0].source_sha256 == runs[1].source_sha256
        assert runs[0].run_id != runs[1].run_id


class TestDryRun:
    def test_dry_run_writes_nothing(self, db_session: Session, workbook_path: Path) -> None:
        before = _snapshot(db_session)
        report = ingest(workbook_path, dry_run=True)
        assert report.leads_total == 2351
        assert report.leads_created == 0
        assert report.quality_mean > 0
        assert _snapshot(db_session) == before
