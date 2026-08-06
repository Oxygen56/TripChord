"""Add SearchRun / SourceAttempt / TerminalReceipt persistence (v0.3)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260806_0001"
down_revision: str | None = "20260727_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "search_runs",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=100), nullable=False),
        sa.Column("snapshot_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_index(
        "ix_search_runs_tenant_created", "search_runs", ["tenant_id", "created_at"]
    )
    op.create_index("ix_search_runs_tenant_id", "search_runs", ["tenant_id"])
    op.create_table(
        "source_attempts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.String(length=64),
            sa.ForeignKey("search_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_id", sa.String(length=120), nullable=False),
        sa.Column("scope_key", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("terminal_state", sa.String(length=40), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_class", sa.String(length=120), nullable=True),
        sa.Column("detail", sa.String(length=400), nullable=True),
    )
    op.create_index("ix_source_attempts_run_id", "source_attempts", ["run_id"])
    op.create_table(
        "terminal_receipts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.String(length=64),
            sa.ForeignKey("search_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_id", sa.String(length=120), nullable=False),
        sa.Column("scope_key", sa.String(length=160), nullable=False),
        sa.Column("terminal_state", sa.String(length=40), nullable=False),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("evidence_sha256", sa.String(length=64), nullable=True),
        sa.Column("receipt_sha256", sa.String(length=64), nullable=False),
    )
    op.create_index("ix_terminal_receipts_run_id", "terminal_receipts", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_terminal_receipts_run_id", table_name="terminal_receipts")
    op.drop_table("terminal_receipts")
    op.drop_index("ix_source_attempts_run_id", table_name="source_attempts")
    op.drop_table("source_attempts")
    op.drop_index("ix_search_runs_tenant_created", table_name="search_runs")
    op.drop_index("ix_search_runs_tenant_id", table_name="search_runs")
    op.drop_table("search_runs")
