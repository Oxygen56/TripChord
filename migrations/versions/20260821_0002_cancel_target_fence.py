"""Persist the exact lease identity revoked by cancellation."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260821_0002"
down_revision: str | None = "20260821_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "live_planning_jobs",
        sa.Column("cancel_target_owner", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "live_planning_jobs",
        sa.Column("cancel_target_generation", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("live_planning_jobs", "cancel_target_generation")
    op.drop_column("live_planning_jobs", "cancel_target_owner")
