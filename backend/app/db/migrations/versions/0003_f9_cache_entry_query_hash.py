"""F9 — cache_entries.query_hash: the semantic cache's upsert + poison-control key

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-17

F12's `0001_initial.py` already created the whole `cache_entries` table — `id`, `query_text`,
`embedding` (LargeBinary), `answer` (JSONB), `index_manifest_id`, `hits`, `created_at`,
`last_hit_at` — so this migration does NOT recreate any of it. `embedding` stays BYTEA: Pinecone is
the vector store and F9 compares cached vectors in-process with a numpy matmul, so there are no
DB-side vector ops and pgvector stays out (ops.py's module docstring).

It adds exactly one column:

- `query_hash` (String, NOT NULL, unique): sha256 of the normalized query. F9's write path upserts
  `ON CONFLICT (query_hash) DO UPDATE` — without the unique key, every repeat ask of a cached
  question would insert a duplicate row and grow the brute-force matrix without bound. It is also
  what `python -m app.caching.run --delete-query` keys on.

Deliberately NOT added: `request_id`. The brief asks for per-entry delete by request id, but nothing
in the pipeline generates one yet (F13 owns request logging), so the column would be NULL on every
row F9 writes. `--delete-query` gives the operator the same capability on the column above. See
docs/specs/f9-redis/requirements.md AC-21.

The cached token counts F9 reports "$ saved" from need no column either: they ride inside the
existing `answer` JSONB now that `AnswerResponse` carries `tokens_in`/`tokens_out`.

The `server_default=""` + drop dance below is the standard NOT NULL add: it backfills any existing
rows (none in practice — nothing has written to this table before F9) and is then removed so the
column matches the model exactly and `alembic check` reports no drift.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cache_entries",
        sa.Column("query_hash", sa.String(), nullable=False, server_default=""),
    )
    op.alter_column("cache_entries", "query_hash", server_default=None)
    op.create_unique_constraint("uq_cache_entries_query_hash", "cache_entries", ["query_hash"])


def downgrade() -> None:
    op.drop_constraint("uq_cache_entries_query_hash", "cache_entries", type_="unique")
    op.drop_column("cache_entries", "query_hash")
