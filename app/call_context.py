"""Build Bolna user_data payloads. All keys are strings and always present."""

from __future__ import annotations

import os
from typing import Any, Optional

from psycopg import AsyncConnection

from app.config import get_settings


def customer_first_name(name: Optional[str]) -> str:
    """First token, title-cased. Empty -> 'there'. 'John Doe' -> 'John'."""
    if name is None:
        return "there"
    s = str(name).strip()
    if not s:
        return "there"
    token = s.split()[0]
    if not token:
        return "there"
    return token[:1].upper() + token[1:]


def referrer_first_name(name: Optional[str]) -> str:
    return customer_first_name(name)


async def resolve_agent(conn: AsyncConnection, playbook_key: str) -> dict[str, Any]:
    cur = await conn.execute(
        "SELECT agent_id_env_var FROM playbooks WHERE key = %s",
        (playbook_key,),
    )
    row = await cur.fetchone()
    if not row:
        return {"agent_env": "", "agent_id": "", "agent_id_configured": False}
    env_name = row["agent_id_env_var"]
    settings = get_settings()
    mapping = {
        "BOLNA_AGENT_ID_REACTIVATION": settings.bolna_agent_id_reactivation,
        "BOLNA_AGENT_ID_REFERRAL": settings.bolna_agent_id_referral,
    }
    agent_id = mapping.get(env_name, "") or ""
    if not agent_id and env_name:
        agent_id = os.environ.get(env_name, "") or ""
    return {
        "agent_env": env_name,
        "agent_id": agent_id,
        "agent_id_configured": bool(agent_id),
    }


async def build_user_data(
    conn: AsyncConnection,
    *,
    user_id: str,
    playbook_key: str,
    stage_key: str,
    hint_text: str,
) -> dict[str, str]:
    """All string keys, always present. Never mention language or sensitive fields."""
    cur = await conn.execute(
        "SELECT name, referrer_user_id FROM users WHERE id = %s",
        (user_id,),
    )
    user = await cur.fetchone()
    name = user["name"] if user else None
    customer = customer_first_name(name)

    cur = await conn.execute(
        """
        SELECT label, goal FROM playbook_stages
         WHERE playbook_key = %s AND stage_key = %s
        """,
        (playbook_key, stage_key),
    )
    stage = await cur.fetchone()
    stage_label = (stage["label"] if stage else stage_key) or stage_key
    goal = (stage["goal"] if stage else "") or ""
    hint = hint_text or ""

    if playbook_key == "referral":
        ref_first = "there"
        if user and user["referrer_user_id"]:
            cur = await conn.execute(
                "SELECT name FROM users WHERE id = %s",
                (user["referrer_user_id"],),
            )
            ref = await cur.fetchone()
            ref_first = referrer_first_name(ref["name"] if ref else None)
        return {
            "Customer_Name": customer,
            "referrer_first_name": ref_first,
            "stage_label": str(stage_label),
            "goal": str(goal),
            "script_hint": str(hint),
        }

    return {
        "Customer_Name": customer,
        "stage_label": str(stage_label),
        "goal": str(goal),
        "script_hint": str(hint),
    }


async def call_preview(conn: AsyncConnection, user_id: str) -> dict[str, Any]:
    """Preview payload for an open stall (or current stage) without placing a call."""
    cur = await conn.execute(
        """
        SELECT s.id, s.playbook_key, s.stage_key
          FROM stalls s
         WHERE s.user_id = %s AND s.status = 'open'
         ORDER BY s.opened_at DESC
         LIMIT 1
        """,
        (user_id,),
    )
    stall = await cur.fetchone()
    if stall:
        playbook_key = stall["playbook_key"]
        stage_key = stall["stage_key"]
    else:
        cur = await conn.execute(
            "SELECT current_stage, ref_status, referrer_user_id FROM users WHERE id = %s",
            (user_id,),
        )
        u = await cur.fetchone()
        if not u or not u["current_stage"]:
            raise LookupError("no open stall or current stage")
        playbook_key = (
            "referral"
            if u["ref_status"] == "resolved" and u["referrer_user_id"]
            else "reactivation"
        )
        stage_key = u["current_stage"]

    cur = await conn.execute(
        """
        SELECT text FROM script_hints
         WHERE playbook_key = %s AND stage_key = %s
         ORDER BY hint_index ASC LIMIT 1
        """,
        (playbook_key, stage_key),
    )
    h = await cur.fetchone()
    hint_text = h["text"] if h else ""
    user_data = await build_user_data(
        conn,
        user_id=user_id,
        playbook_key=playbook_key,
        stage_key=stage_key,
        hint_text=hint_text,
    )
    agent = await resolve_agent(conn, playbook_key)
    return {
        "agent_env": agent["agent_env"],
        "agent_id_configured": agent["agent_id_configured"],
        "playbook": playbook_key,
        "user_data": user_data,
    }
