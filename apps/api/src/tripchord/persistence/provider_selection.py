"""DB-backed persistence for per-scope provider selection (v0.2 deviation).

The v0.2 kernel shipped with an atomic ``.runtime/provider-selection.json``
store.  This repository is the database replacement so selection survives
migration and is tenant-scoped; the JSON store remains as a read-only fallback
for very old local installs that predate the table.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripchord.persistence.models import ProviderSelectionRow, utc_now


class ProviderSelectionRepository:
    def __init__(self, session: AsyncSession, tenant_id: str = "anonymous") -> None:
        self._session = session
        self._tenant_id = tenant_id

    async def load_all(self) -> dict[str, bool]:
        rows = (
            await self._session.scalars(
                select(ProviderSelectionRow).where(
                    ProviderSelectionRow.tenant_id == self._tenant_id
                )
            )
        ).all()
        return {row.scope_key: bool(row.enabled) for row in rows}

    async def set_enabled(self, scope_key: str, enabled: bool) -> dict[str, bool]:
        row = await self._session.scalar(
            select(ProviderSelectionRow).where(
                ProviderSelectionRow.tenant_id == self._tenant_id,
                ProviderSelectionRow.scope_key == scope_key,
            )
        )
        now = utc_now()
        if row is None:
            row = ProviderSelectionRow(
                tenant_id=self._tenant_id,
                scope_key=scope_key,
                enabled=enabled,
                updated_at=now,
            )
            self._session.add(row)
        else:
            row.enabled = enabled
            row.updated_at = now
        await self._session.commit()
        return await self.load_all()
