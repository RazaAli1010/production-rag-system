"""initial schema — all 12 tables, enums, FKs, indexes

Revision ID: 0001
Revises:
Create Date: 2026-07-13

Order (design.md §7): PG enums -> tables (FK-dependency order) -> post-create circular FK
(sessions.summarized_upto_message_id -> messages.id, matching the model's use_alter=True) ->
indexes. Constraint/index names are spelled out explicitly to match Base.metadata's naming
convention (base.py) exactly, so a post-upgrade `alembic revision --autogenerate` diff is empty.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

user_role = postgresql.ENUM("student", "admin", name="user_role")
document_status = postgresql.ENUM(
    "registered", "downloaded", "extracted", "indexed", "failed", name="document_status"
)
message_role = postgresql.ENUM("user", "assistant", "system", name="message_role")
request_channel = postgresql.ENUM("web", "telegram", "api", name="request_channel")


def _created_at_column(name: str = "created_at") -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def upgrade() -> None:
    bind = op.get_bind()
    user_role.create(bind, checkfirst=True)
    document_status.create(bind, checkfirst=True)
    message_role.create(bind, checkfirst=True)
    request_channel.create(bind, checkfirst=True)

    # --- users ---
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("hashed_password", sa.String(), nullable=False),
        sa.Column(
            "role", postgresql.ENUM(name="user_role", create_type=False), nullable=False
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        _created_at_column(),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    # --- documents ---
    op.create_table(
        "documents",
        sa.Column("doc_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("source_org", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("file_type", sa.String(), nullable=False),
        sa.Column("downloaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version_label", sa.String(), nullable=False),
        sa.Column("is_scanned", sa.Boolean(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column(
            "status", postgresql.ENUM(name="document_status", create_type=False), nullable=False
        ),
        sa.PrimaryKeyConstraint("doc_id", name="pk_documents"),
        sa.CheckConstraint("source_org IN ('PU', 'HEC')", name="ck_documents_source_org_valid"),
        sa.CheckConstraint(
            "file_type IN ('pdf', 'html', 'docx', 'pptx', 'xlsx')",
            name="ck_documents_file_type_valid",
        ),
    )
    op.create_index("ix_documents_sha256", "documents", ["sha256"], unique=False)

    # --- api_keys ---
    op.create_table(
        "api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key_hash", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=True),
        _created_at_column(),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_api_keys_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_api_keys"),
    )

    # --- refresh_tokens ---
    op.create_table(
        "refresh_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("jti", sa.String(), nullable=False),
        _created_at_column("issued_at"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_jti", sa.String(), nullable=True),
        sa.Column("user_agent", sa.String(), nullable=True),
        sa.Column("ip", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_refresh_tokens_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_refresh_tokens"),
    )
    op.create_index("ix_refresh_tokens_jti", "refresh_tokens", ["jti"], unique=True)

    # --- login_attempts ---
    op.create_table(
        "login_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email_or_ip", sa.String(), nullable=False),
        _created_at_column("attempted_at"),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_login_attempts"),
    )
    op.create_index(
        "ix_login_attempts_email_or_ip", "login_attempts", ["email_or_ip"], unique=False
    )
    op.create_index(
        "ix_login_attempts_attempted_at", "login_attempts", ["attempted_at"], unique=False
    )
    op.create_index(
        "ix_login_attempts_email_or_ip_attempted_at",
        "login_attempts",
        ["email_or_ip", "attempted_at"],
        unique=False,
    )

    # --- chunks (depends on documents) ---
    op.create_table(
        "chunks",
        sa.Column("chunk_id", sa.String(), nullable=False),
        sa.Column("doc_id", sa.String(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("section_heading", sa.String(), nullable=True),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("anchor", sa.String(), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["doc_id"], ["documents.doc_id"], name="fk_chunks_doc_id_documents", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("chunk_id", name="pk_chunks"),
    )
    op.create_index("ix_chunks_doc_id_seq", "chunks", ["doc_id", "seq"], unique=False)

    # --- sessions (user_id -> users; summarized_upto_message_id -> messages added post-create) ---
    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("summary", sa.String(), nullable=True),
        sa.Column("summary_token_count", sa.Integer(), nullable=True),
        sa.Column("summarized_upto_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        _created_at_column(),
        _created_at_column("last_active_at"),
        sa.Column("is_archived", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_sessions_user_id_users", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
    )

    # --- messages (depends on sessions) ---
    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "role", postgresql.ENUM(name="message_role", create_type=False), nullable=False
        ),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("citations", postgresql.JSONB(), nullable=True),
        sa.Column("refused", sa.Boolean(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=True),
        _created_at_column(),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name="fk_messages_session_id_sessions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_messages"),
    )

    # --- post-create circular FK: sessions.summarized_upto_message_id -> messages.id ---
    op.create_foreign_key(
        "fk_sessions_summarized_upto_message_id_messages",
        "sessions",
        "messages",
        ["summarized_upto_message_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_index(
        "ix_sessions_user_id_last_active_at",
        "sessions",
        ["user_id", "last_active_at"],
        unique=False,
    )
    op.create_index(
        "ix_messages_session_id_created_at", "messages", ["session_id", "created_at"], unique=False
    )

    # --- request_logs (depends on users, sessions) ---
    op.create_table(
        "request_logs",
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "channel", postgresql.ENUM(name="request_channel", create_type=False), nullable=False
        ),
        sa.Column("query_hash", sa.String(), nullable=False),
        sa.Column("pipeline_flags", postgresql.JSONB(), nullable=False),
        sa.Column("cache_hit", sa.Boolean(), nullable=False),
        sa.Column("refused", sa.Boolean(), nullable=False),
        sa.Column("degraded", sa.Boolean(), nullable=False),
        sa.Column("memory_summarized", sa.Boolean(), nullable=False),
        sa.Column("embed_ms", sa.Integer(), nullable=True),
        sa.Column("retrieve_ms", sa.Integer(), nullable=True),
        sa.Column("rerank_ms", sa.Integer(), nullable=True),
        sa.Column("rewrite_ms", sa.Integer(), nullable=True),
        sa.Column("memory_ms", sa.Integer(), nullable=True),
        sa.Column("summarize_ms", sa.Integer(), nullable=True),
        sa.Column("llm_ms", sa.Integer(), nullable=True),
        sa.Column("total_ms", sa.Integer(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=False),
        sa.Column("tokens_out", sa.Integer(), nullable=False),
        sa.Column("est_cost_usd", sa.Float(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.Column("error_type", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_request_logs_user_id_users", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name="fk_request_logs_session_id_sessions",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("request_id", name="pk_request_logs"),
    )

    # --- cache_entries ---
    op.create_table(
        "cache_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("query_text", sa.String(), nullable=False),
        sa.Column("embedding", sa.LargeBinary(), nullable=False),
        sa.Column("answer", postgresql.JSONB(), nullable=False),
        sa.Column("index_manifest_id", sa.String(), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False),
        _created_at_column(),
        sa.Column("last_hit_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_cache_entries"),
    )

    # --- eval_runs ---
    op.create_table(
        "eval_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("git_sha", sa.String(), nullable=False),
        sa.Column("index_manifest", postgresql.JSONB(), nullable=False),
        sa.Column("pipeline_flags", postgresql.JSONB(), nullable=False),
        _created_at_column("started_at"),
        sa.PrimaryKeyConstraint("id", name="pk_eval_runs"),
    )

    # --- eval_results (depends on eval_runs) ---
    op.create_table(
        "eval_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("metric", sa.String(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("slice_tag", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["eval_runs.id"],
            name="fk_eval_results_run_id_eval_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_eval_results"),
    )


def downgrade() -> None:
    op.drop_table("eval_results")
    op.drop_table("eval_runs")
    op.drop_table("cache_entries")
    op.drop_table("request_logs")

    op.drop_constraint(
        "fk_sessions_summarized_upto_message_id_messages", "sessions", type_="foreignkey"
    )
    op.drop_table("messages")
    op.drop_table("sessions")
    op.drop_table("chunks")
    op.drop_table("login_attempts")
    op.drop_table("refresh_tokens")
    op.drop_table("api_keys")
    op.drop_table("documents")
    op.drop_table("users")

    bind = op.get_bind()
    request_channel.drop(bind, checkfirst=True)
    message_role.drop(bind, checkfirst=True)
    document_status.drop(bind, checkfirst=True)
    user_role.drop(bind, checkfirst=True)
