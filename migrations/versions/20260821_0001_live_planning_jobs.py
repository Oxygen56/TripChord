"""Persist the live planning job control plane and worker lease fencing."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260821_0001"
down_revision: str | None = "20260807_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "live_planning_jobs",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=100), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("command_spec", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column("lease_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_live_planning_job_tenant_idempotency"
        ),
    )
    op.create_index(
        "ix_live_planning_jobs_tenant_updated",
        "live_planning_jobs",
        ["tenant_id", "updated_at"],
    )
    op.create_index("ix_live_planning_jobs_tenant_id", "live_planning_jobs", ["tenant_id"])
    op.create_index("ix_live_planning_jobs_state", "live_planning_jobs", ["state"])
    op.create_table(
        "live_planning_pair_results",
        sa.Column("tenant_id", sa.String(length=100), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("date_pair_id", sa.String(length=200), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("checkpoint", sa.JSON(), nullable=False),
        sa.Column("execution", sa.JSON(), nullable=False),
        sa.Column("execution_sha256", sa.String(length=64), nullable=False),
        sa.Column("lease_owner", sa.String(length=200), nullable=False),
        sa.Column("lease_generation", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "job_id", "date_pair_id"),
    )
    with op.batch_alter_table("live_planning_pair_results") as batch:
        batch.create_unique_constraint(
            "uq_live_pair_result_identity", ["tenant_id", "job_id", "date_pair_id"]
        )
    op.create_index(
        "ix_live_pair_results_job_sequence",
        "live_planning_pair_results",
        ["tenant_id", "job_id", "sequence"],
    )


def downgrade() -> None:
    op.drop_index("ix_live_pair_results_job_sequence", table_name="live_planning_pair_results")
    with op.batch_alter_table("live_planning_pair_results") as batch:
        batch.drop_constraint("uq_live_pair_result_identity", type_="unique")
    op.drop_table("live_planning_pair_results")
    op.drop_index("ix_live_planning_jobs_state", table_name="live_planning_jobs")
    op.drop_index("ix_live_planning_jobs_tenant_id", table_name="live_planning_jobs")
    op.drop_index("ix_live_planning_jobs_tenant_updated", table_name="live_planning_jobs")
    op.drop_table("live_planning_jobs")
