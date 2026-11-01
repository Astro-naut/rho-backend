"""Reward assignment for bandit arms after the policy reward window."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg import AsyncConnection

from app.decision import load_policy

log = logging.getLogger("rho.rewards")


async def _update_bandit(
    conn: AsyncConnection, arm_key: str, context_key: str, success: bool
) -> None:
    await conn.execute(
        """
        INSERT INTO bandit_arms (arm_key, context_key, alpha, beta, pulls)
        VALUES (%s, %s, 1, 1, 0)
        ON CONFLICT (arm_key, context_key) DO NOTHING
        """,
        (arm_key, context_key),
    )
    if success:
        await conn.execute(
            """
            UPDATE bandit_arms
               SET alpha = alpha + 1, pulls = pulls + 1, updated_at = now()
             WHERE arm_key = %s AND context_key = %s
            """,
            (arm_key, context_key),
        )
    else:
        await conn.execute(
            """
            UPDATE bandit_arms
               SET beta = beta + 1, pulls = pulls + 1, updated_at = now()
             WHERE arm_key = %s AND context_key = %s
            """,
            (arm_key, context_key),
        )


async def _update_hint(conn: AsyncConnection, hint_id: int, success: bool) -> None:
    if success:
        await conn.execute(
            """
            UPDATE hint_arms
               SET alpha = alpha + 1, pulls = pulls + 1, updated_at = now()
             WHERE hint_id = %s
            """,
            (hint_id,),
        )
    else:
        await conn.execute(
            """
            UPDATE hint_arms
               SET beta = beta + 1, pulls = pulls + 1, updated_at = now()
             WHERE hint_id = %s
            """,
            (hint_id,),
        )


async def _conversion_in_window(
    conn: AsyncConnection,
    user_id: str,
    window_start: datetime,
    window_end: datetime,
) -> bool:
    cur = await conn.execute(
        """
        SELECT 1 FROM events
         WHERE user_id = %s
           AND event = 'deposit_completed'
           AND ts >= %s
           AND ts <= %s
         LIMIT 1
        """,
        (user_id, window_start, window_end),
    )
    return await cur.fetchone() is not None


def _context_key_from_decision(d: dict[str, Any]) -> str:
    ctx = d.get("context") or {}
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except json.JSONDecodeError:
            ctx = {}
    if ctx.get("context_key"):
        return str(ctx["context_key"])
    hb = ctx.get("hour_bucket") or "off"
    return f"{d['playbook_key']}|{d['stage_key']}|{hb}"


async def process_call_rewards(conn: AsyncConnection, limit: int = 100) -> int:
    """Assign rewards once the reward window has elapsed. Idempotent via UNIQUE(decision_id, reward_type)."""
    n = 0

    # Calls that have a decision and are not yet rewarded
    cur = await conn.execute(
        """
        SELECT c.*, d.arm_key AS decision_arm, d.holdout, d.action AS decision_action,
               d.context AS decision_context, d.created_at AS decision_at,
               d.playbook_key AS d_playbook, d.stage_key AS d_stage, d.hint_id AS d_hint_id
          FROM calls c
          JOIN decisions d ON d.id = c.decision_id
         WHERE c.rewarded_at IS NULL
           AND c.decision_id IS NOT NULL
         ORDER BY c.created_at ASC
         LIMIT %s
        """,
        (limit,),
    )
    calls = await cur.fetchall()
    for c in calls:
        c = dict(c)
        policy = await load_policy(conn, c["playbook_key"])
        hours = float(policy["reward_window_hours"])
        start = c["created_at"]
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        end = start + timedelta(hours=hours)
        if datetime.now(timezone.utc) < end:
            continue

        converted = await _conversion_in_window(conn, c["user_id"], start, end)
        cost = float(c["cost_inr"] or 0)
        optout_penalty = 0.0
        extracted = c["extracted_data"] or {}
        if isinstance(extracted, str):
            try:
                extracted = json.loads(extracted)
            except json.JSONDecodeError:
                extracted = {}
        outcome = str(extracted.get("outcome") or "").lower()
        if outcome in {"opted_out", "opt_out", "opt-out", "do_not_call"}:
            optout_penalty = float(policy["optout_penalty_inr"])

        conversion_value = float(policy["conversion_value_inr"]) if converted else 0.0
        reward_value = conversion_value - cost - optout_penalty
        success = reward_value > 0

        if converted:
            reward_type = "conversion"
        elif optout_penalty > 0:
            reward_type = "optout"
        else:
            reward_type = "none"

        cur_ins = await conn.execute(
            """
            INSERT INTO rewards (
                decision_id, call_id, stall_id, user_id,
                reward_type, value_inr, window_ends_at, finalized
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE)
            ON CONFLICT (decision_id, reward_type) DO NOTHING
            RETURNING id
            """,
            (
                c["decision_id"],
                c["id"],
                c["stall_id"],
                c["user_id"],
                reward_type,
                reward_value,
                end,
            ),
        )
        if not await cur_ins.fetchone():
            # Still mark rewarded_at so we don't loop
            await conn.execute(
                "UPDATE calls SET rewarded_at = COALESCE(rewarded_at, now()), updated_at = now() WHERE id = %s",
                (c["id"],),
            )
            continue

        ctx = c.get("decision_context") or {}
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except json.JSONDecodeError:
                ctx = {}
        context_key = ctx.get("context_key") or f"{c['playbook_key']}|{c['stage_key']}|{ctx.get('hour_bucket', 'off')}"
        arm = c.get("decision_arm") or ("call_now" if c["decision_action"] == "call_now" else "none")

        if c["decision_action"] == "call_now":
            await _update_bandit(conn, "call_now", context_key, success)
            hint_id = c.get("hint_id") or c.get("d_hint_id")
            if hint_id:
                await _update_hint(conn, int(hint_id), success)
        elif c["holdout"] or c["decision_action"] == "none":
            await _update_bandit(conn, "none", context_key, converted)

        await conn.execute(
            "UPDATE calls SET rewarded_at = now(), updated_at = now() WHERE id = %s",
            (c["id"],),
        )
        n += 1
        log.info(
            "reward call=%s value=%s type=%s converted=%s success=%s",
            c["id"],
            reward_value,
            reward_type,
            converted,
            success,
        )

    # Holdout / none decisions with no call: update none arm after window
    cur = await conn.execute(
        """
        SELECT d.*
          FROM decisions d
          LEFT JOIN calls c ON c.decision_id = d.id
          LEFT JOIN rewards r ON r.decision_id = d.id
         WHERE c.id IS NULL
           AND r.id IS NULL
           AND d.action = 'none'
           AND (d.holdout = TRUE OR COALESCE(d.reason, d.filter_reason) IN ('holdout', 'below_threshold'))
         ORDER BY d.created_at ASC
         LIMIT %s
        """,
        (limit,),
    )
    decisions = await cur.fetchall()
    for d in decisions:
        d = dict(d)
        policy = await load_policy(conn, d["playbook_key"])
        hours = float(policy["reward_window_hours"])
        start = d["created_at"]
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        end = start + timedelta(hours=hours)
        if datetime.now(timezone.utc) < end:
            continue
        converted = await _conversion_in_window(conn, d["user_id"], start, end)
        context_key = _context_key_from_decision(d)
        await _update_bandit(conn, "none", context_key, converted)
        reward_type = "conversion" if converted else "none"
        reward_value = float(policy["conversion_value_inr"]) if converted else 0.0
        await conn.execute(
            """
            INSERT INTO rewards (
                decision_id, call_id, stall_id, user_id,
                reward_type, value_inr, window_ends_at, finalized
            ) VALUES (%s, NULL, %s, %s, %s, %s, %s, TRUE)
            ON CONFLICT (decision_id, reward_type) DO NOTHING
            """,
            (
                d["id"],
                d["stall_id"],
                d["user_id"],
                reward_type,
                reward_value,
                end,
            ),
        )
        n += 1
    return n
