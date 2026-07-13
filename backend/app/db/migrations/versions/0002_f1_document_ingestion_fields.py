"""F1 — documents ingestion fields: note column, nullable sha256/downloaded_at, status index

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-13

F12's `0001_initial.py` already created `documents.status/is_scanned/page_count/sha256/
downloaded_at` and the `document_status` enum (already including `indexed`, for F2) — this
migration does NOT recreate any of that (design.md §8's snippet assumed those columns didn't
exist yet; they do). It adds only what F1 actually needs on top:

- `note` (Text, nullable): human-readable status/failure note (AC-3, AC-8, AC-9, AC-26).
- `sha256` / `downloaded_at` relaxed to nullable: AC-2 upserts a row at `status=registered`
  *before* download, when neither value is known yet.
- `ix_documents_status`: the report/CLI (`--type`, run report) filter by status.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("note", sa.Text(), nullable=True))
    op.alter_column("documents", "sha256", existing_type=sa.String(), nullable=True)
    op.alter_column(
        "documents", "downloaded_at", existing_type=sa.DateTime(timezone=True), nullable=True
    )
    op.create_index("ix_documents_status", "documents", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_documents_status", table_name="documents")
    op.alter_column(
        "documents", "downloaded_at", existing_type=sa.DateTime(timezone=True), nullable=False
    )
    op.alter_column("documents", "sha256", existing_type=sa.String(), nullable=False)
    op.drop_column("documents", "note")
