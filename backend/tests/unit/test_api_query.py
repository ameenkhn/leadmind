"""Query construction, checked by compiling SQL rather than by running it.

These assertions are about the *shape* of the statement — that merged leads are excluded by
default, that every ordering ends in a tiebreak, that user input reaches the database as a bound
parameter and never as SQL text. All of that is decided at compile time, so it needs no database
and runs in the unit suite where it will actually be run often.
"""

from __future__ import annotations

import uuid

from sqlalchemy.dialects import postgresql

from app.models.enums import EntityKind, IdentifierKind
from app.services.leads import (
    DEFAULT_SORT,
    LeadFilters,
    apply_sort,
    build_lead_query,
)
from app.verification.types import VerificationStatus

RUBRIC = "1.1"


def compile_sql(statement: object) -> str:
    return str(
        statement.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}
        )
    )


class TestDefaults:
    def test_merged_leads_are_hidden_by_default(self) -> None:
        sql = compile_sql(build_lead_query(LeadFilters()))
        assert "merged_into_id IS NULL" in sql

    def test_include_merged_removes_the_predicate(self) -> None:
        sql = compile_sql(build_lead_query(LeadFilters(include_merged=True)))
        assert "merged_into_id IS NULL" not in sql

    def test_no_filters_produces_no_where_clause(self) -> None:
        sql = compile_sql(build_lead_query(LeadFilters(include_merged=True)))
        assert "WHERE" not in sql


class TestSorting:
    def test_every_sort_tiebreaks_on_id(self) -> None:
        """Without this, equal-scored leads swap between pages and some are never seen."""
        for sort in ("quality", "-quality", "followers", "name", "created", "updated"):
            sql = compile_sql(
                apply_sort(build_lead_query(LeadFilters()), sort, rubric_version=RUBRIC)
            )
            assert sql.rstrip().endswith("leads.id ASC"), sort

    def test_unknown_sort_falls_back_instead_of_failing(self) -> None:
        """Sort keys come from a query string. An unrecognised one is a typo, not an attack
        surface: it must never reach SQL, and it must not 500 either."""
        hostile = "; DROP TABLE leads--"
        sql = compile_sql(
            apply_sort(build_lead_query(LeadFilters()), hostile, rubric_version=RUBRIC)
        )
        assert "DROP" not in sql.upper()
        assert "data_quality_scores" in sql

    def test_descending_sort_puts_nulls_last(self) -> None:
        sql = compile_sql(
            apply_sort(build_lead_query(LeadFilters()), "-quality", rubric_version=RUBRIC)
        )
        assert "NULLS LAST" in sql

    def test_ascending_sort_also_puts_nulls_last(self) -> None:
        """An unscored lead is an absence of evidence; flipping the sort must not promote it."""
        sql = compile_sql(
            apply_sort(build_lead_query(LeadFilters()), "quality", rubric_version=RUBRIC)
        )
        assert "NULLS LAST" in sql

    def test_default_sort_is_recognised(self) -> None:
        assert DEFAULT_SORT.lstrip("-") == "quality"


class TestFilters:
    def test_search_text_is_bound_never_interpolated(self) -> None:
        sql = compile_sql(build_lead_query(LeadFilters(q="'; DROP TABLE leads--")))
        assert "DROP TABLE" not in sql
        assert "ILIKE" in sql.upper()

    def test_search_covers_identifier_values(self) -> None:
        """Pasting an email address into the search box has to find its lead."""
        sql = compile_sql(build_lead_query(LeadFilters(q="someone@example.com")))
        assert "lead_identifiers" in sql

    def test_channel_requirements_are_conjunctive(self) -> None:
        sql = compile_sql(
            build_lead_query(
                LeadFilters(has_channels=(IdentifierKind.EMAIL, IdentifierKind.LINKEDIN))
            )
        )
        assert sql.count("EXISTS") >= 2

    def test_missing_channel_is_a_negated_exists(self) -> None:
        sql = compile_sql(build_lead_query(LeadFilters(missing_channels=(IdentifierKind.WEBSITE,))))
        assert "NOT (EXISTS" in sql

    def test_mailbox_status_joins_the_domain_cache_by_domain(self) -> None:
        """Verification is cached per domain, not per address — 1 269 gmail leads, one row."""
        sql = compile_sql(
            build_lead_query(LeadFilters(mailbox_status=VerificationStatus.UNREACHABLE))
        )
        assert "domain_verifications" in sql
        assert "split_part" in sql

    def test_quality_bounds_scope_to_a_rubric_version(self) -> None:
        sql = compile_sql(build_lead_query(LeadFilters(min_quality=80)))
        assert "rubric_version" in sql

    def test_followers_uses_the_latest_observation_not_the_largest(self) -> None:
        sql = compile_sql(build_lead_query(LeadFilters(min_followers=1000)))
        assert "batch_sequence DESC" in sql
        assert "max(" not in sql.lower()

    def test_owned_website_maps_to_company_presence(self) -> None:
        sql = compile_sql(build_lead_query(LeadFilters(owned_website=True)))
        assert "company_id IS NOT NULL" in sql

    def test_entity_kind_and_company_filters_combine(self) -> None:
        filters = LeadFilters(
            entity_kinds=(EntityKind.BUSINESS,), company_id=uuid.uuid4(), placeholder_name=False
        )
        sql = compile_sql(build_lead_query(filters))
        assert "entity_kind" in sql
        assert "company_id =" in sql
        assert "is_placeholder_name IS false" in sql
