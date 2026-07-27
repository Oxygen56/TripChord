from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from tripchord.persistence.models import Base


class Database:
    def __init__(self, url: str, *, echo: bool = False) -> None:
        engine_options: dict[str, object] = {"echo": echo}
        if url in {"sqlite+aiosqlite://", "sqlite+aiosqlite:///:memory:"}:
            engine_options["poolclass"] = StaticPool
        self.engine: AsyncEngine = create_async_engine(url, **engine_options)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_schema(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
