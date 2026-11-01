"""Ingest: profile, events, referral resolution, opt-out. Matches Sprout PWA contract."""

from __future__ import annotations

import json
import logging
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple

from psycopg import AsyncConnection

from app.phones import normalize_phone
from app.stalls import close_open_stall

log = logging.getLogger("rho.ingest")

STAGE_ORDER = ["signup", "profile", "kyc", "bank", "plan", "deposit"]
FUTURE_SKEW = timedelta(minutes=5)
MAX_AGE = timedelta(days=30)


def parse_ts(raw: Any) -> datetime:
    """Accept ms/s epoch or ISO. Reject future >5min skew; clamp older than 30 days."""
    now = datetime.now(timezone.utc)
    if isinstance(raw, datetime):
        ts = raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    elif isinstance(raw, (int, float)):
        n = float(raw)
        if n > 1e12:  # ms
            n = n / 1000.0
        ts = datetime.fromtimestamp(n, tz=timezone.utc)
    elif isinstance(raw, str):
        s = raw.strip()
        if s.isdigit() or (s.replace(".", "", 1).isdigit() and s.count(".") < 2):
            return parse_ts(float(s))
        # ISO
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        ts = datetime.fromisoformat(s)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = ts.astimezone(timezone.utc)
    else:
        raise ValueError("invalid timestamp")

    if ts > now + FUTURE_SKEW:
        raise ValueError("timestamp too far in the future")
    floor = now - MAX_AGE
    if ts < floor:
        ts = floor
    return ts


def _next_stage(completed: str) -> Optional[str]:
    try:
        i = STAGE_ORDER.index(completed)
    except ValueError:
        return None
    if i + 1 < len(STAGE_ORDER):
        return STAGE_ORDER[i + 1]
    return None  # funnel complete


def _first_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    part = name.strip().split()
    return part[0] if part else None


async def _ensure_user(conn: AsyncConnection, user_id: str) -> None:
    await conn.execute(
        """
        INSERT INTO users (id) VALUES (%s)
        ON CONFLICT (id) DO NOTHING
        """,
        (user_id,),
    )


async def _issue_referral_code(conn: AsyncConnection, user_id: str) -> str:
    """Issue SPR-XXXXXX with collision retry. Returns existing code if already set."""
    cur = await conn.execute(
        "SELECT referral_code FROM users WHERE id = %s", (user_id,)
    )
    row = await cur.fetchone()
    if row and row["referral_code"]:
        return row["referral_code"]

    alphabet = string.ascii_uppercase + string.digits
    for _ in range(32):
        code = "SPR-" + "".join(secrets.choice(alphabet) for _ in range(6))
        cur = await conn.execute(
            """
            UPDATE users SET referral_code = %s, updated_at = now()
             WHERE id = %s AND referral_code IS NULL
            RETURNING referral_code
            """,
            (code, user_id),
        )
        got = await cur.fetchone()
        if got:
            return got["referral_code"]
        # lost race or collision on unique — re-read
        cur = await conn.execute(
            "SELECT referral_code FROM users WHERE id = %s", (user_id,)
        )
        row = await cur.fetchone()
        if row and row["referral_code"]:
            return row["referral_code"]
    raise RuntimeError("could not allocate referral code")


async def resolve_ref_code(
    conn: AsyncConnection, raw_code: Optional[str]
) -> Tuple[Optional[str], str, Optional[str]]:
    """Resolve a referral code.

    Returns (referrer_user_id | None, ref_status, referrer_first_name | None).
    - Server SPR-* codes: exact match on users.referral_code
    - Legacy SPROUT-NAME: only if exactly one user matches first-name heuristic
    - else ambiguous / unknown; no referrer; stay on reactivation
    """
    if not raw_code or not str(raw_code).strip():
        return None, "unknown", None
    code = str(raw_code).strip()

    cur = await conn.execute(
        "SELECT id, name FROM users WHERE referral_code = %s",
        (code,),
    )
    row = await cur.fetchone()
    if row:
        return row["id"], "resolved", _first_name(row["name"])

    # Legacy SPROUT-FIRSTNAME (case-insensitive)
    upper = code.upper()
    if upper.startswith("SPROUT-"):
        name_part = code.split("-", 1)[1].strip()
        if not name_part:
            return None, "unknown", None
        cur = await conn.execute(
            """
            SELECT id, name FROM users
             WHERE upper(split_part(coalesce(name, ''), ' ', 1)) = upper(%s)
            """,
            (name_part,),
        )
        matches = await cur.fetchall()
        if len(matches) == 1:
            m = matches[0]
            return m["id"], "resolved", _first_name(m["name"])
        if len(matches) > 1:
            return None, "ambiguous", None
        return None, "unknown", None

    return None, "unknown", None


