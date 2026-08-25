"""LeadMind command line."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.ingestion.pipeline import ingest
from app.ingestion.report import IngestReport

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="LeadMind — AI lead intelligence and qualification engine.",
)
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
