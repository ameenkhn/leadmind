"""LeadMind command line."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.table import Table

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.ingestion.pipeline import ingest
from app.ingestion.report import IngestReport

if TYPE_CHECKING:
    from app.verification.runner import VerificationReport

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="LeadMind — AI lead intelligence and qualification engine.",
)
verify_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Turn Phase 1's 'unverified' placeholders into measurements.",
)
app.add_typer(verify_app, name="verify")
console = Console()


@app.command("ingest")
def ingest_command(
    workbook: Annotated[
        Path, typer.Argument(help="Path to the source workbook (.xlsx).", exists=True)
    ],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Process everything but write nothing.")
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit the report as JSON instead of a table.")
    ] = False,
) -> None:
    """Ingest a workbook: normalise, validate, deduplicate, score, and persist."""
    configure_logging()

    if dry_run:
        report = ingest(workbook, dry_run=True)
    else:
        from app.db.session import session_scope

        with session_scope() as session:
            report = ingest(workbook, session, dry_run=False)

    if as_json:
        console.print_json(json.dumps(report.as_dict()))
    else:
        _render(report)

    if not report.reconciles:
        console.print(
            "[bold red]Reconciliation failed:[/] rows read do not equal leads + merged rows."
        )
        raise typer.Exit(code=1)


@app.command("check-schema")
def check_schema_command(
    workbook: Annotated[Path, typer.Argument(help="Path to the workbook.", exists=True)],
) -> None:
    """Verify a workbook matches the declared per-sheet column maps, and stop if it does not."""
    configure_logging()
    from app.core.errors import SchemaMismatchError
    from app.ingestion.readers.excel import ExcelLeadReader

    try:
        counts = ExcelLeadReader(workbook).validate_schema()
    except SchemaMismatchError as exc:
        console.print(f"[bold red]Schema mismatch:[/] {exc}")
        raise typer.Exit(code=1) from exc

    table = Table(title="Declared sheets", show_header=True)
    table.add_column("Sheet")
    table.add_column("Columns", justify="right")
    for sheet, column_count in counts.items():
        table.add_row(sheet, str(column_count))
    console.print(table)


@app.command("config")
def config_command() -> None:
    """Print effective settings with secrets redacted."""
    settings = get_settings()
    url = settings.sync_database_url
    if "@" in url:
        scheme, _, rest = url.partition("://")
        url = f"{scheme}://***@{rest.split('@', 1)[1]}"
    console.print_json(
        json.dumps(
            {
                "environment": settings.environment,
                "log_level": settings.log_level,
                "database_url": url,
                "config_dir": str(settings.config_dir),
                "fuzzy_name_threshold": settings.fuzzy_name_threshold,
            }
        )
    )


@verify_app.command("emails")
def verify_emails_command(
    concurrency: Annotated[
        int, typer.Option("--concurrency", "-c", min=1, max=128, help="Parallel DNS queries.")
    ] = 8,
    limit: Annotated[
        int | None, typer.Option("--limit", help="Check at most this many domains.")
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Re-check domains even if their TTL has not expired.")
    ] = False,
) -> None:
    """Verify that email domains accept mail, by MX lookup.

    Checked per domain rather than per address: 2,520 addresses share far fewer domains, and the
    cache means a second run costs nothing. No SMTP callout is performed — see
    docs/04-verification.md for why that is deliberate.
    """
    configure_logging()
    from app.db.session import session_scope
    from app.verification.runner import verify_email_domains

    with session_scope() as session:
        report = verify_email_domains(session, concurrency=concurrency, limit=limit, force=force)
    _render_verification(report)


@verify_app.command("websites")
def verify_websites_command(
    concurrency: Annotated[
        int, typer.Option("--concurrency", "-c", min=1, max=64, help="Parallel requests.")
    ] = 16,
    per_host: Annotated[
        int, typer.Option("--per-host", min=1, max=8, help="Parallel requests per host.")
    ] = 2,
    timeout: Annotated[float, typer.Option("--timeout", help="Per-request timeout.")] = 12.0,
    limit: Annotated[
        int | None, typer.Option("--limit", help="Check at most this many URLs.")
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Re-check URLs even if their TTL has not expired.")
    ] = False,
) -> None:
    """Check which owned websites actually answer.

    Requests are SSRF-guarded and rate-limited per host: the targets are small businesses'
    hosting, and a burst of parallel requests is indistinguishable from an attack.
    """
    configure_logging()
    from app.db.session import session_scope
    from app.verification.runner import verify_websites

    with session_scope() as session:
        report = verify_websites(
            session,
            concurrency=concurrency,
            per_host=per_host,
            timeout=timeout,
            limit=limit,
            force=force,
        )
    _render_verification(report)


@app.command("rescore")
def rescore_command(
    workbook: Annotated[
        Path, typer.Argument(help="Path to the source workbook (.xlsx).", exists=True)
    ],
) -> None:
    """Recompute data quality scores against the current rubric and verification data.

    A full re-ingest would produce the same result — it is idempotent and reads the same
    signals — but this touches only the scores.
    """
    configure_logging()
    from app.db.session import session_scope
    from app.ingestion.pipeline import rescore

    with session_scope() as session:
        summary = rescore(workbook, session)

    table = Table(title="Rescore", show_header=False)
    table.add_column("", style="bold")
    table.add_column("", justify="right")
    table.add_row("Rubric version", summary.rubric_version)
    table.add_row("Leads scored", f"{summary.leads_scored:,}")
    table.add_row("Mean / median", f"{summary.mean} / {summary.median}")
    console.print(table)

    spread = Table(title="Factors actually measured per lead")
    spread.add_column("Factors", justify="right")
    spread.add_column("Leads", justify="right")
    for count, leads in summary.factors_evaluated.items():
        spread.add_row(str(count), f"{leads:,}")
    console.print(spread)


@verify_app.command("status")
def verify_status_command() -> None:
    """Show what has been verified so far, and what is stale."""
    configure_logging()
    import datetime as dt

    from sqlalchemy import func, select

    from app.db.session import session_scope
    from app.models.verification import DomainVerificationRecord, UrlVerificationRecord

    now = dt.datetime.now(dt.UTC)
    table = Table(title="Verification coverage")
    table.add_column("Check")
    table.add_column("Records", justify="right")
    table.add_column("Fresh", justify="right")
    table.add_column("Stale", justify="right")

    with session_scope() as session:
        for label, model in (
            ("email domains", DomainVerificationRecord),
            ("websites", UrlVerificationRecord),
        ):
            total = session.scalar(select(func.count()).select_from(model)) or 0
            fresh = (
                session.scalar(
                    select(func.count()).select_from(model).where(model.expires_at > now)
                )
                or 0
            )
            table.add_row(label, f"{total:,}", f"{fresh:,}", f"{total - fresh:,}")
    console.print(table)


def _render_verification(report: VerificationReport) -> None:
    summary = Table(title=f"Verification — {report.kind}", show_header=False)
    summary.add_column("", style="bold")
    summary.add_column("", justify="right")
    summary.add_row("Distinct targets", f"{report.candidates:,}")
    summary.add_row("Served from cache", f"{report.served_from_cache:,}")
    summary.add_row("Checked now", f"{report.checked:,}")
    for key, value in report.extra.items():
        summary.add_row(key.replace("_", " ").capitalize(), f"{value:,}")
    summary.add_row("Leads affected", f"{report.leads_affected:,}")
    summary.add_row("Duration", f"{report.duration_seconds:.1f}s")
    console.print(summary)

    if report.by_status:
        outcomes = Table(title="Outcomes")
        outcomes.add_column("Status")
        outcomes.add_column("Count", justify="right")
        outcomes.add_column("")
        peak = max(report.by_status.values())
        for status, count in report.by_status.most_common():
            outcomes.add_row(status, f"{count:,}", "█" * max(1, round(count / peak * 36)))
        console.print(outcomes)


def _render(report: IngestReport) -> None:
    summary = Table(
        title=f"Ingest report{' (dry run)' if report.dry_run else ''}", show_header=False
    )
    summary.add_column("", style="bold")
    summary.add_column("", justify="right")
    summary.add_row("Source", Path(report.source_file).name)
    summary.add_row("SHA-256", report.source_sha256[:16] + "…")
    summary.add_row("Rows read", f"{report.rows_read:,}")
    for sheet, count in report.rows_per_sheet.items():
        summary.add_row(f"  {sheet}", f"{count:,}")
    summary.add_row("Rows merged (exact duplicate)", f"{report.rows_merged:,}")
    summary.add_row("Leads", f"{report.leads_total:,}")
    summary.add_row("  created", f"{report.leads_created:,}")
    summary.add_row("  updated", f"{report.leads_updated:,}")
    summary.add_row("Companies", f"{report.companies_total:,}")
    summary.add_row("  multi-branch", f"{report.companies_multi_branch:,}")
    summary.add_row("Identifiers written", f"{report.identifiers_written:,}")
    summary.add_row("Follower observations", f"{report.metric_observations_written:,}")
    summary.add_row("Source queries", f"{report.source_queries_written:,}")
    summary.add_row("Weak eval labels", f"{report.eval_labels_written:,}")
    summary.add_row(
        "Data quality (mean / median)",
        f"{report.quality_mean} / {report.quality_median}",
    )
    summary.add_row("Duration", f"{report.duration_seconds:.1f}s")
    summary.add_row(
        "Reconciles",
        "[green]yes[/]" if report.reconciles else "[bold red]NO[/]",
    )
    console.print(summary)

    if report.duplicate_candidates:
        queue = Table(title="Queued for human review (never auto-merged)")
        queue.add_column("Method")
        queue.add_column("Pairs", justify="right")
        for method, count in sorted(report.duplicate_candidates.items()):
            queue.add_row(method, f"{count:,}")
        console.print(queue)

    if report.issues_by_code:
        issues = Table(title="Validation issues (nothing was dropped)")
        issues.add_column("Code")
        issues.add_column("Rows", justify="right")
        for code, count in report.issues_by_code.most_common(20):
            issues.add_row(code, f"{count:,}")
        console.print(issues)

    if report.quality_histogram:
        histogram = Table(title="Data quality distribution")
        histogram.add_column("Band")
        histogram.add_column("Leads", justify="right")
        histogram.add_column("")
        peak = max(report.quality_histogram.values())
        for band, count in report.quality_histogram.items():
            bar = "█" * max(1, round(count / peak * 40))
            histogram.add_row(band, f"{count:,}", bar)
        console.print(histogram)


if __name__ == "__main__":
    app()
