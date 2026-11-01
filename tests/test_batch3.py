"""Batch 3 tests: filters, stalls, first-name, user_data keys, cents→INR, pickup, referrals."""

from __future__ import annotations

import json
import os

os.environ.setdefault("DATABASE_URL", "postgresql://postgres@/rho?host=/workspace/rho-v2/.pgdata")
os.environ.setdefault("INR_PER_USD", "88")
os.environ.setdefault("DRY_RUN", "1")
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.bolna_client import update_call_from_execution
from app.call_context import build_user_data, customer_first_name
from app.config import get_settings
from app.decision import evaluate_stall, in_call_window, load_policy
from app.ingest import resolve_ref_code, upsert_profile
from app.stalls import detect_stalls, is_still_stalled

IST = ZoneInfo("Asia/Kolkata")
TEAM = "+919876543210"


async def _user(conn, uid, **kw):
    name = kw.get("name", "John Doe")
    phone = kw.get("phone", TEAM)
    consent = kw.get("consent", True)
    opted_out = kw.get("opted_out", False)
    dnd = kw.get("dnd", False)
    stage = kw.get("current_stage", "kyc")
    await conn.execute(
        """
        INSERT INTO users (id, name, phone, consent, opted_out, dnd, current_stage, language)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'hinglish')
        ON CONFLICT (id) DO UPDATE SET
          name=EXCLUDED.name, phone=EXCLUDED.phone, consent=EXCLUDED.consent,
          opted_out=EXCLUDED.opted_out, dnd=EXCLUDED.dnd, current_stage=EXCLUDED.current_stage
        """,
        (uid, name, phone, consent, opted_out, dnd, stage),
    )


async def _open_stall(conn, uid, stage="kyc", playbook="reactivation", minutes_ago=60):
    opened = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    cur = await conn.execute(
        """
        INSERT INTO stalls (user_id, stage_key, playbook_key, status, opened_at)
        VALUES (%s, %s, %s, 'open', %s)
        RETURNING id, user_id, stage_key, playbook_key, status, opened_at
        """,
        (uid, stage, playbook, opened),
    )
    row = await cur.fetchone()
    await conn.execute(
        """
        INSERT INTO user_stage_state (user_id, stage_key, entered_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id, stage_key) DO UPDATE SET entered_at = EXCLUDED.entered_at, completed_at = NULL
        """,
        (uid, stage, opened),
    )
    return dict(row)


# --- first-name ---
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("John Doe", "John"),
        ("Neha", "Neha"),
        ("", "there"),
        (None, "there"),
        ("  ", "there"),
        ("  arjun singh  ", "Arjun"),
        ("john", "John"),
    ],
)
def test_first_name_derivation(raw, expected):
    assert customer_first_name(raw) == expected


# --- cents -> INR ---
def test_cents_to_inr():
    get_settings.cache_clear()
    s = get_settings()
    cents = 250  # $2.50
    inr = (cents / 100.0) * float(s.inr_per_usd)
    assert s.inr_per_usd == 88.0
    assert inr == 220.0


# --- user_data keys ---
@pytest.mark.asyncio
async def test_user_data_keys_reactivation(db):
    await _user(db, "u-react", name="John Doe")
    data = await build_user_data(
        db, user_id="u-react", playbook_key="reactivation", stage_key="kyc", hint_text="hint"
    )
    assert set(data.keys()) == {"Customer_Name", "stage_label", "goal", "script_hint"}
    assert all(isinstance(v, str) for v in data.values())
    assert data["Customer_Name"] == "John"


@pytest.mark.asyncio
async def test_user_data_keys_referral(db):
    await _user(db, "u-ref", name="Referrer One")
    await _user(db, "u-inv", name="Invitee Two")
    await db.execute(
        "UPDATE users SET referrer_user_id=%s, ref_status='resolved' WHERE id=%s",
        ("u-ref", "u-inv"),
    )
    data = await build_user_data(
        db, user_id="u-inv", playbook_key="referral", stage_key="kyc", hint_text="hi"
    )
    assert set(data.keys()) == {
        "Customer_Name",
        "referrer_first_name",
        "stage_label",
        "goal",
        "script_hint",
    }
    assert data["referrer_first_name"] == "Referrer"
    assert data["Customer_Name"] == "Invitee"


