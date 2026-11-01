"""In-process tick worker + run_tick(). Per-step advisory locks."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from psycopg import AsyncConnection

from app.bolna_client import place_call_for_decision, poll_non_terminal_calls
from app.config import get_settings
from app.db import get_pool
from app.decision import evaluate_stall, run_decisions
from app.rewards import process_call_rewards
from app.stalls import detect_stalls

log = logging.getLogger("rho.worker")

# Per-step advisory lock keys
_LOCK_STALLS = 0x52484F5F5354414C & 0x7FFFFFFFFFFFFFFF  # RHO_STAL
_LOCK_DECISIONS = 0x52484F5F44454349 & 0x7FFFFFFFFFFFFFFF  # RHO_DECI
_LOCK_DIAL = 0x52484F5F4449414C & 0x7FFFFFFFFFFFFFFF  # RHO_DIAL
_LOCK_POLL = 0x52484F5F504F4C4C & 0x7FFFFFFFFFFFFFFF  # RHO_POLL
_LOCK_REWARDS = 0x52484F5F52455741 & 0x7FFFFFFFFFFFFFFF  # RHO_REWA


async def _with_lock(
    conn: AsyncConnection, lock_key: int, fn
) -> tuple[bool, Any]:
    """Try advisory xact lock; if held, skip. Returns (ran, result)."""
    cur = await conn.execute(
        "SELECT pg_try_advisory_xact_lock(%s) AS got",
        (lock_key,),
    )
    row = await cur.fetchone()
    if not row or not row["got"]:
        return False, None
    result = await fn(conn)
    return True, result


async def hook_run_decisions(conn: AsyncConnection) -> int:
    ready = await run_decisions(conn)
    # Stash ready decisions on the connection via a simple attribute for dial step
    # within the same tick — dial runs in its own lock/transaction, so return count
    # and let dial re-read call_now decisions without calls.
    return len(ready)


async def _pending_call_now_decisions(conn: AsyncConnection, limit: int = 50) -> list[dict]:
    """call_now decisions that still have no calls row."""
    cur = await conn.execute(
        """
        SELECT d.id AS decision_id, d.stall_id, d.user_id, d.playbook_key, d.stage_key,
               d.hint_id, d.arm_key, d.score, d.holdout,
               h.text AS hint_text
          FROM decisions d
          LEFT JOIN calls c ON c.stall_id = d.stall_id
          LEFT JOIN script_hints h ON h.id = d.hint_id
         WHERE d.action = 'call_now'
           AND c.id IS NULL
         ORDER BY d.created_at ASC
         LIMIT %s
         FOR UPDATE OF d SKIP LOCKED
        """,
        (limit,),
    )
    rows = await cur.fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "decision_id": int(r["decision_id"]),
                "stall_id": int(r["stall_id"]),
                "user_id": r["user_id"],
                "playbook_key": r["playbook_key"],
                "stage_key": r["stage_key"],
                "hint_id": r["hint_id"],
                "hint_text": r["hint_text"] or "",
                "arm_key": r["arm_key"],
                "score": r["score"],
                "holdout": r["holdout"],
            }
        )
    return out


async def hook_place_calls(conn: AsyncConnection) -> int:
    decisions = await _pending_call_now_decisions(conn)
    placed = 0
    for d in decisions:
        result = await place_call_for_decision(conn, d)
        if result is not None:
            placed += 1
    return placed


async def hook_poll_call_results(conn: AsyncConnection) -> int:
    return await poll_non_terminal_calls(conn)


async def hook_update_rewards(conn: AsyncConnection) -> int:
    return await process_call_rewards(conn)


async def run_tick() -> dict[str, Any]:
    """stalls -> decisions -> dial -> poll -> rewards, each under its own advisory lock."""
    pool = await get_pool()
    result = {
        "ok": True,
        "stalls_opened": 0,
        "stalls_closed": 0,
        "decisions": 0,
        "calls": 0,
        "rewards": 0,
        "polls": 0,
        "skipped": False,
        "locks_skipped": [],
    }

    # Each step in its own transaction so locks don't block the whole tick.
    async with pool.connection() as conn:
        async with conn.transaction():
            ran, stats = await _with_lock(conn, _LOCK_STALLS, detect_stalls)
            if ran and stats:
                result["stalls_opened"] = int(stats.get("opened", 0))
                result["stalls_closed"] = int(stats.get("closed", 0))
            elif not ran:
                result["locks_skipped"].append("stalls")

    async with pool.connection() as conn:
        async with conn.transaction():
            ran, n = await _with_lock(conn, _LOCK_DECISIONS, hook_run_decisions)
            if ran:
                result["decisions"] = int(n or 0)
            else:
                result["locks_skipped"].append("decisions")

    async with pool.connection() as conn:
        async with conn.transaction():
            ran, n = await _with_lock(conn, _LOCK_DIAL, hook_place_calls)
            if ran:
                result["calls"] = int(n or 0)
            else:
                result["locks_skipped"].append("dial")

    async with pool.connection() as conn:
        async with conn.transaction():
            ran, n = await _with_lock(conn, _LOCK_POLL, hook_poll_call_results)
            if ran:
                result["polls"] = int(n or 0)
            else:
                result["locks_skipped"].append("poll")

    async with pool.connection() as conn:
        async with conn.transaction():
            ran, n = await _with_lock(conn, _LOCK_REWARDS, hook_update_rewards)
            if ran:
                result["rewards"] = int(n or 0)
            else:
                result["locks_skipped"].append("rewards")

    # Overall skipped only if every step was locked out
    if len(result["locks_skipped"]) == 5:
        result["skipped"] = True

    log.info(
        "tick done opened=%s closed=%s decisions=%s calls=%s polls=%s rewards=%s skipped_locks=%s",
        result["stalls_opened"],
        result["stalls_closed"],
        result["decisions"],
        result["calls"],
        result["polls"],
        result["rewards"],
        result["locks_skipped"],
    )
    return result


async def force_evaluate_user(user_id: str) -> dict[str, Any]:
    """DEV: force-evaluate that user's open stall now (filters except threshold)."""
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                """
                SELECT * FROM stalls
                 WHERE user_id = %s AND status = 'open'
                 ORDER BY opened_at DESC
                 LIMIT 1
                 FOR UPDATE
                """,
                (user_id,),
            )
            stall = await cur.fetchone()
            if not stall:
                return {"ok": False, "error": "no open stall"}
            stall = dict(stall)
            cur = await conn.execute(
                "SELECT id FROM calls WHERE stall_id = %s", (stall["id"],)
            )
            if await cur.fetchone():
                return {"ok": False, "error": "call already exists for stall", "stall_id": stall["id"]}

            decision = await evaluate_stall(conn, stall, force=True)
            placed = None
            if decision["action"] == "call_now":
                placed = await place_call_for_decision(conn, decision)
            return {
                "ok": True,
                "decision": decision,
                "call": placed,
            }


async def worker_loop(stop_event: asyncio.Event | None = None) -> None:
    settings = get_settings()
    interval = max(5, int(settings.tick_interval_seconds))
    log.info("worker loop started interval=%ss", interval)
    while True:
        if stop_event and stop_event.is_set():
            break
        try:
            await run_tick()
        except Exception:
            log.exception("worker tick failed")
        try:
            if stop_event:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                break
            await asyncio.sleep(interval)
        except asyncio.TimeoutError:
            continue
