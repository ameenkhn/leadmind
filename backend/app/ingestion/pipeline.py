"""The ingest pipeline.

Read → normalise → validate → deduplicate → merge → resolve companies → score → persist.

Three properties this module exists to guarantee:

**Idempotency.** Running the same workbook twice produces the same database, not two copies of
it. Leads are matched to existing rows through their exact identity identifiers (email, phone,
Facebook URL) rather than through insertion order, so a re-run updates in place. Every child
table has a natural uniqueness key and is upserted against it.

**Reconciliation.** Every source row ends up either as a lead or as a row merged into one, and
the report says which. A pipeline that cannot account for its input has no business being
trusted with a scoring model on top of it.

**Bounded query count.** Writing 2 351 leads with a lookup per lead per child table is roughly
15 000 round trips. Everything that can be is loaded once into :class:`_Store` and matched in
memory, which keeps the run linear in rows rather than in rows × tables.
"""

from __future__ import annotations

import datetime as dt
import statistics
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

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
    """Short git SHA, so a stored score can be traced to the code that produced it."""
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


# --------------------------------------------------------------------------------------
# In-memory half: no database involvement at all, which makes it testable without one.
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class Prepared:
    """Everything computed in memory, before a single row is written."""

    rows: list[SourceRow]
    records: list[NormalizedRecord]
    leads: list[MergedLead]
    companies: list[ResolvedCompany]
    candidates: list[CandidatePair]
    rows_merged: int
    source_file: str
    source_sha256: str


def prepare(path: Path) -> Prepared:
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

    return Prepared(
        rows=rows,
        records=records,
        leads=leads,
        companies=resolve_companies(leads),
        candidates=dedup.candidates,
        rows_merged=dedup.merged_row_count,
        source_file=str(path),
        source_sha256=reader.source_sha256,
    )


# --------------------------------------------------------------------------------------
# Persistence half
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Store:
    """Everything already in the database, loaded once and matched in memory."""

    category_ids: dict[str, UUID] = field(default_factory=dict)
    location_ids: dict[str, UUID] = field(default_factory=dict)
    companies: dict[str, Company] = field(default_factory=dict)
    lead_by_identity: dict[tuple[IdentifierKind, str], Lead] = field(default_factory=dict)
    quality: dict[tuple[UUID, str], DataQualityScore] = field(default_factory=dict)
    eval_labels: dict[tuple[UUID, str, LabelSource], EvalLabel] = field(default_factory=dict)
    candidates: dict[tuple[UUID, UUID, object], DuplicateCandidate] = field(default_factory=dict)
    source_records: dict[tuple[str, str, int], LeadSourceRecord] = field(default_factory=dict)

    @classmethod
    def load(cls, session: Session, source_sha256: str) -> _Store:
        store = cls()
        store.category_ids = {c.slug: c.id for c in session.scalars(select(Category)).all()}
        store.location_ids = {loc.slug: loc.id for loc in session.scalars(select(Location)).all()}
        store.companies = {c.primary_domain: c for c in session.scalars(select(Company)).all()}

        identifiers = session.scalars(
            select(LeadIdentifier)
            .where(LeadIdentifier.kind.in_(IDENTITY_KINDS))
            .options(
                selectinload(LeadIdentifier.lead).selectinload(Lead.identifiers),
                selectinload(LeadIdentifier.lead).selectinload(Lead.metrics),
                selectinload(LeadIdentifier.lead).selectinload(Lead.source_queries),
            )
        ).all()
        store.lead_by_identity = {
            (identifier.kind, identifier.value_normalized): identifier.lead
            for identifier in identifiers
        }

        store.quality = {
            (score.lead_id, score.rubric_version): score
            for score in session.scalars(select(DataQualityScore)).all()
        }
        store.eval_labels = {
            (label.lead_id, label.dimension, label.label_source): label
            for label in session.scalars(select(EvalLabel)).all()
        }
        store.candidates = {
            (c.lead_a_id, c.lead_b_id, c.method): c
            for c in session.scalars(select(DuplicateCandidate)).all()
        }
        store.source_records = {
            (r.source_sha256, r.source_sheet, r.source_row_no): r
            for r in session.scalars(
                select(LeadSourceRecord).where(LeadSourceRecord.source_sha256 == source_sha256)
            ).all()
        }
        return store

    def match(self, merged: MergedLead) -> list[Lead]:
        found: dict[UUID, Lead] = {}
        for identifier in merged.identifiers:
            if identifier.kind not in IDENTITY_KINDS:
                continue
            lead = self.lead_by_identity.get((identifier.kind, identifier.value))
            if lead is not None:
                found[lead.id] = lead
        return list(found.values())


