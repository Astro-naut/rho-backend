"""Decision engine: hard filters -> holdout -> Thompson bandit over call_now/none."""

from __future__ import annotations

import json
import logging
import math
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection

from app.dnd import dnd_check

log = logging.getLogger("rho.decision")
IST = ZoneInfo("Asia/Kolkata")


async def load_policy(conn: AsyncConnection, playbook_key: str) -> dict[str, Any]:
    cur = await conn.execute("SELECT * FROM policies WHERE playbook_key = %s", (playbook_key,))
    row = await cur.fetchone()
    if not row:
        raise RuntimeError(f"missing policy for {playbook_key}")
    return dict(row)


def hour_bucket(now: Optional[datetime] = None) -> str:
    dt = now or datetime.now(IST)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc).astimezone(IST)
    else:
        dt = dt.astimezone(IST)
    h = dt.hour
    if 9 <= h < 12:
        return "morning"
    if 12 <= h < 17:
        return "afternoon"
    if 17 <= h < 21:
        return "evening"
    return "off"


def _window_hm(policy: dict[str, Any]) -> tuple[int, int, int, int]:
    start = policy["call_window_start"]
    end = policy["call_window_end"]
    if hasattr(start, "hour"):
        sh, sm = start.hour, start.minute
    else:
        parts = str(start).split(":")
        sh, sm = int(parts[0]), int(parts[1])
    if hasattr(end, "hour"):
        eh, em = end.hour, end.minute
    else:
        parts = str(end).split(":")
        eh, em = int(parts[0]), int(parts[1])
    return sh, sm, eh, em


def next_window_open(policy: dict[str, Any], now: Optional[datetime] = None) -> datetime:
    dt = now or datetime.now(IST)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc).astimezone(IST)
    else:
        dt = dt.astimezone(IST)
    sh, sm, eh, em = _window_hm(policy)
    start = dt.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = dt.replace(hour=eh, minute=em, second=0, microsecond=0)
    if start <= dt <= end:
        return dt
    if dt < start:
        return start
    return start + timedelta(days=1)


def in_call_window(policy: dict[str, Any], now: Optional[datetime] = None) -> bool:
    dt = now or datetime.now(IST)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc).astimezone(IST)
    else:
        dt = dt.astimezone(IST)
    sh, sm, eh, em = _window_hm(policy)
    start = dt.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = dt.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start <= dt <= end


async def is_still_stalled(conn: AsyncConnection, stall: dict[str, Any]) -> bool:
    if stall.get("status") != "open":
        return False
    cur = await conn.execute(
        "SELECT current_stage FROM users WHERE id = %s",
        (stall["user_id"],),
    )
    u = await cur.fetchone()
    if not u:
        return False
    return u["current_stage"] == stall["stage_key"]


async def _log_decision(
    conn: AsyncConnection,
    *,
    stall_id: int,
    user_id: str,
    playbook_key: str,
    stage_key: str,
    action: str,
    reason: str,
    score: Optional[float],
    features: dict[str, Any],
    arm_key: Optional[str] = None,
    hint_id: Optional[int] = None,
    holdout: bool = False,
) -> int:
    # Store both filter_reason and reason; features live in context JSONB.
    cur = await conn.execute(
        """
        INSERT INTO decisions (
            stall_id, user_id, playbook_key, stage_key, action,
            filter_reason, reason, score, holdout, hint_id, context, arm_key
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s::jsonb, %s
        ) RETURNING id
        """,
        (
            stall_id,
            user_id,
            playbook_key,
            stage_key,
            action,
            reason,
            reason,
            score,
            holdout,
            hint_id,
            json.dumps(features, default=str),
            arm_key,
        ),
    )
    row = await cur.fetchone()
    return int(row["id"])


async def _ensure_bandit_arm(
    conn: AsyncConnection, arm_key: str, context_key: str
) -> dict[str, Any]:
    await conn.execute(
        """
        INSERT INTO bandit_arms (arm_key, context_key, alpha, beta, pulls)
        VALUES (%s, %s, 1, 1, 0)
        ON CONFLICT (arm_key, context_key) DO NOTHING
        """,
        (arm_key, context_key),
    )
    cur = await conn.execute(
        "SELECT * FROM bandit_arms WHERE arm_key = %s AND context_key = %s",
        (arm_key, context_key),
    )
    return dict(await cur.fetchone())