# --- referral precedence ---
@pytest.mark.asyncio
async def test_referral_resolved_server_code(db):
    await upsert_profile(db, "refA", phone="+919999900001", language="hinglish", consent=False, name="Asha Verma", ref_code=None)
    cur = await db.execute("SELECT referral_code FROM users WHERE id='refA'")
    code = (await cur.fetchone())["referral_code"]
    rid, status, first = await resolve_ref_code(db, code)
    assert status == "resolved" and rid == "refA" and first == "Asha"


@pytest.mark.asyncio
async def test_referral_legacy_unique(db):
    await _user(db, "leg1", name="Unique Name")
    rid, status, first = await resolve_ref_code(db, "SPROUT-UNIQUE")
    assert status == "resolved" and rid == "leg1"


@pytest.mark.asyncio
async def test_referral_legacy_ambiguous(db):
    await _user(db, "a1", name="Sam One")
    await _user(db, "a2", name="Sam Two")
    rid, status, first = await resolve_ref_code(db, "SPROUT-SAM")
    assert rid is None and status == "ambiguous"


@pytest.mark.asyncio
async def test_referral_unknown(db):
    rid, status, first = await resolve_ref_code(db, "SPROUT-NOBODY")
    assert rid is None and status == "unknown"
    rid, status, first = await resolve_ref_code(db, "NOT-A-CODE")
    assert status == "unknown"
    rid, status, first = await resolve_ref_code(db, None)
    assert status == "unknown"


# --- stall detection ---
@pytest.mark.asyncio
async def test_stall_detection_opens_and_still_stalled(db):
    # Lower threshold for kyc reactivation to 1 minute via playbook_stages
    await db.execute(
        """
        UPDATE playbook_stages SET threshold_minutes = 1
         WHERE playbook_key='reactivation' AND stage_key='kyc'
        """
    )
    await _user(db, "stall1", current_stage="kyc")
    entered = datetime.now(timezone.utc) - timedelta(minutes=10)
    await db.execute(
        """
        INSERT INTO user_stage_state (user_id, stage_key, entered_at)
        VALUES ('stall1', 'kyc', %s)
        ON CONFLICT (user_id, stage_key) DO UPDATE SET entered_at=EXCLUDED.entered_at
        """,
        (entered,),
    )
    stats = await detect_stalls(db)
    assert stats["opened"] >= 1
    cur = await db.execute("SELECT id, status FROM stalls WHERE user_id='stall1' AND status='open'")
    row = await cur.fetchone()
    assert row is not None
    assert await is_still_stalled(db, int(row["id"])) is True


# --- filters ---
@pytest.mark.asyncio
async def test_filters_no_consent(db):
    await _user(db, "f1", consent=False)
    stall = await _open_stall(db, "f1")
    # Force window open by setting wide window already in seed; use noon IST
    noon = datetime.now(IST).replace(hour=12, minute=0, second=0, microsecond=0)
    d = await evaluate_stall(db, stall, now=noon.astimezone(timezone.utc))
    assert d["action"] == "none" and d["reason"] == "no_consent"


@pytest.mark.asyncio
async def test_filters_opted_out(db):
    await _user(db, "f2", opted_out=True)
    stall = await _open_stall(db, "f2")
    noon = datetime.now(IST).replace(hour=12, minute=0, second=0, microsecond=0)
    d = await evaluate_stall(db, stall, now=noon.astimezone(timezone.utc))
    assert d["action"] == "none" and d["reason"] == "opted_out"


@pytest.mark.asyncio
async def test_filters_on_dnd(db):
    await _user(db, "f3", dnd=True)
    stall = await _open_stall(db, "f3")
    noon = datetime.now(IST).replace(hour=12, minute=0, second=0, microsecond=0)
    d = await evaluate_stall(db, stall, now=noon.astimezone(timezone.utc))
    assert d["action"] == "none" and d["reason"] == "on_dnd"


