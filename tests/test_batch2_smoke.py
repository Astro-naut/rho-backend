"""Batch 2 smoke tests: dry-run E2E, mocked Bolna dial+poll+webhook, no double-dial."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

# Ensure env before settings cache
os.environ.setdefault("DATABASE_URL", "postgresql://postgres@/rho?host=/workspace/rho-v2/.pgdata")
os.environ["DRY_RUN"] = "1"
os.environ["DEV"] = "1"
os.environ["WORKER_ENABLED"] = "0"
os.environ["BOLNA_API_KEY"] = "test-bolna-key"
os.environ["BOLNA_AGENT_ID_REACTIVATION"] = "agent-react"
os.environ["BOLNA_AGENT_ID_REFERRAL"] = "agent-ref"
os.environ["BOLNA_WEBHOOK_SECRET"] = "whsec"
os.environ["TEAM_PHONES"] = "+919876543210"
os.environ["ADMIN_API_KEY"] = "test-admin-key"
os.environ["TICK_SECRET"] = "test-tick-secret"
os.environ["INR_PER_USD"] = "88"
os.environ["BOLNA_API_BASE"] = "https://api.bolna.ai"

from app.config import get_settings

get_settings.cache_clear()

from app.db import close_pool, get_pool
from app.bolna_client import place_call_for_decision, poll_non_terminal_calls, update_call_from_execution
from app.call_context import build_user_data, customer_first_name
from app.decision import evaluate_stall, in_call_window, load_policy
from app.team_phones import seed_team_phones_from_env
from app.webhooks import handle_bolna_webhook, verify_webhook_secret
from app.worker import force_evaluate_user, run_tick

IST = ZoneInfo("Asia/Kolkata")
TEAM = "+919876543210"


@pytest.fixture(autouse=True)
async def _pool():
    get_settings.cache_clear()
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            await seed_team_phones_from_env(conn)
    yield
    await close_pool()
    get_settings.cache_clear()


async def _make_stalled_user(
    conn,
    *,
    suffix: str | None = None,
    name: str = "John Doe",
    phone: str = TEAM,
    consent: bool = True,
    playbook: str = "reactivation",
    stage: str = "kyc",
    opened_minutes_ago: int = 30,
):
    uid = f"smoke-{suffix or uuid.uuid4().hex[:8]}"
    await conn.execute(
        """
        INSERT INTO users (id, name, phone, consent, opted_out, dnd, current_stage, language)
        VALUES (%s, %s, %s, %s, FALSE, FALSE, %s, 'hinglish')
        ON CONFLICT (id) DO UPDATE SET
            name = EXCLUDED.name,
            phone = EXCLUDED.phone,
            consent = EXCLUDED.consent,
            opted_out = FALSE,
            dnd = FALSE,
            current_stage = EXCLUDED.current_stage
        """,
        (uid, name, phone, consent, stage),
    )
    # Clear DND list entry so prior opt-out webhooks don't poison smoke runs
    await conn.execute("DELETE FROM dnd_numbers WHERE phone = %s", (phone,))
    # Raise caps so prior smoke calls don't trip user_cap
    await conn.execute(
        "UPDATE policies SET per_user_cap = 50 WHERE playbook_key = %s",
        (playbook,),
    )
    # Close any prior open stall and remove prior calls/decisions for a clean smoke user
    await conn.execute(
        """
        UPDATE stalls SET status = 'closed', closed_at = now(), close_reason = 'smoke_reset'
         WHERE user_id = %s AND status = 'open'
        """,
        (uid,),
    )
    await conn.execute("DELETE FROM rewards WHERE user_id = %s", (uid,))
    await conn.execute("DELETE FROM calls WHERE user_id = %s", (uid,))
    await conn.execute("DELETE FROM decisions WHERE user_id = %s", (uid,))
    opened = datetime.now(timezone.utc) - timedelta(minutes=opened_minutes_ago)
    cur = await conn.execute(
        """
        INSERT INTO stalls (user_id, stage_key, playbook_key, status, opened_at)
        VALUES (%s, %s, %s, 'open', %s)
        RETURNING *
        """,
        (uid, stage, playbook, opened),
    )
    stall = dict(await cur.fetchone())
    # Ensure policy window covers now and min_score is reachable for force tests
    await conn.execute(
        """
        UPDATE policies
           SET call_window_start = '00:00',
               call_window_end = '23:59',
               min_score = 0.0,
               holdout_pct = 0.0,
               half_life_minutes = 120,
               reserved_call_cost_inr = 15
         WHERE playbook_key = %s
        """,
        (playbook,),
    )
    return uid, stall


def test_customer_first_name():
    assert customer_first_name("John Doe") == "John"
    assert customer_first_name("") == "there"
    assert customer_first_name(None) == "there"
    assert customer_first_name("priya") == "Priya"


def test_webhook_secret_constant_time():
    assert verify_webhook_secret("whsec", None) is True
    assert verify_webhook_secret(None, "whsec") is True
    assert verify_webhook_secret("nope", None) is False
    assert verify_webhook_secret(None, None) is False


@pytest.mark.asyncio
async def test_dry_run_end_to_end():
    """Team phone + consent -> force evaluate -> dry_run call, payload logged, no HTTP."""
    os.environ["DRY_RUN"] = "1"
    get_settings.cache_clear()
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            uid, stall = await _make_stalled_user(conn, suffix="dry", name="Amrita Shrivastava")

    # Force evaluate via DEV path
    result = await force_evaluate_user(uid)
    assert result["ok"] is True, result
    assert result["decision"]["action"] == "call_now"
    assert result["call"] is not None
    assert result["call"]["status"] == "dry_run"
    assert "payload" in result["call"]
    payload = result["call"]["payload"]
    assert payload["recipient_phone_number"] == TEAM
    assert payload["user_data"]["Customer_Name"] == "Amrita"
    assert "stage_label" in payload["user_data"]
    assert "goal" in payload["user_data"]
    assert "script_hint" in payload["user_data"]
    assert "language" not in payload["user_data"]

    async with pool.connection() as conn:
        cur = await conn.execute("SELECT status, dry_run, payload_sent FROM calls WHERE stall_id = %s", (stall["id"],))
        row = await cur.fetchone()
        assert row is not None
        assert row["status"] == "dry_run"
        assert row["dry_run"] is True


@pytest.mark.asyncio
async def test_mocked_bolna_dial_poll_webhook():
    """DRY_RUN=0 + team phone + mocked Bolna: dial, poll, webhook opt-out."""
    os.environ["DRY_RUN"] = "0"
    get_settings.cache_clear()

    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            uid, stall = await _make_stalled_user(conn, suffix="real", name="Priya Sharma")

    with respx.mock(assert_all_called=False) as router:
        dial = router.post("https://api.bolna.ai/call").mock(
            return_value=httpx.Response(
                200,
                json={"execution_id": "exec-smoke-1", "status": "initiated"},
            )
        )
        poll = router.get("https://api.bolna.ai/executions/exec-smoke-1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "exec-smoke-1",
                    "status": "completed",
                    "conversation_duration": 42,
                    "total_cost": 0.25,
                    "transcript": "hello",
                    "summary": "done",
                    "extracted_data": {"outcome": "completed"},
                    "termination_reason": "hangup",
                },
            )
        )

        result = await force_evaluate_user(uid)
        assert result["ok"] is True, result
        assert result["decision"]["action"] == "call_now", result
        assert result["call"] is not None, result
        assert result["call"]["status"] == "initiated"
        assert result["call"]["execution_id"] == "exec-smoke-1"
        assert dial.called

        async with pool.connection() as conn:
            async with conn.transaction():
                n = await poll_non_terminal_calls(conn)
        assert n >= 1
        assert poll.called

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT status, pickup, cost_cents, cost_inr, conversation_duration, termination_reason FROM calls WHERE stall_id = %s",
                (stall["id"],),
            )
            row = await cur.fetchone()
            assert row["status"] == "completed"
            assert row["pickup"] is True
            assert row["conversation_duration"] == 42
            assert row["cost_cents"] == 25
            assert abs(float(row["cost_inr"]) - 0.25 * 88) < 0.01
            assert row["termination_reason"] == "hangup"

        # Webhook with opt-out
        async with pool.connection() as conn:
            async with conn.transaction():
                wh = await handle_bolna_webhook(
                    conn,
                    {
                        "execution_id": "exec-smoke-1",
                        "status": "completed",
                        "extracted_data": {"outcome": "opted_out"},
                        "conversation_duration": 42,
                    },
                )
        assert wh["ok"] is True
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT opted_out FROM users WHERE id = %s", (uid,))
            assert (await cur.fetchone())["opted_out"] is True

    # restore dry_run
    os.environ["DRY_RUN"] = "1"
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_no_double_dial_concurrent_ticks():
    """Two concurrent ticks must not create two calls for the same stall."""
    os.environ["DRY_RUN"] = "1"
    get_settings.cache_clear()

    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            uid, stall = await _make_stalled_user(conn, suffix="race", name="Race User")
            # Pre-create a call_now decision so dial step has work; or use force then clear?
            # Simpler: open stall, lower threshold, run two ticks concurrently.
            await conn.execute(
                "UPDATE policies SET min_score = 0, holdout_pct = 0, call_window_start = '00:00', call_window_end = '23:59' WHERE playbook_key = 'reactivation'"
            )

    results = await asyncio.gather(run_tick(), run_tick())
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*)::int AS n FROM calls WHERE stall_id = %s",
            (stall["id"],),
        )
        n = (await cur.fetchone())["n"]
        cur = await conn.execute(
            "SELECT COUNT(*)::int AS n FROM calls WHERE user_id = %s",
            (uid,),
        )
        n_user = (await cur.fetchone())["n"]

    assert n <= 1, f"double dial: {n} calls for stall {stall['id']}; ticks={results}"
    assert n_user == n, f"unexpected extra calls for user: stall_calls={n} user_calls={n_user} ticks={results}"


@pytest.mark.asyncio
async def test_call_preview_keys():
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            uid, _ = await _make_stalled_user(conn, suffix="prev", name="John Doe")
            from app.call_context import call_preview

            preview = await call_preview(conn, uid)
    assert preview["agent_env"] == "BOLNA_AGENT_ID_REACTIVATION"
    assert preview["agent_id_configured"] is True
    assert preview["playbook"] == "reactivation"
    ud = preview["user_data"]
    assert ud["Customer_Name"] == "John"  # first token even for John Doe
    assert set(ud.keys()) == {"Customer_Name", "stage_label", "goal", "script_hint"}


@pytest.mark.asyncio
async def test_referral_user_data_has_referrer():
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            ref_id = "smoke-referrer"
            await conn.execute(
                """
                INSERT INTO users (id, name, phone, consent, referral_code, current_stage)
                VALUES (%s, 'Vikram Singh', %s, TRUE, 'REFVIK', 'deposit')
                ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name
                """,
                (ref_id, TEAM),
            )
            uid, stall = await _make_stalled_user(
                conn, suffix="ref", name="Neha Kapoor", playbook="referral", stage="profile"
            )
            await conn.execute(
                "UPDATE users SET referrer_user_id = %s, ref_status = 'resolved' WHERE id = %s",
                (ref_id, uid),
            )
            ud = await build_user_data(
                conn, user_id=uid, playbook_key="referral", stage_key="profile", hint_text="hi"
            )
    assert ud["Customer_Name"] == "Neha"
    assert ud["referrer_first_name"] == "Vikram"
    assert set(ud.keys()) == {
        "Customer_Name",
        "referrer_first_name",
        "stage_label",
        "goal",
        "script_hint",
    }
