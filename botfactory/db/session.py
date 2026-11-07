"""Async database engine / session helper."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import event, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base


def _add_missing_columns(conn) -> list[str]:
    """Additive schema sync: ``create_all`` never alters existing tables, so columns added in
    newer versions are appended with ``ALTER TABLE`` (safe on SQLite and PostgreSQL)."""
    insp = inspect(conn)
    existing = set(insp.get_table_names())
    added: list[str] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            if not col.nullable and col.server_default is None:
                raise RuntimeError(f"cannot add NOT NULL column {table.name}.{col.name} without a server default")
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(dialect=conn.dialect)}'
            if col.server_default is not None:
                arg = col.server_default.arg
                lit = "'" + arg.replace("'", "''") + "'" if isinstance(arg, str) else str(arg.compile(dialect=conn.dialect))
                ddl += f" DEFAULT {lit}"
            if not col.nullable:
                ddl += " NOT NULL"
            conn.execute(text(ddl))
            added.append(f"{table.name}.{col.name}")
    return added


# Tables that hold only temporary data and whose primary key changed: they are dropped and recreated
# (``ALTER TABLE`` cannot change a primary key). {table: column that identifies the new layout}
_RECREATE_IF_MISSING = {"cart_items": "variant_id"}


def _recreate_changed_tables(conn) -> list[str]:
    insp = inspect(conn)
    dropped = []
    for table, col in _RECREATE_IF_MISSING.items():
        if insp.has_table(table) and col not in {c["name"] for c in insp.get_columns(table)}:
            conn.execute(text(f'DROP TABLE "{table}"'))
            dropped.append(table)
    return dropped


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

    async def create_all(self) -> list[str]:
        """Create missing tables and add missing columns; returns the columns added."""
        async with self.engine.begin() as conn:
            recreated = await conn.run_sync(_recreate_changed_tables)
            await conn.run_sync(Base.metadata.create_all)
            return [f"{t} (recreated)" for t in recreated] + await conn.run_sync(_add_missing_columns)

    async def dispose(self) -> None:
        await self.engine.dispose()
