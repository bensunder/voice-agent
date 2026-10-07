"""Postgres access: async connection pool and a forward-only migration runner."""

from __future__ import annotations

import logging
from importlib import resources
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

log = logging.getLogger(__name__)

MIGRATION_LOCK_ID = 7_301_552_018  # arbitrary, stable advisory-lock key


def make_pool(dsn: str, min_size: int = 1, max_size: int = 10) -> AsyncConnectionPool:
    return AsyncConnectionPool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        kwargs={"row_factory": dict_row, "autocommit": False},
        open=False,
        name="salesagent",
        timeout=10,
    )


async def migrate(dsn: str) -> list[str]:
    """Apply pending migrations exactly once, safe under concurrent starts."""
    files = sorted(
        f.name for f in resources.files("salesagent.migrations").iterdir() if f.name.endswith(".sql")
    )
    applied_now: list[str] = []
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_ID,))
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            cur = await conn.execute("SELECT name FROM schema_migrations")
            done = {r[0] for r in await cur.fetchall()}
            for name in files:
                if name in done:
                    continue
                sql = resources.files("salesagent.migrations").joinpath(name).read_text()
                async with conn.transaction():
                    await conn.execute(sql)  # type: ignore[arg-type]
                    await conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
                applied_now.append(name)
                log.info("applied migration %s", name)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_ID,))
    return applied_now


async def audit(
    conn: psycopg.AsyncConnection[Any],
    kind: str,
    *,
    lead_id: Any = None,
    attempt_id: Any = None,
    detail: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        "INSERT INTO audit_event (lead_id, attempt_id, kind, detail) VALUES (%s, %s, %s, %s)",
        (lead_id, attempt_id, kind, Jsonb(detail or {})),
    )


async def enqueue(
    conn: psycopg.AsyncConnection[Any], topic: str, dedupe_key: str, payload: dict[str, Any]
) -> None:
    """Transactional outbox: written in the same transaction as the state change."""
    await conn.execute(
        "INSERT INTO outbox (topic, dedupe_key, payload) VALUES (%s, %s, %s)"
        " ON CONFLICT (dedupe_key) DO NOTHING",
        (topic, dedupe_key, Jsonb(payload)),
    )
