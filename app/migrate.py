"""Tiny idempotent migration runner: schema_migrations + advisory lock."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from app.config import get_settings

log = logging.getLogger("rho.migrate")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
# Fixed advisory lock key for migrations (arbitrary stable int64).
_LOCK_KEY = 0x52484F5F4D4947  # 'RHO_MIG' truncated


def _list_migrations() -> list[Path]:
    if not MIGRATIONS_DIR.is_dir():
        return []
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def apply_migrations_sync(conninfo: str | None = None) -> list[str]:
    """Apply pending *.sql migrations under an advisory lock. Returns applied filenames."""
    settings = get_settings()
    url = conninfo or settings.database_url
    applied: list[str] = []
    with psycopg.connect(url, row_factory=dict_row, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
            try:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        filename TEXT PRIMARY KEY,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                conn.commit()
                for path in _list_migrations():
                    name = path.name
                    cur.execute(
                        "SELECT 1 FROM schema_migrations WHERE filename = %s",
                        (name,),
                    )
                    if cur.fetchone():
                        continue
                    sql = path.read_text(encoding="utf-8")
                    log.info("applying migration %s", name)
                    cur.execute(sql)
                    cur.execute(
                        "INSERT INTO schema_migrations (filename) VALUES (%s)",
                        (name,),
                    )
                    conn.commit()
                    applied.append(name)
            finally:
                cur.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
                conn.commit()
    return applied


async def apply_migrations() -> list[str]:
    return await asyncio.to_thread(apply_migrations_sync)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    applied = apply_migrations_sync()
    if applied:
        print("applied:", ", ".join(applied))
    else:
        print("no pending migrations")


if __name__ == "__main__":
    main()
