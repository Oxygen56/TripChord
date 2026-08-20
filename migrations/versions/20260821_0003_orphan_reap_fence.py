"""Fence durable jobs while an orphan worker is being reaped."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260821_0003"
down_revision: str | None = "20260821_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "live_planning_jobs",
        sa.Column("reap_owner", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "live_planning_jobs",
        sa.Column("reap_generation", sa.Integer(), nullable=True),
    )
    op.add_column(
        "live_planning_jobs",
        sa.Column("reap_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "live_planning_jobs",
        sa.Column("reap_target_owner", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "live_planning_jobs",
        sa.Column("reap_target_generation", sa.Integer(), nullable=True),
    )
    op.add_column(
        "live_planning_jobs",
        sa.Column("reap_controller", sa.String(length=200), nullable=True),
    )
    op.add_column("live_planning_jobs", sa.Column("reap_pgid", sa.Integer(), nullable=True))
    op.add_column(
        "live_planning_jobs",
        sa.Column("reap_marker_digest", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "live_planning_jobs", sa.Column("reap_proof_kind", sa.String(length=30), nullable=True)
    )
    op.add_column(
        "live_planning_jobs",
        sa.Column("reap_proof_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "live_planning_jobs",
        sa.Column("reap_authenticated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "live_planning_jobs",
        sa.Column("reap_death_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("live_planning_jobs", "reap_controller")
    op.drop_column("live_planning_jobs", "reap_death_confirmed_at")
    op.drop_column("live_planning_jobs", "reap_authenticated_at")
    op.drop_column("live_planning_jobs", "reap_proof_verified_at")
    op.drop_column("live_planning_jobs", "reap_proof_kind")
    op.drop_column("live_planning_jobs", "reap_marker_digest")
    op.drop_column("live_planning_jobs", "reap_pgid")
    op.drop_column("live_planning_jobs", "reap_target_generation")
    op.drop_column("live_planning_jobs", "reap_target_owner")
    op.drop_column("live_planning_jobs", "reap_expires_at")
    op.drop_column("live_planning_jobs", "reap_generation")
    op.drop_column("live_planning_jobs", "reap_owner")