@pytest.mark.asyncio
async def test_filters_outside_window(db):
    await _user(db, "f4")
    stall = await _open_stall(db, "f4")
    # 3am IST — outside 10-19
    night = datetime.now(IST).replace(hour=3, minute=0, second=0, microsecond=0)
    d = await evaluate_stall(db, stall, now=night.astimezone(timezone.utc))
    assert d["action"] == "call_later" and d["reason"] == "outside_window"


# --- pickup logic ---
@pytest.mark.asyncio
async def test_pickup_logic(db):
    await _user(db, "p1")
    stall = await _open_stall(db, "p1")
    # Insert decision + call
    cur = await db.execute(
        """
        INSERT INTO decisions (stall_id, user_id, playbook_key, stage_key, action, reason, score)
        VALUES (%s, 'p1', 'reactivation', 'kyc', 'call_now', 'bandit', 0.9)
        RETURNING id
        """,
        (stall["id"],),
    )
    did = (await cur.fetchone())["id"]
    cur = await db.execute(
        """
        INSERT INTO calls (stall_id, decision_id, user_id, playbook_key, stage_key, phone,
                           execution_id, status, dry_run)
        VALUES (%s, %s, 'p1', 'reactivation', 'kyc', %s, 'exec-pickup-1', 'initiated', TRUE)
        RETURNING id
        """,
        (stall["id"], did, TEAM),
    )
    call_id = (await cur.fetchone())["id"]

    # duration > 0 → pickup true
    await update_call_from_execution(
        db,
        call_id,
        {
            "id": "exec-pickup-1",
            "status": "completed",
            "conversation_duration": 42,
            "total_cost_cents": 100,
            "extracted_data": {"reason_code": "fees", "outcome": "will_complete"},
        },
    )
    cur = await db.execute("SELECT pickup, conversation_duration, cost_cents, cost_inr FROM calls WHERE id=%s", (call_id,))
    row = await cur.fetchone()
    assert row["pickup"] is True
    assert int(row["conversation_duration"] or 0) == 42

    # no duration → pickup false (reuse call row after clearing pickup)
    await db.execute(
        "UPDATE calls SET pickup=NULL, conversation_duration=NULL WHERE id=%s",
        (call_id,),
    )
    await update_call_from_execution(
        db,
        call_id,
        {"id": "exec-pickup-1", "status": "completed", "conversation_duration": 0},
    )
    cur = await db.execute("SELECT pickup FROM calls WHERE id=%s", (call_id,))
    row = await cur.fetchone()
    # COALESCE keeps previous True if we pass pickup False — check apply_execution_fields
    # Re-read: pickup = COALESCE(%s, pickup) so False should set False
    assert row["pickup"] is False



@pytest.mark.asyncio
async def test_cents_to_inr_on_call_update(db):
    await _user(db, "c1")
    stall = await _open_stall(db, "c1")
    cur = await db.execute(
        """
        INSERT INTO decisions (stall_id, user_id, playbook_key, stage_key, action, reason)
        VALUES (%s, 'c1', 'reactivation', 'kyc', 'call_now', 'force') RETURNING id
        """,
        (stall["id"],),
    )
    did = (await cur.fetchone())["id"]
    await db.execute(
        """
        INSERT INTO calls (stall_id, decision_id, user_id, playbook_key, stage_key, phone,
                           execution_id, status, dry_run)
        VALUES (%s, %s, 'c1', 'reactivation', 'kyc', %s, 'exec-cost-1', 'initiated', TRUE)
        """,
        (stall["id"], did, TEAM),
    )
    # total_cost_cents path
    cur = await db.execute("SELECT id FROM calls WHERE execution_id='exec-cost-1'")
    cid = (await cur.fetchone())["id"]
    await update_call_from_execution(
        db,
        cid,
        {
            "id": "exec-cost-1",
            "status": "completed",
            "conversation_duration": 10,
            "total_cost_cents": 100,  # $1.00
        },
    )
    cur = await db.execute(
        "SELECT cost_cents, cost_inr FROM calls WHERE execution_id='exec-cost-1'"
    )
    row = await cur.fetchone()
    assert row["cost_cents"] == 100
    assert abs(float(row["cost_inr"]) - 88.0) < 0.01