async def enter_stage(
    conn: AsyncConnection,
    user_id: str,
    stage_key: str,
    at: datetime,
) -> None:
    await conn.execute(
        """
        INSERT INTO user_stage_state (user_id, stage_key, entered_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id, stage_key) DO NOTHING
        """,
        (user_id, stage_key, at),
    )
    await conn.execute(
        """
        UPDATE users
           SET current_stage = %s, last_event_at = %s, updated_at = now()
         WHERE id = %s
        """,
        (stage_key, at, user_id),
    )


async def complete_stage(
    conn: AsyncConnection,
    user_id: str,
    stage_key: str,
    at: datetime,
) -> Optional[str]:
    """Mark stage completed, close stalls, enter next stage. Returns next stage or None."""
    await conn.execute(
        """
        INSERT INTO user_stage_state (user_id, stage_key, entered_at, completed_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (user_id, stage_key) DO UPDATE
          SET completed_at = COALESCE(user_stage_state.completed_at, EXCLUDED.completed_at)
        """,
        (user_id, stage_key, at, at),
    )
    await close_open_stall(conn, user_id, reason="completed")
    nxt = _next_stage(stage_key)
    if nxt:
        await enter_stage(conn, user_id, nxt, at)
    else:
        await conn.execute(
            """
            UPDATE users
               SET current_stage = NULL, last_event_at = %s, updated_at = now()
             WHERE id = %s
            """,
            (at, user_id),
        )
    return nxt


async def upsert_profile(
    conn: AsyncConnection,
    user_id: str,
    *,
    phone: Optional[str],
    language: Optional[str],
    consent: bool,
    name: Optional[str],
    ref_code: Optional[str],
) -> dict:
    await _ensure_user(conn, user_id)
    e164 = normalize_phone(phone) if phone else None
    now = datetime.now(timezone.utc)

    referrer_id, ref_status, referrer_first = (None, None, None)
    if ref_code:
        referrer_id, ref_status, referrer_first = await resolve_ref_code(conn, ref_code)
        if referrer_id == user_id:
            referrer_id, ref_status, referrer_first = None, "unknown", None

    await conn.execute(
        """
        UPDATE users SET
            phone = COALESCE(%s, phone),
            language = COALESCE(%s, language),
            consent = %s,
            name = COALESCE(%s, name),
            ref_code_used = COALESCE(%s, ref_code_used),
            ref_status = COALESCE(%s, ref_status),
            referrer_user_id = COALESCE(%s, referrer_user_id),
            updated_at = now(),
            last_event_at = %s
         WHERE id = %s
        """,
        (
            e164,
            language or "hinglish",
            bool(consent),
            name,
            ref_code,
            ref_status,
            referrer_id,
            now,
            user_id,
        ),
    )

    # Profile POST enters the signup stage (idempotent).
    cur = await conn.execute(
        "SELECT current_stage FROM users WHERE id = %s", (user_id,)
    )
    row = await cur.fetchone()
    if not row or not row["current_stage"]:
        await enter_stage(conn, user_id, "signup", now)

    code = await _issue_referral_code(conn, user_id)

    cur = await conn.execute(
        "SELECT current_stage, ref_status FROM users WHERE id = %s", (user_id,)
    )
    u = await cur.fetchone()
    return {
        "ok": True,
        "user_id": user_id,
        "referral_code": code,
        "current_stage": u["current_stage"],
        "ref_status": u["ref_status"],
        "referrer_first_name": referrer_first,
    }


