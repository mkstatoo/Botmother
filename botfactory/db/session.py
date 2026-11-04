"""Async database engine / session helper."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base


class Database:
    def __init__(self, url: str) -> None:
        parsed = make_url(url)
        self.is_sqlite = parsed.get_backend_name() == "sqlite"
        if self.is_sqlite and parsed.database and parsed.database != ":memory:":
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)

        kwargs: dict = {"pool_pre_ping": True}
        if self.is_sqlite:
            kwargs["connect_args"] = {"timeout": 30}
        self.engine: AsyncEngine = create_async_engine(url, **kwargs)

        if self.is_sqlite:

            @event.listens_for(self.engine.sync_engine, "connect")
            def _pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.close()

        self._maker = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    def session(self) -> AsyncSession:
        """Use as ``async with db.session() as s:``."""
        return self._maker()

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()
