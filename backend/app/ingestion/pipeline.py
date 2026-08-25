"""The ingest pipeline.

Read → normalise → validate → deduplicate → merge → resolve companies → score → persist.

Two properties this module exists to guarantee:

**Idempotency.** Running the same workbook twice produces the same database, not two copies of
it. Leads are matched to existing rows through their exact identity identifiers (email, phone,
Facebook URL) rather than through insertion order, so a re-run updates in place. Every child
table has a natural uniqueness key and is upserted against it.

**Reconciliation.** Every source row ends up either as a lead or as a row merged into one, and
the report says which. A pipeline that cannot account for its input has no business being
trusted with a scoring model on top of it.
"""

from __future__ import annotations

import datetime as dt
import statistics
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.errors import IngestionError
from app.core.logging import get_logger, run_context
from app.ingestion.dedup.cluster import CandidatePair, deduplicate
from app.ingestion.normalizers.record import NormalizedRecord, normalize_record
from app.ingestion.quality.rubric import get_rubric, score_lead
from app.ingestion.readers.excel import ExcelLeadReader, SourceRow
from app.ingestion.report import IngestReport
from app.ingestion.resolution.company import ResolvedCompany, resolve_companies
from app.ingestion.resolution.merge import MergedLead, merge_cluster
from app.ingestion.seed import seed_categories, seed_locations
from app.ingestion.validators.rules import validate_record
from app.models import (
    Category,
    Company,
    DataQualityScore,
    DuplicateCandidate,
    EvalLabel,
    IngestRun,
    Lead,
    LeadIdentifier,
    LeadSourceQuery,
    LeadSourceRecord,
    Location,
    MetricObservation,
    ValidationIssue,
)
from app.models.enums import (
    DuplicateStatus,
    IdentifierKind,
    IngestStatus,
    LabelSource,
    MetricKind,
)

logger = get_logger(__name__)

IDENTITY_KINDS: tuple[IdentifierKind, ...] = (
    IdentifierKind.EMAIL,
    IdentifierKind.PHONE,
    IdentifierKind.FACEBOOK,
)

RELEVANCE_DIMENSION = "lead_relevance"


def _code_version() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


@dataclass(slots=True)
class _Prepared:
    """Everything computed in memory, before a single row is written."""

    rows: list[SourceRow]
    records: list[NormalizedRecord]
    leads: list[MergedLead]
    candidates: list[CandidatePair]
    rows_merged: int
    source_file: str
    source_sha256: str


def prepare(path: Path) -> _Prepared:
    """Run the whole in-memory half of the pipeline. Touches no database."""
    reader = ExcelLeadReader(path)
    rows = list(reader.iter_rows())

    records: list[NormalizedRecord] = []
    for row in rows:
        record = normalize_record(row)
        validate_record(record)
        records.append(record)

    dedup = deduplicate(records)
    leads = [merge_cluster(cluster, records) for cluster in dedup.clusters]

    return _Prepared(
        rows=rows,
        records=records,
        leads=leads,
        candidates=dedup.candidates,
        rows_merged=dedup.merged_row_count,
        source_file=str(path),
        source_sha256=reader.source_sha256,
    )


def _existing_lead_ids(session: Session, leads: list[MergedLead]) -> dict[int, list[Lead]]:
    """Map each merged lead to the already-stored leads sharing one of its identity keys."""
    wanted: set[tuple[IdentifierKind, str]] = set()
    for lead in leads:
        for identifier in lead.identifiers:
            if identifier.kind in IDENTITY_KINDS:
                wanted.add((identifier.kind, identifier.value))
    if not wanted:
        return {}

    stored = session.scalars(
        select(LeadIdentifier).where(LeadIdentifier.kind.in_(IDENTITY_KINDS))
    ).all()
    by_key: dict[tuple[IdentifierKind, str], list[LeadIdentifier]] = {}
    for stored_identifier in stored:
        by_key.setdefault(
            (stored_identifier.kind, stored_identifier.value_normalized), []
        ).append(stored_identifier)

    matches: dict[int, list[Lead]] = {}
    for index, lead in enumerate(leads):
        found: dict[str, Lead] = {}
        for identifier in lead.identifiers:
            if identifier.kind not in IDENTITY_KINDS:
                continue
            for match in by_key.get((identifier.kind, identifier.value), []):
                found[str(match.lead_id)] = match.lead
        if found:
            matches[index] = list(found.values())
    return matches