async def ingest_event(
    conn: AsyncConnection,
    *,
    user_id: str,
    stage: str,
    event: str,
    ts_raw: Any,
    ref_code: Optional[str] = None,
) -> dict:
    ts = parse_ts(ts_raw)
    await _ensure_user(conn, user_id)

    await conn.execute(
        """
        INSERT INTO events (user_id, stage, event, ts, ref_code, payload)
        VALUES (%s, %s, %s, %s, %s, %s::jsonb)
        """,
        (
            user_id,
            stage,
            event,
            ts,
            ref_code,
            json.dumps({"stage": stage, "event": event}),
        ),
    )

    if stage == "referral" and event == "shared":
        code = ref_code
        if not code:
            cur = await conn.execute(
                "SELECT referral_code FROM users WHERE id = %s", (user_id,)
            )
            row = await cur.fetchone()
            code = row["referral_code"] if row else None
        await conn.execute(
            """
            INSERT INTO referral_shares (user_id, code, ts)
            VALUES (%s, %s, %s)
            """,
            (user_id, code or "", ts),
        )
        await conn.execute(
            "UPDATE users SET last_event_at = %s, updated_at = now() WHERE id = %s",
            (ts, user_id),
        )
        return {"ok": True, "user_id": user_id, "recorded": "referral_shared"}

    if event == "completed":
        if stage not in STAGE_ORDER:
            return {"ok": True, "user_id": user_id, "recorded": "event", "note": "unknown_stage"}
        nxt = await complete_stage(conn, user_id, stage, ts)
        return {
            "ok": True,
            "user_id": user_id,
            "recorded": "completed",
            "stage": stage,
            "next_stage": nxt,
        }

    await conn.execute(
        "UPDATE users SET last_event_at = %s, updated_at = now() WHERE id = %s",
        (ts, user_id),
    )
    return {"ok": True, "user_id": user_id, "recorded": "event"}


async def get_referral_code(conn: AsyncConnection, user_id: str) -> Optional[str]:
    cur = await conn.execute(
        "SELECT referral_code FROM users WHERE id = %s", (user_id,)
    )
    row = await cur.fetchone()
    if not row or not row["referral_code"]:
        return None
    return row["referral_code"]


async def opt_out(conn: AsyncConnection, user_id: str) -> dict:
    await _ensure_user(conn, user_id)
    await conn.execute(
        """
        UPDATE users SET opted_out = true, updated_at = now() WHERE id = %s
        """,
        (user_id,),
    )
    await close_open_stall(conn, user_id, reason="opted_out")
    return {"ok": True, "user_id": user_id, "opted_out": True}


async def set_user_dnd(conn: AsyncConnection, user_id: str, dnd: bool, who: str = "admin") -> dict:
    await _ensure_user(conn, user_id)
    cur = await conn.execute("SELECT phone, dnd FROM users WHERE id = %s", (user_id,))
    row = await cur.fetchone()
    before = {"user_id": user_id, "dnd": row["dnd"] if row else None, "phone": row["phone"] if row else None}
    await conn.execute(
        "UPDATE users SET dnd = %s, updated_at = now() WHERE id = %s",
        (bool(dnd), user_id),
    )
    phone = row["phone"] if row else None
    if phone:
        e164 = normalize_phone(phone)
        if e164 and dnd:
            await conn.execute(
                """
                INSERT INTO dnd_numbers (phone, source, note)
                VALUES (%s, 'user_flag', %s)
                ON CONFLICT (phone) DO NOTHING
                """,
                (e164, f"user:{user_id}"),
            )
        elif e164 and not dnd:
            await conn.execute(
                "DELETE FROM dnd_numbers WHERE phone = %s AND source = 'user_flag'",
                (e164,),
            )
    after = {"user_id": user_id, "dnd": bool(dnd), "phone": phone}
    await conn.execute(
        """
        INSERT INTO admin_audit_log (who, action, entity, entity_id, before, after)
        VALUES (%s, 'user.dnd', 'users', %s, %s::jsonb, %s::jsonb)
        """,
        (who, user_id, json.dumps(before), json.dumps(after)),
    )
    if dnd:
        await close_open_stall(conn, user_id, reason="dnd")
    return after