def _sync_company(store: _Store, session: Session, resolved: ResolvedCompany) -> Company:
    company = store.companies.get(resolved.domain)
    if company is None:
        company = Company(
            primary_domain=resolved.domain,
            name=resolved.name,
            website_url=resolved.website_url,
        )
        session.add(company)
        store.companies[resolved.domain] = company
    else:
        company.name = resolved.name or company.name
        company.website_url = resolved.website_url or company.website_url
    return company


def _sync_identifiers(session: Session, lead: Lead, merged: MergedLead) -> int:
    existing = {(i.kind, i.value_normalized): i for i in lead.identifiers}
    written = 0
    for identifier in merged.identifiers:
        current = existing.get((identifier.kind, identifier.value))
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
        current = existing.get((MetricKind.FOLLOWERS, observation.source))
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


def _sync_eval_label(store: _Store, session: Session, lead: Lead, merged: MergedLead) -> int:
    """Store the shipped ``Relevance`` column as a *weak* label.

    Its provenance is unknown, so it seeds the evaluation set without ever being mistaken for
    ground truth. ``label_source`` is what keeps weak and gold labels from being averaged.
    """
    if not merged.relevance_labels:
        return 0
    label = merged.relevance_labels[0]
    key = (lead.id, RELEVANCE_DIMENSION, LabelSource.WEAK_RELEVANCE)
    existing = store.eval_labels.get(key)
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


def _sync_issues(session: Session, lead: Lead, merged: MergedLead, *, replace: bool) -> None:
    if replace:
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


def _sync_quality(
    store: _Store, session: Session, lead: Lead, merged: MergedLead, now: dt.datetime
) -> float:
    result = score_lead(merged, rubric=get_rubric())
    existing = store.quality.get((lead.id, result.rubric_version))
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


