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


class TestVerificationIntegration:
    """The Phase 1b loop end to end: ingest, verify, rescore."""

    def test_verification_is_cached_per_domain_and_rescore_uses_it(
        self, db_session: Session, workbook_path: Path
    ) -> None:
        from sqlalchemy import func

        from app.ingestion.pipeline import rescore
        from app.models.verification import DomainVerificationRecord
        from app.verification.resolver import MxRecord, StaticResolver
        from app.verification.runner import email_domains_with_counts, verify_email_domains
        from app.verification.types import VerificationStatus

        ingest(workbook_path, db_session)

        domains = email_domains_with_counts(db_session)
        assert domains, "expected email domains to verify"
        # gmail.com dominates by construction; the cache's leverage is exactly this skew.
        top_domain, top_count = domains[0]
        assert top_count > 100

        # A stub resolver: every domain gets an MX except one, which does not exist.
        dead_domain = domains[-1][0]
        records: dict[str, list[MxRecord] | None] = {
            domain: [MxRecord(10, "aspmx.l.google.com")] for domain, _ in domains
        }
        records[dead_domain] = None
        resolver = StaticResolver(records)

        first = verify_email_domains(db_session, resolver=resolver, concurrency=8)
        assert first.candidates == len(domains)
        assert first.checked == len(domains)
        assert first.served_from_cache == 0
        assert first.by_status[VerificationStatus.UNREACHABLE.value] == 1
        # One DNS query per domain, not one per address: that is the whole point.
        assert len(resolver.calls) == len(domains)
        assert first.extra["addresses_covered"] > first.candidates

        # Second run: everything is inside its TTL, so nothing is re-queried.
        calls_before = len(resolver.calls)
        second = verify_email_domains(db_session, resolver=resolver, concurrency=8)
        assert second.checked == 0
        assert second.served_from_cache == len(domains)
        assert len(resolver.calls) == calls_before

        stored = db_session.scalar(select(func.count()).select_from(DomainVerificationRecord))
        assert stored == len(domains)

        top_record = db_session.scalar(
            select(DomainVerificationRecord).where(DomainVerificationRecord.domain == top_domain)
        )
        assert top_record is not None
        assert top_record.address_count == top_count
        assert top_record.expires_at > top_record.checked_at

        # Rescoring now has mailbox evidence, so more factors are measurable than at ingest.
        summary = rescore(workbook_path, db_session)
        assert summary.leads_scored == 2351
        assert max(summary.factors_evaluated) >= 10

    def test_force_re_checks_inside_the_ttl(self, db_session: Session, workbook_path: Path) -> None:
        from app.verification.resolver import MxRecord, StaticResolver
        from app.verification.runner import email_domains_with_counts, verify_email_domains

        ingest(workbook_path, db_session)
        domains = [domain for domain, _ in email_domains_with_counts(db_session)][:20]
        resolver = StaticResolver({d: [MxRecord(10, "mx.example.com")] for d in domains})

        verify_email_domains(db_session, resolver=resolver, concurrency=4, limit=20)
        checked_once = len(resolver.calls)
        assert checked_once == 20

        verify_email_domains(db_session, resolver=resolver, concurrency=4, limit=20, force=True)
        assert len(resolver.calls) > checked_once
