"""Shared fixtures: ephemeral Postgres via pgembed + migrated schema."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import psycopg
import pytest
import pytest_asyncio
from pgembed import get_server

os.environ["DRY_RUN"] = "1"
os.environ["DEV"] = "1"
os.environ["WORKER_ENABLED"] = "0"
os.environ.setdefault("BOLNA_API_KEY", "test-bolna-key")
os.environ.setdefault("BOLNA_AGENT_ID_REACTIVATION", "agent-react")
os.environ.setdefault("BOLNA_AGENT_ID_REFERRAL", "agent-ref")
os.environ.setdefault("BOLNA_WEBHOOK_SECRET", "whsec")
os.environ.setdefault("TEAM_PHONES", "+919876543210")
os.environ.setdefault("ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("TICK_SECRET", "test-tick-secret")
os.environ.setdefault("INR_PER_USD", "88")
os.environ.setdefault("BOLNA_API_BASE", "https://api.bolna.ai")


@pytest.fixture(scope="session")
def pg_uri():
    parent = Path(tempfile.mkdtemp(prefix="rho-pg-"))
    pgdata = parent / "pgdata"
    pgdata.mkdir()
    server = get_server(pgdata, cleanup_mode="delete")
    admin_uri = server.get_uri()  # default postgres db
    with psycopg.connect(admin_uri, autocommit=True) as conn:
        conn.execute("CREATE DATABASE rho_test")
    db_uri = server.get_uri(database="rho_test")
    os.environ["DATABASE_URL"] = db_uri
    from app.config import get_settings

    get_settings.cache_clear()
    from app.migrate import apply_migrations

    asyncio.run(apply_migrations())
    yield db_uri
    try:
        server.cleanup()
    except Exception:
        pass


@pytest_asyncio.fixture
async def db(pg_uri):
    os.environ["DATABASE_URL"] = pg_uri
    from app.config import get_settings
    from app.db import close_pool, get_pool
    from app.team_phones import seed_team_phones_from_env

    get_settings.cache_clear()
    await close_pool()
    pool = await get_pool()
    async with pool.connection() as conn:
        for table in (
            "rewards",
            "calls",
            "decisions",
            "stalls",
            "events",
            "user_stage_state",
            "referral_shares",
            "admin_audit_log",
            "dnd_numbers",
            "bandit_arms",
            "users",
            "team_phones",
        ):
            await conn.execute(f"TRUNCATE {table} CASCADE")
        await seed_team_phones_from_env(conn)
        await conn.execute("UPDATE hint_arms SET alpha=1, beta=1, pulls=0")
        await conn.commit()
        try:
            yield conn
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    await close_pool()
    get_settings.cache_clear()
