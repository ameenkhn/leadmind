"""Sheet-aware workbook reader.

Produces canonical :class:`SourceRow` records without interpreting a single value. Normalisation
happens downstream; this layer's only jobs are (a) refusing to guess at a schema it was not told
about and (b) making re-ingestion idempotent via content hashes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from app.core.errors import SchemaMismatchError
from app.core.logging import get_logger
from app.ingestion.readers.column_map import SHEET_SPECS, SheetSpec

logger = get_logger(__name__)

_CHUNK = 1 << 20


@dataclass(frozen=True, slots=True)
class SourceRow:
    """One spreadsheet row, canonicalised but not yet normalised."""

    source_file: str
    source_sha256: str
    sheet: str
    sheet_sequence: int
    row_no: int
    """1-based position within the sheet, independent of the sheet's own S.No column."""

    serial: str | None
    values: dict[str, Any]
    raw: dict[str, Any]
    inferred_fields: frozenset[str]

    @property
    def row_sha256(self) -> str:
        payload = json.dumps(self.raw, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get(self, field: str) -> Any:
        return self.values.get(field)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _clean(value: Any) -> Any:
    """Collapse pandas' several flavours of 'absent' into ``None``; keep everything else raw."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if pd.api.types.is_scalar(value) and pd.isna(value):
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    return value


class ExcelLeadReader:
    """Reads ``Outbound_Leads.xlsx``-shaped workbooks against declared per-sheet specs."""

    def __init__(self, path: Path, *, specs: dict[str, SheetSpec] | None = None) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"workbook not found: {self.path}")
        self.specs = dict(specs or SHEET_SPECS)
        self.source_sha256 = file_sha256(self.path)

    def validate_schema(self) -> dict[str, int]:
        """Confirm every sheet matches its declared spec exactly.

        Raises :class:`SchemaMismatchError` on an unknown sheet, a missing column, or a column
        the spec has never heard of. An unannounced column almost always means the upstream
        export changed, and continuing would either drop the new field or mis-map it.
        """
        frames = pd.read_excel(self.path, sheet_name=None, nrows=0)
        unknown_sheets = set(frames) - set(self.specs)
        if unknown_sheets:
            raise SchemaMismatchError(
                "workbook contains sheets with no declared column map",
                sheets=sorted(unknown_sheets),
                known=sorted(self.specs),
            )
        missing_sheets = set(self.specs) - set(frames)
        if missing_sheets:
            raise SchemaMismatchError(
                "declared sheets absent from workbook", sheets=sorted(missing_sheets)
            )

        counts: dict[str, int] = {}
        for sheet, frame in frames.items():
            spec = self.specs[sheet]
            headers = {str(c).strip() for c in frame.columns}
            unexpected = headers - spec.expected_headers
            missing = spec.expected_headers - headers
            if unexpected or missing:
                raise SchemaMismatchError(
                    "sheet columns do not match the declared map",
                    sheet=sheet,
                    unexpected=sorted(unexpected),
                    missing=sorted(missing),
                )
            counts[sheet] = len(spec.columns)
        return counts

    def iter_rows(self) -> Iterator[SourceRow]:
        """Yield every row of every sheet in declared sheet order."""
        self.validate_schema()
        frames = pd.read_excel(self.path, sheet_name=None, dtype=object)
        for spec in sorted(self.specs.values(), key=lambda s: s.sequence):
            frame = frames[spec.sheet]
            logger.info("sheet_read", sheet=spec.sheet, rows=len(frame))
            for offset, record in enumerate(frame.to_dict(orient="records"), start=1):
                raw = {str(k).strip(): _clean(v) for k, v in record.items()}
                values: dict[str, Any] = {
                    canonical: raw.get(header) for header, canonical in spec.columns.items()
                }
                for field_name, default in spec.defaults.items():
                    if values.get(field_name) is None:
                        values[field_name] = default
                serial = values.get("serial")
                yield SourceRow(
                    source_file=self.path.name,
                    source_sha256=self.source_sha256,
                    sheet=spec.sheet,
                    sheet_sequence=spec.sequence,
                    row_no=offset,
                    serial=None if serial is None else str(serial),
                    values=values,
                    raw=raw,
                    inferred_fields=spec.inferred_fields,
                )
