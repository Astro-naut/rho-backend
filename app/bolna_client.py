"""Bolna dial + execution fetch. DRY_RUN and team_phones gate real HTTP."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

import httpx
from psycopg import AsyncConnection

from app.call_context import build_user_data, resolve_agent
from app.config import get_settings
from app.team_phones import is_team_phone

log = logging.getLogger("rho.bolna")

TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "busy",
        "no-answer",
        "no_answer",
        "canceled",
        "cancelled",
        "error",
        "stopped",
        "dry_run",
    }
)


class BolnaError(RuntimeError):
    pass


def _client() -> httpx.AsyncClient:
    settings = get_settings()
    headers = {"Authorization": f"Bearer {settings.bolna_api_key}"}
    return httpx.AsyncClient(
        base_url=settings.bolna_api_base.rstrip("/"),
        headers=headers,
        timeout=30.0,
    )


async def _post_call_with_retries(payload: dict[str, Any], attempts: int = 4) -> dict[str, Any]:
    last_exc: Optional[Exception] = None
    async with _client() as client:
        for i in range(attempts):
            try:
                resp = await client.post("/call", json=payload)
                if resp.status_code >= 500:
                    raise BolnaError(f"bolna 5xx: {resp.status_code} {resp.text[:300]}")
                if resp.status_code >= 400:
                    raise BolnaError(f"bolna {resp.status_code}: {resp.text[:500]}")
                return resp.json()
            except (httpx.TransportError, BolnaError) as e:
                last_exc = e
                if isinstance(e, BolnaError) and "5xx" not in str(e):
                    raise
                delay = 0.5 * (2**i)
                log.warning("bolna retry %s after %s: %s", i + 1, delay, e)
                await asyncio.sleep(delay)
    raise BolnaError(f"bolna failed after retries: {last_exc}")


async def get_execution(execution_id: str) -> dict[str, Any]:
    async with _client() as client:
        resp = await client.get(f"/executions/{execution_id}")
        if resp.status_code >= 400:
            raise BolnaError(f"get_execution {resp.status_code}: {resp.text[:400]}")
        return resp.json()


async def place_call_for_decision(
    conn: AsyncConnection,
    decision: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Idempotent insert of calls row (unique stall_id) BEFORE dialing, then dial or dry_run."""
    settings = get_settings()
    stall_id = decision["stall_id"]
    user_id = decision["user_id"]
    playbook_key = decision["playbook_key"]
    stage_key = decision["stage_key"]
    hint_id = decision.get("hint_id")
    hint_text = decision.get("hint_text") or ""
    decision_id = decision["decision_id"]

    cur = await conn.execute("SELECT phone FROM users WHERE id = %s", (user_id,))
    u = await cur.fetchone()
    if not u or not u["phone"]:
        log.warning("no phone for user %s", user_id)
        return None
    phone = u["phone"]

    agent = await resolve_agent(conn, playbook_key)
    user_data = await build_user_data(
        conn,
        user_id=user_id,
        playbook_key=playbook_key,
        stage_key=stage_key,
        hint_text=hint_text,
    )
    payload = {
        "agent_id": agent["agent_id"],
        "recipient_phone_number": phone,
        "user_data": user_data,
    }

    # Idempotent insert BEFORE dialing
    cur = await conn.execute(
        """
        INSERT INTO calls (
            stall_id, decision_id, user_id, playbook_key, stage_key, hint_id,
            phone, agent_id, status, payload_sent, dry_run
        ) VALUES (
            %s, %s, %s, %s, %s, %s,
            %s, %s, 'queued', %s::jsonb, %s
        )
        ON CONFLICT (stall_id) DO NOTHING
        RETURNING id
        """,
        (
            stall_id,
            decision_id,
            user_id,
            playbook_key,
            stage_key,
            hint_id,
            phone,
            agent["agent_id"] or None,
            json.dumps(payload),
            bool(settings.dry_run),
        ),
    )
    row = await cur.fetchone()
    if not row:
        log.info("call already exists for stall %s — skip dial", stall_id)
        return None
    call_id = int(row["id"])

    dry = bool(settings.dry_run)
    allowed = await is_team_phone(conn, phone)
    real = (
        (not dry)
        and allowed
        and bool(settings.bolna_api_key)
        and bool(agent["agent_id"])
    )

    if not real:
        reason = "dry_run" if dry else "not_team_phone"
        if not dry and not allowed:
            log.warning("refusing real dial to non-team phone %s", phone)
        log.info("DRY_RUN/gate call_id=%s reason=%s payload=%s", call_id, reason, json.dumps(payload))
        await conn.execute(
            """
            UPDATE calls
               SET status = 'dry_run',
                   dry_run = TRUE,
                   updated_at = now()
             WHERE id = %s
            """,
            (call_id,),
        )
        return {"call_id": call_id, "status": "dry_run", "payload": payload, "gate": reason}

    try:
        resp = await _post_call_with_retries(payload)
    except Exception as e:
        log.exception("bolna dial failed call_id=%s", call_id)
        await conn.execute(
            """
            UPDATE calls
               SET status = 'error',
                   termination_reason = %s,
                   dry_run = FALSE,
                   updated_at = now()
             WHERE id = %s
            """,
            (str(e)[:500], call_id),
        )
        return {"call_id": call_id, "status": "error", "error": str(e)}

    execution_id = (
        resp.get("execution_id")
        or resp.get("id")
        or (resp.get("data") or {}).get("execution_id")
        or (resp.get("data") or {}).get("id")
    )
    status = resp.get("status") or "initiated"
    await conn.execute(
        """
        UPDATE calls
           SET execution_id = %s,
               status = %s,
               dry_run = FALSE,
               updated_at = now()
         WHERE id = %s
        """,
        (str(execution_id) if execution_id else None, status, call_id),
    )
    return {
        "call_id": call_id,
        "status": status,
        "execution_id": execution_id,
        "payload": payload,
        "response": resp,
    }


