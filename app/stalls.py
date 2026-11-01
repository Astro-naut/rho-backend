"""Stall detection: open when a stage's threshold is exceeded; close on completion.

Config-driven: thresholds and stage order come from playbook_stages.
Playbook = referral if ref_code resolved to a referrer, else reactivation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from psycopg import AsyncConnection


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def resolve_playbook_key(conn: AsyncConnection, user_id: str) -> str:
    """Referral wins only when ref_status='resolved' and referrer_user_id is set."""
    cur = await conn.execute(
        """
        SELECT ref_status, referrer_user_id
          FROM users WHERE id = %s
        """,
        (user_id,),
    )
    row = await cur.fetchone()
    if row and row["ref_status"] == "resolved" and row["referrer_user_id"]:
        return "referral"
    return "reactivation"


async def close_stall_for_stage(
    conn: AsyncConnection,
    user_id: str,
    stage_key: str,
    reason: str = "completed",
) -> int:
    """Close any open stall for this user on this stage (or any stage if completing)."""
    cur = await conn.execute(
        """
        UPDATE stalls
           SET status = 'closed',
               closed_at = now(),
               close_reason = %s
         WHERE user_id = %s
           AND status = 'open'
           AND (stage_key = %s OR true)
        RETURNING id
        """,
        (reason, user_id, stage_key),
    )
    # Close the open stall whenever the user progresses (completes any stage).
    rows = await cur.fetchall()
    return len(rows)


async def close_open_stall(
    conn: AsyncConnection,
    user_id: str,
    reason: str = "completed",
) -> int:
    cur = await conn.execute(
        """
        UPDATE stalls
           SET status = 'closed',
               closed_at = now(),
               close_reason = %s
         WHERE user_id = %s AND status = 'open'
        RETURNING id
        """,
        (reason, user_id),
    )
    return len(await cur.fetchall())


async def is_still_stalled(conn: AsyncConnection, stall_id: int) -> bool:
    """Pre-dial recheck: stall still open and threshold still exceeded, stage not completed."""
    cur = await conn.execute(
        """
        SELECT s.id, s.user_id, s.stage_key, s.playbook_key, s.status, s.opened_at,
               uss.entered_at, uss.completed_at,
               ps.threshold_minutes
          FROM stalls s
          JOIN playbook_stages ps
            ON ps.playbook_key = s.playbook_key AND ps.stage_key = s.stage_key
          LEFT JOIN user_stage_state uss
            ON uss.user_id = s.user_id AND uss.stage_key = s.stage_key
         WHERE s.id = %s
        """,
        (stall_id,),
    )
    row = await cur.fetchone()
    if not row or row["status"] != "open":
        return False
    if row["completed_at"] is not None:
        return False
    entered = row["entered_at"] or row["opened_at"]
    if entered.tzinfo is None:
        entered = entered.replace(tzinfo=timezone.utc)
    age_min = (_utcnow() - entered).total_seconds() / 60.0
    return age_min >= float(row["threshold_minutes"])


async def detect_stalls(conn: AsyncConnection, *, now: Optional[datetime] = None) -> dict[str, int]:
    """Scan users with a current stage; open/close stalls idempotently.

    Must be called under pg_try_advisory_xact_lock by the tick runner.
    """
    now = now or _utcnow()
    opened = 0
    closed = 0

    # Close stalls where the stage is already completed.
    cur = await conn.execute(
        """
        UPDATE stalls s
           SET status = 'closed',
               closed_at = now(),
               close_reason = 'stage_completed'
          FROM user_stage_state uss
         WHERE s.status = 'open'
           AND uss.user_id = s.user_id
           AND uss.stage_key = s.stage_key
           AND uss.completed_at IS NOT NULL
        RETURNING s.id
        """
    )
    closed += len(await cur.fetchall())

    # Also close open stalls when user's current_stage has moved past the stall stage.
    cur = await conn.execute(
        """
        UPDATE stalls s
           SET status = 'closed',
               closed_at = now(),
               close_reason = 'progressed'
         WHERE s.status = 'open'
           AND EXISTS (
               SELECT 1
                 FROM users u
                 JOIN playbook_stages ps_stall
                   ON ps_stall.playbook_key = s.playbook_key
                  AND ps_stall.stage_key = s.stage_key
                 JOIN playbook_stages ps_cur
                   ON ps_cur.playbook_key = s.playbook_key
                  AND ps_cur.stage_key = u.current_stage
                WHERE u.id = s.user_id
                  AND ps_cur.ordinal > ps_stall.ordinal
           )
        RETURNING s.id
        """
    )
    closed += len(await cur.fetchall())

    # Candidates: users currently in a stage that is not completed and past threshold.
    cur = await conn.execute(
        """
        SELECT u.id AS user_id,
               u.current_stage AS stage_key,
               u.ref_status,
               u.referrer_user_id,
               uss.entered_at,
               CASE
                 WHEN u.ref_status = 'resolved' AND u.referrer_user_id IS NOT NULL
                 THEN 'referral' ELSE 'reactivation'
               END AS playbook_key
          FROM users u
          JOIN user_stage_state uss
            ON uss.user_id = u.id AND uss.stage_key = u.current_stage
         WHERE u.current_stage IS NOT NULL
           AND uss.completed_at IS NULL
           AND u.opted_out = false
        """
    )
    candidates = await cur.fetchall()

    for c in candidates:
        playbook = c["playbook_key"]
        stage = c["stage_key"]
        cur = await conn.execute(
            """
            SELECT threshold_minutes
              FROM playbook_stages
             WHERE playbook_key = %s AND stage_key = %s
            """,
            (playbook, stage),
        )
        ps = await cur.fetchone()
        if not ps:
            continue
        entered = c["entered_at"]
        if entered.tzinfo is None:
            entered = entered.replace(tzinfo=timezone.utc)
        age_min = (now - entered).total_seconds() / 60.0
        if age_min < float(ps["threshold_minutes"]):
            continue

        # Idempotent open: unique partial index on (user_id) WHERE status='open'
        cur = await conn.execute(
            """
            INSERT INTO stalls (user_id, stage_key, playbook_key, status, opened_at)
            VALUES (%s, %s, %s, 'open', now())
            ON CONFLICT (user_id) WHERE status = 'open' DO NOTHING
            RETURNING id
            """,
            (c["user_id"], stage, playbook),
        )
        if await cur.fetchone():
            opened += 1
        else:
            # If there is already an open stall on a different stage, leave it;
            # if same stage, nothing to do. If open stall is stale different stage,
            # close+reopen atomically when stage differs.
            cur = await conn.execute(
                """
                SELECT id, stage_key, playbook_key FROM stalls
                 WHERE user_id = %s AND status = 'open'
                 FOR UPDATE
                """,
                (c["user_id"],),
            )
            existing = await cur.fetchone()
            if existing and (
                existing["stage_key"] != stage or existing["playbook_key"] != playbook
            ):
                await conn.execute(
                    """
                    UPDATE stalls
                       SET status = 'closed', closed_at = now(), close_reason = 'stage_changed'
                     WHERE id = %s
                    """,
                    (existing["id"],),
                )
                closed += 1
                cur = await conn.execute(
                    """
                    INSERT INTO stalls (user_id, stage_key, playbook_key, status, opened_at)
                    VALUES (%s, %s, %s, 'open', now())
                    ON CONFLICT (user_id) WHERE status = 'open' DO NOTHING
                    RETURNING id
                    """,
                    (c["user_id"], stage, playbook),
                )
                if await cur.fetchone():
                    opened += 1

    return {"opened": opened, "closed": closed}
