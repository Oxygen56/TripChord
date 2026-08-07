"""Persist live-quote monitor status and check history (v0.9).

Before this migration an opt-in live monitor lived only in the process-local
registry, so a restart dropped its status and history.  The new tables make the
monitor record recoverable: status columns plus the boundary text, and an
append-only check history per monitor.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260807_0001"
down_revision: str | None = "20260806_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "live_monitors",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=100), nullable=False),
        sa.Column("run_id", sa.String(length=100), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("max_checks", sa.Integer(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("check_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("boundary", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index(
        "ix_live_monitors_tenant_id",
        "live_monitors",
        ["tenant_id"],
    )
    op.create_index(
        "ix_live_monitors_run_id",
        "live_monitors",
        ["run_id"],
    )
    op.create_index(
        "ix_live_monitors_tenant_created",
        "live_monitors",
        ["tenant_id", "created_at"],
    )
    op.create_table(
        "live_monitor_checks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "monitor_id",
            sa.String(length=64),
            sa.ForeignKey("live_monitors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("target_component_id", sa.String(length=200), nullable=False),
        sa.Column("event_id", sa.String(length=200), nullable=False),
        sa.Column("applied_disposition", sa.String(length=60), nullable=True),
        sa.Column("decision_state", sa.String(length=60), nullable=False),
        sa.Column("package_changed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index(
        "ix_live_monitor_checks_monitor_id",
        "live_monitor_checks",
        ["monitor_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_live_monitor_checks_monitor_id", table_name="live_monitor_checks")
    op.drop_table("live_monitor_checks")
    op.drop_index("ix_live_monitors_tenant_created", table_name="live_monitors")
    op.drop_index("ix_live_monitors_run_id", table_name="live_monitors")
    op.drop_index("ix_live_monitors_tenant_id", table_name="live_monitors")
    op.drop_table("live_monitors")
