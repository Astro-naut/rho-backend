"""Bolna webhook verification + handling."""

from __future__ import annotations

import hmac
import logging
from typing import Any, Optional

from psycopg import AsyncConnection

from app.bolna_client import update_call_from_execution
from app.config import get_settings

log = logging.getLogger("rho.webhooks")


def verify_webhook_secret(
    header_secret: Optional[str],
    query_secret: Optional[str],
) -> bool:
    settings = get_settings()
    expected = (settings.bolna_webhook_secret or "").encode("utf-8")
    if not expected:
        # Misconfigured — reject
        return False
    candidates = [header_secret, query_secret]
    for c in candidates:
        if c is None:
            continue
        got = c.encode("utf-8")
        if hmac.compare_digest(got, expected):
            return True
    return False


def _extract_execution_id(payload: dict[str, Any]) -> Optional[str]:
    for key in ("execution_id", "id"):
        if payload.get(key):
            return str(payload[key])
    data = payload.get("data") or {}
    if isinstance(data, dict):
        for key in ("execution_id", "id"):
            if data.get(key):
                return str(data[key])
    return None


async def handle_bolna_webhook(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    execution_id = _extract_execution_id(payload)
    if not execution_id:
        return {"ok": False, "error": "missing execution_id/id"}

    cur = await conn.execute(
        "SELECT id FROM calls WHERE execution_id = %s",
        (execution_id,),
    )
    row = await cur.fetchone()
    if not row:
        # Also try matching payload id against calls.id if numeric? No — stick to execution_id.
        log.warning("webhook for unknown execution_id=%s", execution_id)
        return {"ok": False, "error": "unknown execution", "execution_id": execution_id}

    call_id = int(row["id"])
    # Merge top-level + data
    data = dict(payload)
    if isinstance(payload.get("data"), dict):
        data = {**payload["data"], **payload}
    fields = await update_call_from_execution(conn, call_id, data)
    return {"ok": True, "call_id": call_id, "execution_id": execution_id, "status": fields.get("status")}
