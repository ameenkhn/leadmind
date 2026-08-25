"""Integration tests against a live PostgreSQL database.

These run the real pipeline over the real 2 520-row workbook inside transactions that are rolled
back afterwards, so they prove the persistence layer works without leaving a database behind.
The golden tests are the ones that matter: they assert the reconciliation identity that makes
every later phase trustworthy.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.ingestion.pipeline import ingest
from app.ingestion.quality.rubric import get_rubric
from app.ingestion.report import IngestReport
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
from app.models.enums import (
    DuplicateMethod,
    DuplicateStatus,
    IdentifierKind,
    IngestStatus,
    LabelSource,
)

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


def _count(session: Session, model: type) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _snapshot(session: Session) -> dict[str, int]:
    return {model.__name__: _count(session, model) for model in COUNTED_MODELS}


@pytest.fixture(scope="module")
def ingested(engine: Engine, workbook_path: Path) -> Iterator[tuple[Session, IngestReport]]:
    """One full ingest, shared read-only by every test in this module, rolled back at the end."""
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, expire_on_commit=False)
    try:
        report = ingest(workbook_path, session)
        yield session, report
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.mark.golden
class TestGoldenIngest:
    def test_full_dataset_reconciles(self, ingested: tuple[Session, IngestReport]) -> None:
        session, report = ingested
        assert report.rows_read == 2520
        assert report.rows_per_sheet == {"Day_1": 900, "Day_2": 1000, "Day_3": 620}
        assert report.rows_merged == 169
        assert report.leads_total == 2351
        assert report.reconciles, "every source row must be a lead or merged into one"

        assert _count(session, Lead) == 2351
        assert _count(session, LeadSourceRecord) == 2520
        assert _count(session, DataQualityScore) == 2351

    def test_provenance_is_total(self, ingested: tuple[Session, IngestReport]) -> None:
        """No orphan source rows, and every lead traces back to at least one."""
        session, _ = ingested
        orphans = session.scalar(
            select(func.count())
            .select_from(LeadSourceRecord)
            .where(LeadSourceRecord.lead_id.is_(None))
        )
        assert orphans == 0
        distinct = session.scalar(select(func.count(func.distinct(LeadSourceRecord.lead_id))))
        assert distinct == 2351

    def test_franchise_survives_persistence(self, ingested: tuple[Session, IngestReport]) -> None:
        session, _ = ingested
        company = session.scalar(
            select(Company).where(Company.primary_domain == "pumotechnovation.com")
        )
        assert company is not None
        assert len(company.leads) == 5

    def test_follower_history_is_preserved(self, ingested: tuple[Session, IngestReport]) -> None:
        """Leads seen twice keep both observations, including where the values disagree."""
        session, _ = ingested
        repeated = session.scalars(
            select(MetricObservation.lead_id)
            .group_by(MetricObservation.lead_id)
            .having(func.count() > 1)
        ).all()
        assert len(repeated) == 168

        differing = session.scalars(
            select(MetricObservation.lead_id)
            .group_by(MetricObservation.lead_id)
            .having(func.count(func.distinct(MetricObservation.value)) > 1)
        ).all()
        assert len(differing) == 63

    def test_observation_timestamps_are_not_fabricated(
        self, ingested: tuple[Session, IngestReport]
    ) -> None:
        """The workbook has no scrape dates, so observed_at stays NULL rather than invented."""
        session, _ = ingested
        dated = session.scalar(
            select(func.count())
            .select_from(MetricObservation)
            .where(MetricObservation.observed_at.isnot(None))
        )
        assert dated == 0

    def test_matched_queries_are_a_child_table(
        self, ingested: tuple[Session, IngestReport]
    ) -> None:
        """Because the same lead is found by different queries on different days."""
        session, _ = ingested
        multi = session.scalars(
            select(LeadSourceQuery.lead_id)
            .group_by(LeadSourceQuery.lead_id)
            .having(func.count() > 1)
        ).all()
        assert multi


class TestPersistedSemantics:
    def test_duplicate_candidates_are_pending_and_never_auto_merged(
        self, ingested: tuple[Session, IngestReport]
    ) -> None:
        session, _ = ingested
        candidates = session.scalars(select(DuplicateCandidate)).all()
        assert candidates
        assert all(c.status is DuplicateStatus.PENDING for c in candidates)
        assert all(not c.method.is_auto_mergeable for c in candidates)
        assert all(c.lead_a_id != c.lead_b_id for c in candidates)
        assert {c.method for c in candidates} <= {
            DuplicateMethod.SHARED_WEBSITE,
            DuplicateMethod.FUZZY_NAME,
        }

    def test_relevance_is_stored_as_a_weak_label(
        self, ingested: tuple[Session, IngestReport]
    ) -> None:
        """900 shipped labels seed the eval set, tagged so they can never pass as gold."""
        session, _ = ingested
        labels = session.scalars(select(EvalLabel)).all()
        assert len(labels) == 900
        assert all(label.label_source is LabelSource.WEAK_RELEVANCE for label in labels)
        assert {label.label for label in labels} == {"High", "Medium", "Low"}

        gold = session.scalar(
            select(func.count())
            .select_from(EvalLabel)
            .where(EvalLabel.label_source == LabelSource.HUMAN_GOLD)
        )
        assert gold == 0

    def test_identifiers_carry_their_classification(
        self, ingested: tuple[Session, IngestReport]
    ) -> None:
        session, _ = ingested
        identifier = session.scalar(
            select(LeadIdentifier).where(LeadIdentifier.kind == IdentifierKind.EMAIL).limit(1)
        )
        assert identifier is not None
        assert "is_freemail" in identifier.attributes
        assert identifier.attributes["deliverability"] == "unverified"

    def test_validation_issues_are_persisted_not_discarded(
        self, ingested: tuple[Session, IngestReport]
    ) -> None:
        session, _ = ingested
        assert _count(session, ValidationIssue) > 0
        codes = set(session.scalars(select(ValidationIssue.code).distinct()).all())
        assert {"thin_record", "city_address_fragment"} <= codes

    def test_quality_scores_store_their_reasons(
        self, ingested: tuple[Session, IngestReport]
    ) -> None:
        session, _ = ingested
        score = session.scalar(select(DataQualityScore).limit(1))
        assert score is not None
        assert 0 <= score.score <= 100
        assert score.rubric_version
        assert all("reason" in detail for detail in score.factors["factors"].values())

    def test_run_is_recorded_with_its_versions(
        self, ingested: tuple[Session, IngestReport]
    ) -> None:
        session, _ = ingested
        run = session.scalar(select(IngestRun))
        assert run is not None
        assert run.status is IngestStatus.SUCCEEDED
        # Compared against the loaded rubric rather than a literal, so a version bump does not
        # break the test that exists to prove versions are recorded at all.
        assert run.rubric_version == get_rubric().version
        assert run.stats["reconciles"] is True
