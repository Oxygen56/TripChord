"""Add tenant ownership to persistent workspaces."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260727_0002"
down_revision: str | None = "20260727_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workspaces") as batch:
        batch.add_column(
            sa.Column(
                "tenant_id",
                sa.String(length=100),
                nullable=False,
                server_default="anonymous",
            )
        )
        batch.create_index("ix_workspaces_tenant_id", ["tenant_id"])
        batch.alter_column("tenant_id", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("workspaces") as batch:
        batch.drop_index("ix_workspaces_tenant_id")
        batch.drop_column("tenant_id")
