"""Add request idempotency, job leases, attempts, and trace identifiers."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260727_0003"
down_revision: str | None = "20260727_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workspaces") as batch:
        batch.add_column(sa.Column("idempotency_key", sa.String(length=200), nullable=True))
        batch.create_unique_constraint(
            "uq_workspace_tenant_idempotency",
            ["tenant_id", "idempotency_key"],
        )
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3")
        )
        batch.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("idempotency_key", sa.String(length=200), nullable=True))
        batch.add_column(
            sa.Column("trace_id", sa.String(length=36), nullable=False, server_default="legacy")
        )
        batch.create_index("ix_jobs_trace_id", ["trace_id"])
        batch.create_unique_constraint(
            "uq_job_workspace_idempotency",
            ["workspace_id", "idempotency_key"],
        )
        batch.alter_column("attempts", server_default=None)
        batch.alter_column("max_attempts", server_default=None)
        batch.alter_column("trace_id", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint("uq_job_workspace_idempotency", type_="unique")
        batch.drop_index("ix_jobs_trace_id")
        batch.drop_column("trace_id")
        batch.drop_column("idempotency_key")
        batch.drop_column("lease_expires_at")
        batch.drop_column("max_attempts")
        batch.drop_column("attempts")
    with op.batch_alter_table("workspaces") as batch:
        batch.drop_constraint("uq_workspace_tenant_idempotency", type_="unique")
        batch.drop_column("idempotency_key")
