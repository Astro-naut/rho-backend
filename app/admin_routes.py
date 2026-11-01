"""Admin-protected policy, playbooks, team-phones, and DND import routes."""

from __future__ import annotations

import csv
import io
import json
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from psycopg.types.json import Jsonb

from app.auth import require_admin
from app.db import get_pool
from app.dnd import dnd_check  # noqa: F401 — exported for later batches
from app.ingest import set_user_dnd
from app.models import (
    DndIn,
    PlaybookPut,
    PolicyIn,
    PolicyOut,
    TeamPhoneIn,
    TeamPhoneOut,
)
from app.phones import normalize_phone
from app.team_phones import add_team_phone, delete_team_phone, list_team_phones

router = APIRouter(tags=["admin"])


def _time_str(v) -> str:
    if v is None:
        return ""
    if hasattr(v, "strftime"):
        return v.strftime("%H:%M")
    return str(v)[:5]


@router.get("/policy", response_model=List[PolicyOut])
async def get_all_policies(_admin: str = Depends(require_admin)):
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT playbook_key, min_score, daily_budget_inr,
                   call_window_start, call_window_end, timezone,
                   per_user_cap, per_user_cap_days, holdout_pct,
                   conversion_value_inr, optout_penalty_inr, reward_window_hours,
                   COALESCE(half_life_minutes, 120) AS half_life_minutes,
                   COALESCE(reserved_call_cost_inr, 15) AS reserved_call_cost_inr,
                   updated_at
              FROM policies ORDER BY playbook_key
            """
        )
        rows = await cur.fetchall()
        return [
            PolicyOut(
                playbook_key=r["playbook_key"],
                min_score=float(r["min_score"]),
                daily_budget_inr=float(r["daily_budget_inr"]),
                call_window_start=_time_str(r["call_window_start"]),
                call_window_end=_time_str(r["call_window_end"]),
                timezone=r["timezone"],
                per_user_cap=int(r["per_user_cap"]),
                per_user_cap_days=int(r["per_user_cap_days"]),
                holdout_pct=float(r["holdout_pct"]),
                conversion_value_inr=float(r["conversion_value_inr"]),
                optout_penalty_inr=float(r["optout_penalty_inr"]),
                reward_window_hours=int(r["reward_window_hours"]),
                half_life_minutes=float(r["half_life_minutes"]) if "half_life_minutes" in r and r["half_life_minutes"] is not None else 120.0,
                reserved_call_cost_inr=float(r["reserved_call_cost_inr"]) if "reserved_call_cost_inr" in r and r["reserved_call_cost_inr"] is not None else 15.0,
                updated_at=r["updated_at"],
            )
            for r in rows
        ]


@router.get("/policy/{playbook_key}", response_model=PolicyOut)
async def get_policy(playbook_key: str, _admin: str = Depends(require_admin)):
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT * FROM policies WHERE playbook_key = %s", (playbook_key,)
        )
        r = await cur.fetchone()
        if not r:
            raise HTTPException(404, "playbook policy not found")
        return PolicyOut(
            playbook_key=r["playbook_key"],
            min_score=float(r["min_score"]),
            daily_budget_inr=float(r["daily_budget_inr"]),
            call_window_start=_time_str(r["call_window_start"]),
            call_window_end=_time_str(r["call_window_end"]),
            timezone=r["timezone"],
            per_user_cap=int(r["per_user_cap"]),
            per_user_cap_days=int(r["per_user_cap_days"]),
            holdout_pct=float(r["holdout_pct"]),
            conversion_value_inr=float(r["conversion_value_inr"]),
            optout_penalty_inr=float(r["optout_penalty_inr"]),
            reward_window_hours=int(r["reward_window_hours"]),
            updated_at=r["updated_at"],
        )


@router.put("/policy/{playbook_key}", response_model=PolicyOut)
async def put_policy(
    playbook_key: str,
    body: PolicyIn,
    _admin: str = Depends(require_admin),
):
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT * FROM policies WHERE playbook_key = %s FOR UPDATE",
                (playbook_key,),
            )
            before = await cur.fetchone()
            if not before:
                raise HTTPException(404, "playbook policy not found")
            fields = body.model_dump(exclude_unset=True)
            if not fields:
                raise HTTPException(400, "no fields to update")
            sets = []
            vals = []
            for k, v in fields.items():
                sets.append(f"{k} = %s")
                vals.append(v)
            sets.append("updated_at = now()")
            vals.append(playbook_key)
            await conn.execute(
                f"UPDATE policies SET {', '.join(sets)} WHERE playbook_key = %s",
                vals,
            )
            await conn.execute(
                """
                INSERT INTO admin_audit_log (who, action, entity, entity_id, before, after)
                VALUES ('admin', 'policy.update', 'policies', %s, %s::jsonb, %s::jsonb)
                """,
                (
                    playbook_key,
                    json.dumps({k: str(before[k]) for k in before.keys()}, default=str),
                    json.dumps(fields, default=str),
                ),
            )
        return await get_policy(playbook_key, _admin)


@router.get("/playbooks")
async def get_playbooks(_admin: str = Depends(require_admin)):
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT key, label, agent_id_env_var, active FROM playbooks ORDER BY key"
        )
        playbooks = await cur.fetchall()
        out = []
        for p in playbooks:
            cur = await conn.execute(
                """
                SELECT stage_key, ordinal, label, goal, threshold_minutes
                  FROM playbook_stages
                 WHERE playbook_key = %s ORDER BY ordinal
                """,
                (p["key"],),
            )
            stages = await cur.fetchall()
            cur = await conn.execute(
                """
                SELECT stage_key, hint_index, text
                  FROM script_hints
                 WHERE playbook_key = %s
                 ORDER BY stage_key, hint_index
                """,
                (p["key"],),
            )
            hints = await cur.fetchall()
            out.append(
                {
                    "key": p["key"],
                    "label": p["label"],
                    "agent_id_env_var": p["agent_id_env_var"],
                    "active": p["active"],
                    "stages": [dict(s) for s in stages],
                    "hints": [dict(h) for h in hints],
                }
            )
        return out


@router.get("/playbooks/{playbook_key}")
async def get_playbook(playbook_key: str, _admin: str = Depends(require_admin)):
    all_p = await get_playbooks(_admin)
    for p in all_p:
        if p["key"] == playbook_key:
            return p
    raise HTTPException(404, "playbook not found")


@router.put("/playbooks/{playbook_key}")
async def put_playbook(
    playbook_key: str,
    body: PlaybookPut,
    _admin: str = Depends(require_admin),
):
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT key, label, agent_id_env_var, active FROM playbooks WHERE key = %s FOR UPDATE",
                (playbook_key,),
            )
            existing = await cur.fetchone()
            if not existing:
                raise HTTPException(404, "playbook not found")
            if body.label is not None or body.agent_id_env_var is not None or body.active is not None:
                await conn.execute(
                    """
                    UPDATE playbooks SET
                        label = COALESCE(%s, label),
                        agent_id_env_var = COALESCE(%s, agent_id_env_var),
                        active = COALESCE(%s, active)
                     WHERE key = %s
                    """,
                    (body.label, body.agent_id_env_var, body.active, playbook_key),
                )
            if body.stages is not None:
                for st in body.stages:
                    await conn.execute(
                        """
                        INSERT INTO playbook_stages
                            (playbook_key, stage_key, ordinal, label, goal, threshold_minutes)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (playbook_key, stage_key) DO UPDATE SET
                            ordinal = EXCLUDED.ordinal,
                            label = EXCLUDED.label,
                            goal = EXCLUDED.goal,
                            threshold_minutes = EXCLUDED.threshold_minutes
                        """,
                        (
                            playbook_key,
                            st.stage_key,
                            st.ordinal,
                            st.label,
                            st.goal,
                            st.threshold_minutes,
                        ),
                    )
            if body.hints is not None:
                for h in body.hints:
                    await conn.execute(
                        """
                        INSERT INTO script_hints (playbook_key, stage_key, hint_index, text)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (playbook_key, stage_key, hint_index) DO UPDATE
                          SET text = EXCLUDED.text
                        """,
                        (playbook_key, h.stage_key, h.hint_index, h.text),
                    )
            await conn.execute(
                """
                INSERT INTO admin_audit_log (who, action, entity, entity_id, before, after)
                VALUES ('admin', 'playbook.update', 'playbooks', %s, %s::jsonb, %s::jsonb)
                """,
                (
                    playbook_key,
                    json.dumps(dict(existing), default=str),
                    json.dumps(body.model_dump(exclude_unset=True), default=str),
                ),
            )
    return await get_playbook(playbook_key, _admin)


@router.get("/admin/team-phones", response_model=List[TeamPhoneOut])
async def admin_list_team_phones(_admin: str = Depends(require_admin)):
    pool = await get_pool()
    async with pool.connection() as conn:
        rows = await list_team_phones(conn)
        return [TeamPhoneOut(**dict(r)) for r in rows]


@router.post("/admin/team-phones", response_model=TeamPhoneOut)
async def admin_add_team_phone(body: TeamPhoneIn, _admin: str = Depends(require_admin)):
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            try:
                row = await add_team_phone(conn, body.phone, body.label, who=_admin)
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
        return TeamPhoneOut(**row)


@router.delete("/admin/team-phones/{phone}")
async def admin_delete_team_phone(phone: str, _admin: str = Depends(require_admin)):
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            try:
                ok = await delete_team_phone(conn, phone, who=_admin)
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
        if not ok:
            raise HTTPException(404, "phone not found")
        return {"ok": True, "deleted": phone}


@router.put("/users/{user_id}/dnd")
async def admin_set_dnd(user_id: str, body: DndIn, _admin: str = Depends(require_admin)):
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            return await set_user_dnd(conn, user_id, body.dnd, who=_admin)


@router.post("/admin/dnd/import")
async def admin_dnd_import(
    request: Request,
    file: Optional[UploadFile] = File(default=None),
    _admin: str = Depends(require_admin),
):
    """Import CSV of numbers into dnd_numbers. Accepts multipart file or text/csv body."""
    raw: str
    if file is not None:
        data = await file.read()
        raw = data.decode("utf-8", errors="replace")
    else:
        ctype = (request.headers.get("content-type") or "").lower()
        body = await request.body()
        if "text/csv" in ctype or "text/plain" in ctype or not ctype.startswith("multipart"):
            raw = body.decode("utf-8", errors="replace")
        else:
            raise HTTPException(400, "provide a CSV file or text/csv body")

    reader = csv.reader(io.StringIO(raw))
    phones: list[str] = []
    for row in reader:
        if not row:
            continue
        cell = row[0].strip()
        if not cell or cell.lower() in {"phone", "number", "msisdn"}:
            continue
        e164 = normalize_phone(cell)
        if e164:
            phones.append(e164)

    pool = await get_pool()
    inserted = 0
    async with pool.connection() as conn:
        async with conn.transaction():
            for p in phones:
                cur = await conn.execute(
                    """
                    INSERT INTO dnd_numbers (phone, source, note)
                    VALUES (%s, 'import', 'csv import')
                    ON CONFLICT (phone) DO NOTHING
                    RETURNING phone
                    """,
                    (p,),
                )
                if await cur.fetchone():
                    inserted += 1
            await conn.execute(
                """
                INSERT INTO admin_audit_log (who, action, entity, entity_id, before, after)
                VALUES ('admin', 'dnd.import', 'dnd_numbers', null, null, %s::jsonb)
                """,
                (json.dumps({"inserted": inserted, "seen": len(phones)}),),
            )
    return {"ok": True, "inserted": inserted, "seen": len(phones)}
