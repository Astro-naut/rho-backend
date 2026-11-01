"""Team phone allowlist: seed from env, admin CRUD, audit log.

Real outbound calls (later batches) require DRY_RUN=0 AND phone in this table.
"""

from __future__ import annotations

from typing import Any, Optional

from psycopg import AsyncConnection

from app.config import get_settings
from app.phones import normalize_phone


def _jsonable(obj: Any) -> Any:
    import json
    from datetime import date, datetime

    def default(o):  # noqa: ANN001
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        return str(o)

    if obj is None:
        return None
    return json.loads(json.dumps(obj, default=default))


async def _audit(
    conn: AsyncConnection,
    *,
    action: str,
    entity_id: str | None,
    before: Any,
    after: Any,
    who: str = "admin",
) -> None:
    import json

    await conn.execute(
        """
        INSERT INTO admin_audit_log (who, action, entity, entity_id, before, after)
        VALUES (%s, %s, 'team_phones', %s, %s::jsonb, %s::jsonb)
        """,
        (
            who,
            action,
            entity_id,
            None if before is None else json.dumps(_jsonable(before)),
            None if after is None else json.dumps(_jsonable(after)),
        ),
    )


async def seed_team_phones_from_env(conn: AsyncConnection) -> int:
    """Idempotent seed of TEAM_PHONES (+ deprecated TEST_PHONE_ALLOWLIST) into team_phones."""
    settings = get_settings()
    inserted = 0
    for raw in settings.team_phone_seed_list():
        e164 = normalize_phone(raw)
        if not e164:
            continue
        row = await conn.execute(
            """
            INSERT INTO team_phones (phone, label, source)
            VALUES (%s, %s, 'env_seed')
            ON CONFLICT (phone) DO NOTHING
            RETURNING phone
            """,
            (e164, "env seed"),
        )
        if await row.fetchone():
            inserted += 1
            await _audit(
                conn,
                action="team_phone.seed",
                entity_id=e164,
                before=None,
                after={"phone": e164, "source": "env_seed"},
                who="startup",
            )
    return inserted


async def list_team_phones(conn: AsyncConnection) -> list[dict]:
    cur = await conn.execute(
        "SELECT phone, label, source, created_at FROM team_phones ORDER BY created_at"
    )
    return list(await cur.fetchall())


async def add_team_phone(
    conn: AsyncConnection, phone: str, label: Optional[str] = None, who: str = "admin"
) -> dict:
    e164 = normalize_phone(phone)
    if not e164:
        raise ValueError("invalid phone")
    cur = await conn.execute(
        "SELECT phone, label, source, created_at FROM team_phones WHERE phone = %s",
        (e164,),
    )
    existing = await cur.fetchone()
    if existing:
        before = dict(existing)
        await conn.execute(
            """
            UPDATE team_phones
               SET label = COALESCE(%s, label), source = 'admin', updated_at = now()
             WHERE phone = %s
            """,
            (label, e164),
        )
        cur = await conn.execute(
            "SELECT phone, label, source, created_at FROM team_phones WHERE phone = %s",
            (e164,),
        )
        after = dict(await cur.fetchone())
        await _audit(conn, action="team_phone.update", entity_id=e164, before=before, after=after, who=who)
        return after
    await conn.execute(
        """
        INSERT INTO team_phones (phone, label, source)
        VALUES (%s, %s, 'admin')
        """,
        (e164, label),
    )
    cur = await conn.execute(
        "SELECT phone, label, source, created_at FROM team_phones WHERE phone = %s",
        (e164,),
    )
    after = dict(await cur.fetchone())
    await _audit(conn, action="team_phone.create", entity_id=e164, before=None, after=after, who=who)
    return after


async def delete_team_phone(conn: AsyncConnection, phone: str, who: str = "admin") -> bool:
    e164 = normalize_phone(phone)
    if not e164:
        raise ValueError("invalid phone")
    cur = await conn.execute(
        "SELECT phone, label, source, created_at FROM team_phones WHERE phone = %s",
        (e164,),
    )
    existing = await cur.fetchone()
    if not existing:
        return False
    await conn.execute("DELETE FROM team_phones WHERE phone = %s", (e164,))
    await _audit(
        conn,
        action="team_phone.delete",
        entity_id=e164,
        before=dict(existing),
        after=None,
        who=who,
    )
    return True


async def is_team_phone(conn: AsyncConnection, phone: str | None) -> bool:
    e164 = normalize_phone(phone)
    if not e164:
        return False
    cur = await conn.execute("SELECT 1 FROM team_phones WHERE phone = %s", (e164,))
    return (await cur.fetchone()) is not None
