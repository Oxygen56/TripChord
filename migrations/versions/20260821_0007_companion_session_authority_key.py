"""Scope Companion session identity by authority partition."""

from collections.abc import Sequence

from alembic import op

revision: str = "20260821_0007"
down_revision: str | None = "20260821_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "browser_companion_sessions_pkey",
        "browser_companion_sessions",
        type_="primary",
    )
    op.create_primary_key(
        "browser_companion_sessions_pkey",
        "browser_companion_sessions",
        ["authority_partition_sha256", "id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "browser_companion_sessions_pkey",
        "browser_companion_sessions",
        type_="primary",
    )
    op.create_primary_key(
        "browser_companion_sessions_pkey",
        "browser_companion_sessions",
        ["id"],
    )