def _extract_cost_cents(data: dict[str, Any]) -> Optional[int]:
    for key in ("total_cost", "cost", "total_cost_cents", "cost_cents"):
        if key in data and data[key] is not None:
            try:
                v = float(data[key])
                if key in ("total_cost", "cost") and abs(v) < 1000:
                    return int(round(v * 100))
                return int(round(v))
            except (TypeError, ValueError):
                pass
    return None


def apply_execution_fields(data: dict[str, Any]) -> dict[str, Any]:
    status = data.get("status") or data.get("call_status")
    termination_reason = (
        data.get("termination_reason")
        or data.get("hangup_reason")
        or data.get("error_message")
    )
    duration = data.get("conversation_duration")
    if duration is None:
        duration = data.get("duration")
    try:
        duration_i = int(round(float(duration))) if duration is not None else None
    except (TypeError, ValueError):
        duration_i = None
    cost_cents = _extract_cost_cents(data)
    transcript = data.get("transcript")
    if transcript is not None and not isinstance(transcript, str):
        transcript = json.dumps(transcript)
    summary = data.get("summary")
    if summary is not None and not isinstance(summary, str):
        summary = json.dumps(summary)
    extracted = data.get("extracted_data") or data.get("extracted") or {}
    if isinstance(extracted, str):
        try:
            extracted = json.loads(extracted)
        except json.JSONDecodeError:
            extracted = {"raw": extracted}
    pickup = bool(duration_i and duration_i > 0)
    return {
        "status": status,
        "termination_reason": termination_reason,
        "conversation_duration": duration_i,
        "cost_cents": cost_cents,
        "transcript": transcript,
        "summary": summary,
        "extracted_data": extracted if isinstance(extracted, dict) else {},
        "pickup": pickup,
    }


async def update_call_from_execution(
    conn: AsyncConnection,
    call_id: int,
    data: dict[str, Any],
) -> dict[str, Any]:
    fields = apply_execution_fields(data)
    settings = get_settings()
    cost_inr = None
    if fields["cost_cents"] is not None:
        cost_inr = (fields["cost_cents"] / 100.0) * float(settings.inr_per_usd)

    await conn.execute(
        """
        UPDATE calls SET
            status = COALESCE(%s, status),
            termination_reason = COALESCE(%s, termination_reason),
            conversation_duration = COALESCE(%s, conversation_duration),
            cost_cents = COALESCE(%s, cost_cents),
            cost_inr = COALESCE(%s, cost_inr),
            transcript = COALESCE(%s, transcript),
            summary = COALESCE(%s, summary),
            extracted_data = COALESCE(%s::jsonb, extracted_data),
            pickup = COALESCE(%s, pickup),
            updated_at = now()
         WHERE id = %s
        """,
        (
            fields["status"],
            fields["termination_reason"],
            fields["conversation_duration"],
            fields["cost_cents"],
            cost_inr,
            fields["transcript"],
            fields["summary"],
            json.dumps(fields["extracted_data"]) if fields["extracted_data"] is not None else None,
            fields["pickup"],
            call_id,
        ),
    )

    extracted = fields["extracted_data"] or {}
    outcome = str(extracted.get("outcome") or extracted.get("result") or "").lower()
    if outcome in {"opted_out", "opt_out", "opt-out", "do_not_call"}:
        cur = await conn.execute("SELECT user_id, phone FROM calls WHERE id = %s", (call_id,))
        row = await cur.fetchone()
        if row:
            await conn.execute(
                "UPDATE users SET opted_out = TRUE, updated_at = now() WHERE id = %s",
                (row["user_id"],),
            )
            if row["phone"]:
                await conn.execute(
                    """
                    INSERT INTO dnd_numbers (phone, source, note)
                    VALUES (%s, 'user_flag', 'opted out via call')
                    ON CONFLICT (phone) DO NOTHING
                    """,
                    (row["phone"],),
                )
            log.info("user opted out via call %s", call_id)

    return fields


async def poll_non_terminal_calls(conn: AsyncConnection, limit: int = 50) -> int:
    cur = await conn.execute(
        """
        SELECT id, execution_id, status FROM calls
         WHERE execution_id IS NOT NULL
           AND status IS NOT NULL
           AND lower(status) NOT IN (
                'completed','failed','busy','no-answer','no_answer',
                'canceled','cancelled','error','stopped','dry_run'
           )
         ORDER BY updated_at ASC
         LIMIT %s
        """,
        (limit,),
    )
    rows = await cur.fetchall()
    n = 0
    for r in rows:
        try:
            data = await get_execution(r["execution_id"])
            await update_call_from_execution(conn, int(r["id"]), data)
            n += 1
        except Exception:
            log.exception("poll failed call_id=%s exec=%s", r["id"], r["execution_id"])
    return n


async def place_ready_calls(conn: AsyncConnection, decisions: list[dict[str, Any]]) -> int:
    placed = 0
    for d in decisions:
        result = await place_call_for_decision(conn, d)
        if result is not None:
            placed += 1
    return placed
