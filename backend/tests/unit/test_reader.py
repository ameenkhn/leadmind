"""Reader tests, including the schema collision that motivates the whole per-sheet design."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.core.errors import SchemaMismatchError
from app.ingestion.readers.column_map import SHEET_SPECS
from app.ingestion.readers.excel import ExcelLeadReader


class TestColumnMaps:
    def test_day3_niche_is_mapped_to_fb_category(self) -> None:
        """The single most consequential mapping decision in the project.

        `Day_1.Niche` holds five curated verticals; `Day_3.Niche` holds Facebook page
        categories, 74 of which appear verbatim in `Day_1.FB_Category`. Mapping them together
        on the strength of a shared column name would corrupt every vertical feature
        downstream. This assertion exists so that can never silently regress.
        """
        assert SHEET_SPECS["Day_3"].columns["Niche"] == "fb_category"
        assert SHEET_SPECS["Day_1"].columns["Niche"] == "niche"

    def test_day3_source_is_declared_inferred(self) -> None:
        spec = SHEET_SPECS["Day_3"]
        assert spec.defaults["source"] == "Meta Ad Library"
        assert "source" in spec.inferred_fields

    def test_serial_column_name_differs_between_sheets(self) -> None:
        assert "S.No" in SHEET_SPECS["Day_1"].columns
        assert "S.No." in SHEET_SPECS["Day_2"].columns


class TestExcelReader:
    def test_schema_validates(self, workbook_path: Path) -> None:
        assert ExcelLeadReader(workbook_path).validate_schema() == {
            "Day_1": 17,
            "Day_2": 13,
            "Day_3": 13,
        }

    def test_reads_every_row(self, workbook_path: Path) -> None:
        rows = list(ExcelLeadReader(workbook_path).iter_rows())
        assert len(rows) == 2520
        per_sheet = dict.fromkeys(SHEET_SPECS, 0)
        for row in rows:
            per_sheet[row.sheet] += 1
        assert per_sheet == {"Day_1": 900, "Day_2": 1000, "Day_3": 620}

    def test_day3_rows_carry_fb_category_not_niche(self, workbook_path: Path) -> None:
        rows = [r for r in ExcelLeadReader(workbook_path).iter_rows() if r.sheet == "Day_3"]
        assert any(row.get("fb_category") for row in rows)
        assert all(row.get("niche") is None for row in rows)

    def test_row_hash_is_stable_across_reads(self, workbook_path: Path) -> None:
        first = [r.row_sha256 for r in ExcelLeadReader(workbook_path).iter_rows()]
        second = [r.row_sha256 for r in ExcelLeadReader(workbook_path).iter_rows()]
        assert first == second

    def test_unknown_column_is_an_error(self, tmp_path: Path) -> None:
        """An unannounced column means the upstream export changed. Fail loudly."""
        path = tmp_path / "extra.xlsx"
        frame = pd.DataFrame(
            {header: ["x"] for header in SHEET_SPECS["Day_2"].expected_headers}
            | {"Surprise": ["x"]}
        )
        with pd.ExcelWriter(path) as writer:
            frame.to_excel(writer, sheet_name="Day_2", index=False)

        reader = ExcelLeadReader(path, specs={"Day_2": SHEET_SPECS["Day_2"]})
        with pytest.raises(SchemaMismatchError) as info:
            reader.validate_schema()
        assert "Surprise" in info.value.context["unexpected"]

    def test_missing_column_is_an_error(self, tmp_path: Path) -> None:
        path = tmp_path / "missing.xlsx"
        headers = sorted(SHEET_SPECS["Day_2"].expected_headers - {"Email"})
        with pd.ExcelWriter(path) as writer:
            pd.DataFrame({h: ["x"] for h in headers}).to_excel(
                writer, sheet_name="Day_2", index=False
            )

        reader = ExcelLeadReader(path, specs={"Day_2": SHEET_SPECS["Day_2"]})
        with pytest.raises(SchemaMismatchError) as info:
            reader.validate_schema()
        assert "Email" in info.value.context["missing"]