def _sync_source_records(
    store: _Store, session: Session, prepared: Prepared, run: IngestRun, leads: list[Lead]
) -> None:
    lead_of_row: dict[int, Lead] = {}
    for position, merged in enumerate(prepared.leads):
        for row_index in merged.record_indexes:
            lead_of_row[row_index] = leads[position]

    for index, row in enumerate(prepared.rows):
        lead = lead_of_row.get(index)
        current = store.source_records.get((row.source_sha256, row.sheet, row.row_no))
        if current is None:
            session.add(
                LeadSourceRecord(
                    ingest_run_id=run.id,
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
            current.ingest_run_id = run.id
            current.lead_id = lead.id if lead else None
            current.row_sha256 = row.row_sha256
            current.raw = row.raw


def _sync_candidates(
    store: _Store, session: Session, prepared: Prepared, leads: list[Lead]
) -> None:
    lead_of_row: dict[int, Lead] = {}
    for position, merged in enumerate(prepared.leads):
        for row_index in merged.record_indexes:
            lead_of_row[row_index] = leads[position]

    for candidate in prepared.candidates:
        left = lead_of_row.get(candidate.left_index)
        right = lead_of_row.get(candidate.right_index)
        if left is None or right is None or left.id == right.id:
            continue
        # Order the pair deterministically so a re-run finds the same row.
        first, second = sorted((left, right), key=lambda entry: str(entry.id))
        key = (first.id, second.id, candidate.method)
        existing = store.candidates.get(key)
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


def _persist(
    session: Session,
    prepared: Prepared,
    report: IngestReport,
    run_id: str,
    started: dt.datetime,
) -> list[float]:
    seed_categories(session)
    seed_locations(session)
    store = _Store.load(session, prepared.source_sha256)

    run = IngestRun(
        run_id=run_id,
        source_file=prepared.source_file,
        source_sha256=prepared.source_sha256,
        status=IngestStatus.RUNNING,
        started_at=started,
        code_version=_code_version(),
        rubric_version=get_rubric().version,
    )
    session.add(run)
    session.flush()

    company_rows = {
        resolved.domain: _sync_company(store, session, resolved) for resolved in prepared.companies
    }
    session.flush()

    scores: list[float] = []
    lead_rows: list[Lead] = []
    now = dt.datetime.now(dt.UTC)

    for merged in prepared.leads:
        matched = store.match(merged)
        if len(matched) > 1:
            logger.warning(
                "lead_matches_multiple_existing_records",
                lead=merged.display_name,
                matched=len(matched),
            )
        lead = matched[0] if matched else None
        created = lead is None
        if lead is None:
            lead = Lead(display_name=merged.display_name, normalized_name=merged.normalized_name)
            session.add(lead)
            session.flush()

        lead.display_name = merged.display_name
        lead.normalized_name = merged.normalized_name
        lead.entity_kind = merged.entity_kind
        lead.is_placeholder_name = merged.is_placeholder_name
        lead.location_raw = merged.location_raw
        lead.location_confidence = merged.location_confidence
        lead.location_id = store.location_ids.get(merged.location.slug) if merged.location else None
        lead.category_id = store.category_ids.get(merged.vertical_slug or "")
        lead.fb_category_raw = merged.fb_category_raw
        lead.niche_raw = merged.niche_raw
        lead.company_id = company_rows[merged.company_domain].id if merged.company_domain else None
        lead.first_seen_at = lead.first_seen_at or started
        lead.last_seen_at = started

        report.leads_created += int(created)
        report.leads_updated += int(not created)
        report.identifiers_written += _sync_identifiers(session, lead, merged)
        report.metric_observations_written += _sync_observations(session, lead, merged)
        report.source_queries_written += _sync_queries(session, lead, merged)
        report.eval_labels_written += _sync_eval_label(store, session, lead, merged)
        _sync_issues(session, lead, merged, replace=not created)
        scores.append(_sync_quality(store, session, lead, merged, now))
        lead_rows.append(lead)

    session.flush()
    _sync_source_records(store, session, prepared, run, lead_rows)
    _sync_candidates(store, session, prepared, lead_rows)

    run.status = IngestStatus.SUCCEEDED
    run.finished_at = dt.datetime.now(dt.UTC)
    run.stats = report.as_dict()
    return scores


def ingest(
    path: Path,
    session: Session | None = None,
    *,
    dry_run: bool = False,
    run_id: str | None = None,
) -> IngestReport:
    """Ingest a workbook and return a reconciliation report.

    ``session`` is required unless ``dry_run`` is set. A dry run performs the entire
    computation, including quality scoring, without opening a transaction — which makes it a
    usable pre-flight check against a workbook before any database exists.
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
        report.companies_total = len(prepared.companies)
        report.companies_multi_branch = sum(1 for c in prepared.companies if c.is_multi_branch)
        report.duplicate_candidates = dict(
            Counter(candidate.method.value for candidate in prepared.candidates)
        )

        by_code: Counter[str] = Counter()
        by_severity: Counter[str] = Counter()
        for record in prepared.records:
            for issue in record.issues:
                by_code[issue.code] += 1
                by_severity[issue.severity.value] += 1
        report.issues_by_code = by_code
        report.issues_by_severity = by_severity

        if dry_run:
            rubric = get_rubric()
            scores = [score_lead(lead, rubric=rubric).score for lead in prepared.leads]
            logger.info("ingest_dry_run_complete", leads=len(prepared.leads))
        else:
            assert session is not None
            scores = _persist(session, prepared, report, active_run_id, started)

        if scores:
            report.quality_mean = round(statistics.mean(scores), 2)
            report.quality_median = round(statistics.median(scores), 2)
            buckets: Counter[int] = Counter(min(int(score // 10) * 10, 90) for score in scores)
            report.quality_histogram = {
                f"{bucket}-{bucket + 9}": buckets[bucket] for bucket in sorted(buckets)
            }

    report.duration_seconds = (dt.datetime.now(dt.UTC) - started).total_seconds()
    logger.info("ingest_complete", **report.as_dict())
    return report


def leads_per_company(prepared: Prepared) -> dict[str, int]:
    """Convenience for reporting: branch counts keyed by domain."""
    counts: dict[str, int] = defaultdict(int)
    for company in prepared.companies:
        counts[company.domain] = company.branch_count
    return dict(counts)
