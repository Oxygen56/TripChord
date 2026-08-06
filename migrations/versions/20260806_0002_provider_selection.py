"""Persist provider selection in the database (v0.2 deviation)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260806_0002"
down_revision: str | None = "20260806_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_selection",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(length=100), nullable=False),
        sa.Column("scope_key", sa.String(length=160), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "scope_key",
            name="uq_provider_selection_tenant_scope",
        ),
    )
    op.create_index(
        "ix_provider_selection_tenant_id",
        "provider_selection",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_provider_selection_tenant_id", table_name="provider_selection")
    op.drop_table("provider_selection")