def _upsert_company(
    session: Session, domain: str, name: str | None, website: str | None
) -> Company:
    company = session.scalar(select(Company).where(Company.primary_domain == domain))
    if company is None:
        company = Company(primary_domain=domain, name=name, website_url=website)
        session.add(company)
        session.flush()
    else:
        company.name = name or company.name
        company.website_url = website or company.website_url
    return company


def _sync_identifiers(session: Session, lead: Lead, merged: MergedLead) -> int:
    existing = {(i.kind, i.value_normalized): i for i in lead.identifiers}
    written = 0
    for identifier in merged.identifiers:
        key = (identifier.kind, identifier.value)
        current = existing.get(key)
        if current is None:
            session.add(
                LeadIdentifier(
                    lead_id=lead.id,
                    kind=identifier.kind,
                    value_raw=identifier.raw,
                    value_normalized=identifier.value,
                    is_primary=identifier.is_primary,
                    confidence=identifier.confidence,
                    attributes=identifier.attributes,
                )
            )
            written += 1
        else:
            current.value_raw = identifier.raw
            current.is_primary = identifier.is_primary
            current.confidence = identifier.confidence
            current.attributes = identifier.attributes
    return written


def _sync_observations(session: Session, lead: Lead, merged: MergedLead) -> int:
    existing = {(o.metric, o.batch): o for o in lead.metrics}
    written = 0
    for observation in merged.follower_observations:
        key = (MetricKind.FOLLOWERS, observation.source)
        current = existing.get(key)
        if current is None:
            session.add(
                MetricObservation(
                    lead_id=lead.id,
                    metric=MetricKind.FOLLOWERS,
                    value=observation.value,
                    value_raw=observation.raw,
                    batch=observation.source,
                    batch_sequence=observation.sheet_sequence,
                    observed_at=observation.observed_at,
                    source=observation.source,
                )
            )
            written += 1
        else:
            current.value = observation.value
            current.value_raw = observation.raw
            current.observed_at = observation.observed_at
    return written


def _sync_queries(session: Session, lead: Lead, merged: MergedLead) -> int:
    existing = {(q.query, q.source) for q in lead.source_queries}
    written = 0
    for query in merged.source_queries:
        if (query.query, query.source) in existing:
            continue
        session.add(
            LeadSourceQuery(
                lead_id=lead.id,
                query=query.query,
                source=query.source,
                source_is_inferred=query.source_is_inferred,
            )
        )
        written += 1
    return written


def _sync_eval_labels(session: Session, lead: Lead, merged: MergedLead) -> int:
    """Store the shipped ``Relevance`` column as a *weak* label.

    Its provenance is unknown, so it seeds the evaluation set without ever being mistaken for
    ground truth. ``label_source`` is what keeps weak and gold labels from being averaged.
    """
    if not merged.relevance_labels:
        return 0
    label = merged.relevance_labels[0]
    existing = session.scalar(
        select(EvalLabel).where(
            EvalLabel.lead_id == lead.id,
            EvalLabel.dimension == RELEVANCE_DIMENSION,
            EvalLabel.label_source == LabelSource.WEAK_RELEVANCE,
        )
    )
    if existing is not None:
        existing.label = label
        return 0
    session.add(
        EvalLabel(
            lead_id=lead.id,
            dimension=RELEVANCE_DIMENSION,
            label=label,
            label_source=LabelSource.WEAK_RELEVANCE,
            labeller="source_workbook",
            notes="Day_1 Relevance column; provenance unknown, treat as weak supervision",
        )
    )
    return 1


def _sync_issues(session: Session, lead: Lead, merged: MergedLead) -> None:
    session.execute(delete(ValidationIssue).where(ValidationIssue.lead_id == lead.id))
    seen: set[tuple[str, str, str | None]] = set()
    for issue in merged.issues:
        key = (issue.field, issue.code, issue.value_raw)
        if key in seen:
            continue
        seen.add(key)
        session.add(
            ValidationIssue(
                lead_id=lead.id,
                field=issue.field,
                code=issue.code,
                severity=issue.severity,
                message=issue.message,
                value_raw=issue.value_raw,
            )
        )


