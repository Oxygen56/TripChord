"""Add the durable browser acquisition task table."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260821_0006"
down_revision: str | None = "20260821_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "browser_acquisitions",
        sa.Column("id", sa.String(length=80), primary_key=True),
        sa.Column("active_singleflight_key", sa.String(length=220), nullable=True),
        sa.Column("public_task_id", sa.String(length=100), nullable=True),
        sa.Column("authority_partition_sha256", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=100), nullable=False),
        sa.Column("tenant_partition", sa.String(length=200), nullable=False),
        sa.Column("reference_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("inflight_coalesced_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("companion_id", sa.String(length=128), nullable=True),
        sa.Column("session_generation", sa.Integer(), nullable=True),
        sa.Column("session_id", sa.String(length=120), nullable=True),
        sa.Column("runtime_instance_id", sa.String(length=128), nullable=True),
        sa.Column("build_identity", sa.JSON(), nullable=True),
        sa.Column("fingerprint_sha256", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("submission", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column("claim_consumer_id", sa.String(length=100), nullable=True),
        sa.Column("lease_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claim_token_sha256", sa.String(length=64), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quotes", sa.JSON(), nullable=False),
        sa.Column("source_receipt", sa.JSON(), nullable=True),
        sa.Column("failure", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reused_from_task_id", sa.String(length=100), nullable=True),
        sa.Column("reuse_age_seconds", sa.Float(), nullable=True),
    )
    op.create_index(
        "uq_browser_acquisitions_active_singleflight",
        "browser_acquisitions",
        ["active_singleflight_key"],
        unique=True,
    )
    for name, table, columns in (
        (
            "ix_browser_acquisitions_authority_partition_sha256",
            "browser_acquisitions",
            ["authority_partition_sha256"],
        ),
        ("ix_browser_acquisitions_state", "browser_acquisitions", ["state"]),
        ("ix_browser_acquisitions_tenant_id", "browser_acquisitions", ["tenant_id"]),
        ("ix_browser_acquisitions_tenant_partition", "browser_acquisitions", ["tenant_partition"]),
    ):
        op.create_index(name, table, columns)
    op.create_index(
        "ix_browser_acquisitions_lookup",
        "browser_acquisitions",
        ["tenant_partition", "fingerprint_sha256"],
    )
    op.create_index(
        "ix_browser_acquisitions_claimable",
        "browser_acquisitions",
        ["state", "lease_expires_at"],
    )
    op.create_table(
        "browser_task_consumers",
        sa.Column("id", sa.String(length=100), primary_key=True),
        sa.Column("authority_partition_sha256", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=100), nullable=False),
        sa.Column("acquisition_id", sa.String(length=80), nullable=False),
        sa.Column("job_id", sa.String(length=120), nullable=True),
        sa.Column("request_sha256", sa.String(length=64), nullable=True),
        sa.Column("run_id", sa.String(length=120), nullable=True),
        sa.Column("run_revision", sa.Integer(), nullable=True),
        sa.Column("capability", sa.JSON(), nullable=True),
        sa.Column("binding_receipt", sa.JSON(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reused_from_task_id", sa.String(length=100), nullable=True),
        sa.Column("reuse_age_seconds", sa.Float(), nullable=True),
    )
    op.create_index(
        "ix_browser_task_consumers_acquisition",
        "browser_task_consumers",
        ["acquisition_id"],
    )
    op.create_index(
        "ix_browser_task_consumers_tenant",
        "browser_task_consumers",
        ["tenant_id", "created_at"],
    )
    for name, columns in (
        ("ix_browser_task_consumers_acquisition_id", ["acquisition_id"]),
        ("ix_browser_task_consumers_authority_partition_sha256", ["authority_partition_sha256"]),
        ("ix_browser_task_consumers_state", ["state"]),
        ("ix_browser_task_consumers_tenant_id", ["tenant_id"]),
    ):
        op.create_index(name, "browser_task_consumers", columns)
    op.create_table(
        "browser_companion_sessions",
        sa.Column("id", sa.String(length=120), primary_key=True),
        sa.Column("authority_partition_sha256", sa.String(length=64), nullable=False),
        sa.Column("companion_id", sa.String(length=128), nullable=False),
        sa.Column("session_generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("runtime_instance_id", sa.String(length=128), nullable=True),
        sa.Column("build_identity", sa.JSON(), nullable=True),
        sa.Column("providers", sa.JSON(), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("adapter_version", sa.String(length=100), nullable=True),
        sa.Column("contract_version", sa.String(length=100), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_browser_companion_sessions_active",
        "browser_companion_sessions",
        ["authority_partition_sha256", "companion_id"],
    )
    op.create_index(
        "ix_browser_companion_sessions_authority_partition_sha256",
        "browser_companion_sessions",
        ["authority_partition_sha256"],
    )
def downgrade() -> None:
    op.drop_index(
        "ix_browser_companion_sessions_authority_partition_sha256",
        table_name="browser_companion_sessions",
    )
    op.drop_index("ix_browser_task_consumers_tenant", table_name="browser_task_consumers")
    for name in (
        "ix_browser_task_consumers_acquisition_id",
        "ix_browser_task_consumers_authority_partition_sha256",
        "ix_browser_task_consumers_state",
        "ix_browser_task_consumers_tenant_id",
    ):
        op.drop_index(name, table_name="browser_task_consumers")
    op.drop_index("ix_browser_task_consumers_acquisition", table_name="browser_task_consumers")
    op.drop_table("browser_task_consumers")
    op.drop_index("ix_browser_companion_sessions_active", table_name="browser_companion_sessions")
    op.drop_table("browser_companion_sessions")
    op.drop_index("ix_browser_acquisitions_claimable", table_name="browser_acquisitions")
    op.drop_index("ix_browser_acquisitions_lookup", table_name="browser_acquisitions")
    op.drop_index("uq_browser_acquisitions_active_singleflight", table_name="browser_acquisitions")
    for name in (
        "ix_browser_acquisitions_authority_partition_sha256",
        "ix_browser_acquisitions_state",
        "ix_browser_acquisitions_tenant_id",
        "ix_browser_acquisitions_tenant_partition",
    ):
        op.drop_index(name, table_name="browser_acquisitions")
    op.drop_table("browser_acquisitions")
