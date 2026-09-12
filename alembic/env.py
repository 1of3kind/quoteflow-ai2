"""Alembic environment: supports sync and async database URLs.

Reads DATABASE_URL from the environment (falling back to alembic.ini) so
migrations run against staging/production without editing config.
"""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from core.database import Base, normalize_database_url

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    url = os.getenv("DATABASE_URL", "").strip() or config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("DATABASE_URL is not set; cannot run migrations")
    # Normalize Render-style URLs to asyncpg; alembic env handles async URLs below.
    return normalize_database_url(url)


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_sync_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata,
                      compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def _run_async_migrations() -> None:
    connectable = async_engine_from_config(
        {"sqlalchemy.url": _url()}, prefix="sqlalchemy.", poolclass=pool.NullPool)

    async def _connect_and_migrate() -> None:
        async with connectable.connect() as connection:
            await connection.run_sync(_run_sync_migrations)

    asyncio.run(_connect_and_migrate())
    _await_connectable_dispose(connectable)


def _await_connectable_dispose(connectable) -> None:
    asyncio.run(connectable.dispose())


def run_migrations_online() -> None:
    if _url().startswith("postgresql+asyncpg://") or _url().startswith("sqlite+aiosqlite://"):
        _run_async_migrations()
    else:
        from sqlalchemy import create_engine
        engine = create_engine(_url(), poolclass=pool.NullPool)
        with engine.connect() as connection:
            _run_sync_migrations(connection)
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
