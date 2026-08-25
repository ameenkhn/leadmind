"""schema v3 human review and reversible merge

Revision ID: 5f21ac9d1b64
Revises: 34b5acd820d9
Create Date: 2026-08-25 09:30:00.000000

Phase 2 gives the duplicate review queue somewhere to put its answer.

``leads.merged_into_id`` is a pointer, not a deletion. Confirming a duplicate links the loser to
the survivor and leaves every child row — identifiers, observations, provenance, validation
issues — untouched, so the decision is reversible and the reconciliation identity that proves
ingestion worked still holds.

The trigram index exists because the leads list is searched by substring (``?q=pumo``) and
``ILIKE '%pumo%'`` cannot use a b-tree. ``pg_trgm`` was already created in schema v1 for
deduplication; this only adds the index that makes the API's search cheap.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5f21ac9d1b64"
down_revision: Union[str, Sequence[str], None] = "34b5acd820d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("leads", sa.Column("merged_into_id", sa.Uuid(), nullable=True))
    op.add_column("leads", sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("leads", sa.Column("merged_by", sa.String(length=128), nullable=True))
    op.create_foreign_key(
        "fk_leads_merged_into_id_leads",
        "leads",
        "leads",
        ["merged_into_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_leads_merged_into_id", "leads", ["merged_into_id"])
    op.create_check_constraint("ck_leads_no_self_merge", "leads", "merged_into_id <> id")

    op.add_column("duplicate_candidates", sa.Column("resolution_note", sa.Text(), nullable=True))

    op.execute(
        "CREATE INDEX ix_leads_normalized_name_trgm "
        "ON leads USING gin (normalized_name gin_trgm_ops)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS ix_leads_normalized_name_trgm")
    op.drop_column("duplicate_candidates", "resolution_note")
    op.drop_constraint("ck_leads_no_self_merge", "leads", type_="check")
    op.drop_index("ix_leads_merged_into_id", table_name="leads")
    op.drop_constraint("fk_leads_merged_into_id_leads", "leads", type_="foreignkey")
    op.drop_column("leads", "merged_by")
    op.drop_column("leads", "merged_at")
    op.drop_column("leads", "merged_into_id")
