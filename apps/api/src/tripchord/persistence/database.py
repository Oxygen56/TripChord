from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event
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
        memory_sqlite_urls = {"sqlite+aiosqlite://", "sqlite+aiosqlite:///:memory:"}
        if url in memory_sqlite_urls:
            engine_options["poolclass"] = StaticPool
        elif url.startswith("sqlite+aiosqlite:///"):
            # The live planner writes browser-task and long-job state from
            # several concurrent coroutines.  SQLite's default rollback journal
            # makes even an unrelated reader fail while one of those writers is
            # committing.  WAL keeps reads available, while the connection
            # timeout lets the remaining single-writer sections queue instead
            # of surfacing a transient "database is locked" as a failed trip.
            engine_options["connect_args"] = {"timeout": 30.0}
        self.engine: AsyncEngine = create_async_engine(url, **engine_options)
        if url.startswith("sqlite+aiosqlite:///") and url not in memory_sqlite_urls:

            @event.listens_for(self.engine.sync_engine, "connect")
            def configure_file_sqlite(dbapi_connection: object, _: object) -> None:
                cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
                try:
                    cursor.execute("PRAGMA busy_timeout=30000")
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.execute("PRAGMA synchronous=NORMAL")
                    cursor.execute("PRAGMA foreign_keys=ON")
                finally:
                    cursor.close()
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_schema(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