async def _pick_hint(
    conn: AsyncConnection, playbook_key: str, stage_key: str
) -> tuple[Optional[int], str]:
    cur = await conn.execute(
        """
        SELECT h.id, h.text, COALESCE(a.alpha, 1) AS alpha, COALESCE(a.beta, 1) AS beta
          FROM script_hints h
          LEFT JOIN hint_arms a ON a.hint_id = h.id
         WHERE h.playbook_key = %s AND h.stage_key = %s
         ORDER BY h.hint_index
        """,
        (playbook_key, stage_key),
    )
    rows = await cur.fetchall()
    if not rows:
        return None, ""
    best = None
    best_sample = -1.0
    for r in rows:
        sample = random.betavariate(float(r["alpha"]), float(r["beta"]))
        if sample > best_sample:
            best_sample = sample
            best = r
    assert best is not None
    await conn.execute(
        """
        INSERT INTO hint_arms (hint_id, playbook_key, stage_key, alpha, beta, pulls)
        VALUES (%s, %s, %s, 1, 1, 0)
        ON CONFLICT (hint_id) DO NOTHING
        """,
        (best["id"], playbook_key, stage_key),
    )
    return int(best["id"]), str(best["text"])


async def evaluate_stall(
    conn: AsyncConnection,
    stall: dict[str, Any],
    *,
    force: bool = False,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Hard filters + holdout + Thompson gate.

    force=True (DEV trigger): still respects filters except the min_score threshold.
    """
    user_id = stall["user_id"]
    playbook_key = stall["playbook_key"]
    stage_key = stall["stage_key"]
    stall_id = int(stall["id"])
    policy = await load_policy(conn, playbook_key)
    features: dict[str, Any] = {
        "stall_id": stall_id,
        "user_id": user_id,
        "playbook": playbook_key,
        "stage": stage_key,
        "force": force,
    }
    hb = hour_bucket(now)
    context_key = f"{playbook_key}|{stage_key}|{hb}"
    features["hour_bucket"] = hb
    features["context_key"] = context_key

    async def decide(
        action: str,
        reason: str,
        score: Optional[float] = None,
        **extra: Any,
    ) -> dict[str, Any]:
        feat = {**features, **extra.get("feat_extra", {})}
        did = await _log_decision(
            conn,
            stall_id=stall_id,
            user_id=user_id,
            playbook_key=playbook_key,
            stage_key=stage_key,
            action=action,
            reason=reason,
            score=score,
            features=feat,
            arm_key=extra.get("arm_key"),
            hint_id=extra.get("hint_id"),
            holdout=bool(extra.get("holdout", False)),
        )
        out = {
            "decision_id": did,
            "action": action,
            "reason": reason,
            "score": score,
            "stall_id": stall_id,
            "user_id": user_id,
            "playbook_key": playbook_key,
            "stage_key": stage_key,
            "hint_id": extra.get("hint_id"),
            "hint_text": extra.get("hint_text"),
            "arm_key": extra.get("arm_key"),
            "context_key": context_key,
            "holdout": bool(extra.get("holdout", False)),
            "call_later": extra.get("call_later"),
        }
        log.info(
            "decision stall=%s action=%s reason=%s score=%s",
            stall_id,
            action,
            reason,
            score,
        )
        return out

    # 1. stall_resolved
    if not await is_still_stalled(conn, stall):
        return await decide("none", "stall_resolved")

    cur = await conn.execute("SELECT * FROM users WHERE id = %s", (user_id,))
    user = await cur.fetchone()
    if not user:
        return await decide("none", "stall_resolved")

    # 2. no_consent
    if not user["consent"]:
        return await decide("none", "no_consent")

    # 3. opted_out
    if user["opted_out"]:
        return await decide("none", "opted_out")

    # 4. on_dnd (users.dnd OR dnd_numbers)
    on_dnd = bool(user["dnd"])
    if not on_dnd and user.get("phone"):
        on_dnd = await dnd_check(conn, user["phone"])
    features["on_dnd"] = on_dnd
    if on_dnd:
        return await decide("none", "on_dnd")

    # 5. outside_window
    if not in_call_window(policy, now):
        nxt = next_window_open(policy, now)
        return await decide(
            "call_later",
            "outside_window",
            call_later={"next_window_open": nxt.isoformat(), "re_evaluate": True},
            feat_extra={"next_window_open": nxt.isoformat()},
        )

    # 6. user_cap (calls per per_user_cap_days)
    cap_days = int(policy["per_user_cap_days"] or 7)
    cap = int(policy["per_user_cap"] or 2)
    cur = await conn.execute(
        """
        SELECT COUNT(*)::int AS n FROM calls
         WHERE user_id = %s
           AND created_at >= (now() AT TIME ZONE 'utc') - (%s || ' days')::interval
           AND lower(status) <> 'error'
        """,
        (user_id, str(cap_days)),
    )
    n_calls = (await cur.fetchone())["n"]
    features["calls_window"] = n_calls
    features["per_user_cap"] = cap
    if n_calls >= cap:
        return await decide("none", "user_cap", feat_extra={"calls_window": n_calls})

    # 7. daily_budget (today IST spend + reserved estimate)
    reserved = float(policy.get("reserved_call_cost_inr") or 15)
    cur = await conn.execute(
        """
        SELECT COALESCE(SUM(COALESCE(cost_inr, 0)), 0)::float AS spent
          FROM calls
         WHERE (created_at AT TIME ZONE 'Asia/Kolkata')::date
             = (now() AT TIME ZONE 'Asia/Kolkata')::date
        """
    )
    spent = float((await cur.fetchone())["spent"])
    features["spent_today_inr"] = spent
    features["reserved_inr"] = reserved
    if spent + reserved > float(policy["daily_budget_inr"]):
        return await decide(
            "none",
            "daily_budget",
            feat_extra={"spent_today_inr": spent, "reserved_inr": reserved},
        )

    # Holdout — never called
    holdout_pct = float(policy["holdout_pct"] or 0)
    features["holdout_pct"] = holdout_pct
    if holdout_pct > 0 and random.random() < holdout_pct:
        await _ensure_bandit_arm(conn, "none", context_key)
        return await decide(
            "none",
            "holdout",
            score=0.0,
            arm_key="none",
            holdout=True,
        )

    # Thompson sampling over [none, call_now]
    none_arm = await _ensure_bandit_arm(conn, "none", context_key)
    call_arm = await _ensure_bandit_arm(conn, "call_now", context_key)
    sample_none = random.betavariate(float(none_arm["alpha"]), float(none_arm["beta"]))
    sample_call = random.betavariate(float(call_arm["alpha"]), float(call_arm["beta"]))
    uplift = sample_call - sample_none
    norm = max(0.0, min(1.0, (uplift + 1.0) / 2.0))

    opened = stall.get("opened_at")
    if opened is not None:
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        now_utc = now or datetime.now(timezone.utc)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        minutes = max(0.0, (now_utc - opened.astimezone(timezone.utc)).total_seconds() / 60.0)
    else:
        minutes = 0.0
    half = float(policy.get("half_life_minutes") or 120)
    decay = math.exp(-minutes / half) if half > 0 else 1.0
    score = norm * decay
    features.update(
        {
            "sample_none": sample_none,
            "sample_call": sample_call,
            "uplift": uplift,
            "norm": norm,
            "minutes_since_stall": minutes,
            "half_life_minutes": half,
            "decay": decay,
            "min_score": float(policy["min_score"]),
        }
    )

    hint_id, hint_text = await _pick_hint(conn, playbook_key, stage_key)

    if force or score >= float(policy["min_score"]):
        reason = "force" if force and score < float(policy["min_score"]) else "bandit"
        return await decide(
            "call_now",
            reason,
            score=score,
            arm_key="call_now",
            hint_id=hint_id,
            hint_text=hint_text,
            feat_extra=features,
        )

    return await decide(
        "none",
        "below_threshold",
        score=score,
        arm_key="none",
        hint_id=hint_id,
        hint_text=hint_text,
        feat_extra=features,
    )


async def claim_open_stalls(conn: AsyncConnection, limit: int = 50) -> list[dict[str, Any]]:
    cur = await conn.execute(
        """
        SELECT s.*
          FROM stalls s
         WHERE s.status = 'open'
         ORDER BY s.opened_at ASC
         LIMIT %s
         FOR UPDATE OF s SKIP LOCKED
        """,
        (limit,),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def run_decisions(conn: AsyncConnection, limit: int = 50) -> list[dict[str, Any]]:
    """Claim stalls and evaluate each. Returns call_now decisions ready to dial."""
    stalls = await claim_open_stalls(conn, limit=limit)
    ready: list[dict[str, Any]] = []
    for stall in stalls:
        cur = await conn.execute(
            "SELECT id FROM calls WHERE stall_id = %s",
            (stall["id"],),
        )
        if await cur.fetchone():
            continue
        decision = await evaluate_stall(conn, stall)
        if decision["action"] == "call_now":
            ready.append(decision)
    return ready