def _sync_quality(session: Session, lead: Lead, merged: MergedLead) -> float:
    rubric = get_rubric()
    result = score_lead(merged, rubric=rubric)
    existing = session.scalar(
        select(DataQualityScore).where(
            DataQualityScore.lead_id == lead.id,
            DataQualityScore.rubric_version == result.rubric_version,
        )
    )
    now = dt.datetime.now(dt.UTC)
    if existing is None:
        session.add(
            DataQualityScore(
                lead_id=lead.id,
                score=result.score,
                rubric_version=result.rubric_version,
                factors=result.factors,
                computed_at=now,
            )
        )
    else:
        existing.score = result.score
        existing.factors = result.factors
        existing.computed_at = now
    return result.score


def ingest(
    path: Path,
    session: Session | None = None,
    *,
    dry_run: bool = False,
    run_id: str | None = None,
) -> IngestReport:
    """Ingest a workbook and return a reconciliation report.

    ``session`` is required unless ``dry_run`` is set: a dry run does the entire computation,
    including quality scoring, without opening a transaction, which makes it usable as a
    pre-flight check against a workbook before any database exists.
    """
    if session is None and not dry_run:
        raise IngestionError("a database session is required unless dry_run is set")
    started = dt.datetime.now(dt.UTC)
    report = IngestReport(dry_run=dry_run)

    with run_context(run_id=run_id, stage="ingest") as active_run_id:
        prepared = prepare(path)
        report.source_file = prepared.source_file
        report.source_sha256 = prepared.source_sha256
        report.rows_read = len(prepared.rows)
        report.rows_merged = prepared.rows_merged
        report.leads_total = len(prepared.leads)
        report.rows_per_sheet = dict(Counter(row.sheet for row in prepared.rows))

        for record in prepared.records:
            for issue in record.issues:
                report.issues_by_code[issue.code] += 1
                report.issues_by_severity[issue.severity.value] += 1

        companies = resolve_companies(prepared.leads)
        report.companies_total = len(companies)
        report.companies_multi_branch = sum(1 for c in companies if c.is_multi_branch)
        report.duplicate_candidates = dict(
            Counter(candidate.method.value for candidate in prepared.candidates)
        )

        scores: list[float] = []

        if dry_run:
            rubric = get_rubric()
            scores = [score_lead(lead, rubric=rubric).score for lead in prepared.leads]
            logger.info("ingest_dry_run_complete", leads=len(prepared.leads))
        else:
            assert session is not None
            scores = _persist(session, prepared, companies, report, active_run_id, started)

        if scores:
            report.quality_mean = round(statistics.mean(scores), 2)
            report.quality_median = round(statistics.median(scores), 2)
            buckets = Counter(min(int(score // 10) * 10, 90) for score in scores)
            report.quality_histogram = {
                f"{bucket}-{bucket + 9}": buckets[bucket] for bucket in sorted(buckets)
            }

    report.duration_seconds = (dt.datetime.now(dt.UTC) - started).total_seconds()
    logger.info("ingest_complete", **report.as_dict())
    return report


def _persist(
    session: Session,
    prepared: _Prepared,
    companies: list[ResolvedCompany],
    report: IngestReport,
    run_id: str,
    started: dt.datetime,
) -> list[float]:
    seed_categories(session)
    seed_locations(session)
    category_ids = {c.slug: c.id for c in session.scalars(select(Category)).all()}
    location_ids = {loc.slug: loc.id for loc in session.scalars(select(Location)).all()}

    ingest_run = IngestRun(
        run_id=run_id,
        source_file=prepared.source_file,
        source_sha256=prepared.source_sha256,
        status=IngestStatus.RUNNING,
        started_at=started,
        code_version=_code_version(),
        rubric_version=get_rubric().version,
    )
    session.add(ingest_run)
    session.flush()

    company_by_domain: dict[str, Company] = {}
    for company in companies:
        company_by_domain[company.domain] = _upsert_company(
            session, company.domain, company.name, company.website_url
        )

    matches = _existing_lead_ids(session, prepared.leads)
    scores: list[float] = []
    lead_rows: list[Lead] = []

    for index, merged in enumerate(prepared.leads):
        candidates = matches.get(index, [])
        if len(candidates) > 1:
            logger.warning(
                "lead_matches_multiple_existing_records",
                lead=merged.display_name,
                matched=len(candidates),
            )
        lead = candidates[0] if candidates else None
        created = lead is None
        if lead is None:
            lead = Lead(display_name=merged.display_name, normalized_name=merged.normalized_name)
            session.add(lead)

        lead.display_name = merged.display_name
        lead.normalized_name = merged.normalized_name
        lead.entity_kind = merged.entity_kind
        lead.is_placeholder_name = merged.is_placeholder_name
        lead.location_raw = merged.location_raw
        lead.location_confidence = merged.location_confidence
        lead.location_id = location_ids.get(merged.location.slug) if merged.location else None
        lead.category_id = category_ids.get(merged.vertical_slug or "")
        lead.fb_category_raw = merged.fb_category_raw
        lead.niche_raw = merged.niche_raw
        lead.company_id = (
            company_by_domain[merged.company_domain].id if merged.company_domain else None
        )
        lead.first_seen_at = lead.first_seen_at or started
        lead.last_seen_at = started
        session.flush()

        report.leads_created += int(created)
        report.leads_updated += int(not created)
        report.identifiers_written += _sync_identifiers(session, lead, merged)
        report.metric_observations_written += _sync_observations(session, lead, merged)
        report.source_queries_written += _sync_queries(session, lead, merged)
        report.eval_labels_written += _sync_eval_labels(session, lead, merged)
        _sync_issues(session, lead, merged)
        scores.append(_sync_quality(session, lead, merged))
        lead_rows.append(lead)

    session.flush()
    _persist_source_records(session, prepared, ingest_run, lead_rows)
    _persist_candidates(session, prepared, lead_rows)

    ingest_run.status = IngestStatus.SUCCEEDED
    ingest_run.finished_at = dt.datetime.now(dt.UTC)
    ingest_run.stats = report.as_dict()
    return scores


def _persist_source_records(
    session: Session, prepared: _Prepared, ingest_run: IngestRun, lead_rows: list[Lead]
) -> None:
    lead_of_record: dict[int, Lead] = {}
    for lead_index, merged in enumerate(prepared.leads):
        for record_index in merged.record_indexes:
            lead_of_record[record_index] = lead_rows[lead_index]

    existing = {
        (r.source_sha256, r.source_sheet, r.source_row_no): r
        for r in session.scalars(
            select(LeadSourceRecord).where(
                LeadSourceRecord.source_sha256 == prepared.source_sha256
            )
        ).all()
    }

    for index, row in enumerate(prepared.rows):
        key = (row.source_sha256, row.sheet, row.row_no)
        lead = lead_of_record.get(index)
        current = existing.get(key)
        if current is None:
            session.add(
                LeadSourceRecord(
                    ingest_run_id=ingest_run.id,
                    lead_id=lead.id if lead else None,
                    source_file=row.source_file,
                    source_sha256=row.source_sha256,
                    source_sheet=row.sheet,
                    source_row_no=row.row_no,
                    source_serial=row.serial,
                    row_sha256=row.row_sha256,
                    raw=row.raw,
                )
            )
        else:
            current.ingest_run_id = ingest_run.id
            current.lead_id = lead.id if lead else None
            current.row_sha256 = row.row_sha256
            current.raw = row.raw


def _persist_candidates(
    session: Session, prepared: _Prepared, lead_rows: list[Lead]
) -> None:
    lead_of_record: dict[int, Lead] = {}
    for lead_index, merged in enumerate(prepared.leads):
        for record_index in merged.record_indexes:
            lead_of_record[record_index] = lead_rows[lead_index]

    for candidate in prepared.candidates:
        left = lead_of_record.get(candidate.left_index)
        right = lead_of_record.get(candidate.right_index)
        if left is None or right is None or left.id == right.id:
            continue
        # Order the pair deterministically so a re-run matches the same row.
        first, second = sorted((left, right), key=lambda entry: str(entry.id))
        existing = session.scalar(
            select(DuplicateCandidate).where(
                DuplicateCandidate.lead_a_id == first.id,
                DuplicateCandidate.lead_b_id == second.id,
                DuplicateCandidate.method == candidate.method,
            )
        )
        if existing is not None:
            existing.confidence = candidate.confidence
            existing.evidence = dict(candidate.evidence)
            continue
        session.add(
            DuplicateCandidate(
                lead_a_id=first.id,
                lead_b_id=second.id,
                method=candidate.method,
                confidence=candidate.confidence,
                status=DuplicateStatus.PENDING,
                evidence=dict(candidate.evidence),
            )
        )
