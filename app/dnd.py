"""DND check — reads only our dnd_numbers table (swappable later)."""

from __future__ import annotations

from psycopg import AsyncConnection

from app.phones import normalize_phone


async def dnd_check(conn: AsyncConnection, phone: str | None) -> bool:
    """Return True if the phone is on our DND list. Normalises before lookup."""
    e164 = normalize_phone(phone)
    if not e164:
        return False
    row = await conn.execute(
        "SELECT 1 FROM dnd_numbers WHERE phone = %s LIMIT 1",
        (e164,),
    )
    return (await row.fetchone()) is not None
