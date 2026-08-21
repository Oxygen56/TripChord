"""Freeze formal browser completion before its ledger side effect."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260821_0008"
down_revision: str | None = "20260821_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "browser_acquisitions", sa.Column("completion_payload", sa.JSON(), nullable=True)
    )
    op.add_column(
        "browser_acquisitions", sa.Column("completion_receipt", sa.JSON(), nullable=True)
    )
    op.add_column(
        "browser_acquisitions", sa.Column("completion_snapshot", sa.JSON(), nullable=True)
    )
    op.add_column(
        "browser_acquisitions",
        sa.Column("completion_event_details", sa.JSON(), nullable=True),
    )
    op.add_column(
        "browser_acquisitions",
        sa.Column("completion_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "browser_acquisitions",
        sa.Column("completion_published_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "browser_acquisitions",
        sa.Column("completion_published_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("browser_acquisitions", "completion_published_at")
    op.drop_column("browser_acquisitions", "completion_published_sha256")
    op.drop_column("browser_acquisitions", "completion_sha256")
    op.drop_column("browser_acquisitions", "completion_event_details")
    op.drop_column("browser_acquisitions", "completion_receipt")
    op.drop_column("browser_acquisitions", "completion_snapshot")
    op.drop_column("browser_acquisitions", "completion_payload")
