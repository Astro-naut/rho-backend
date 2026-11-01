"""Dashboard analytics — public aggregated reads + threshold what-if / save."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection

from app.config import get_settings

log = logging.getLogger("rho.dashboard")
IST = ZoneInfo("Asia/Kolkata")
STAGES = ["signup", "profile", "kyc", "bank", "plan", "deposit"]

FRIENDLY_DROP = {
    "stall_resolved": "User already moved on",
    "no_consent": "No consent",
    "opted_out": "Opted out of calls",
    "on_dnd": "Do-not-disturb",
    "outside_window": "Outside calling hours",
    "user_cap": "Already called this week",
    "daily_budget": "Budget reached",
    "holdout": "Control group",
    "below_threshold": "Unlikely to help",
    "bandit": "Scored call",
    "force": "Forced (dev)",
}
REASON_LABEL = {
    "trust": "Trust",
    "fees": "Fees",
    "kyc_confusion": "KYC confusion",
    "no_time": "No time",
    "tech_error": "Technical error",
    "other": "Other",
}
REASON_INSIGHT = {
    "trust": "Trust is a top concern — reinforce security cues and brand reassurance on the call and in-app.",
    "fees": "Fee concerns are blocking conversions — coach agents to explain the fee structure clearly.",
    "kyc_confusion": "KYC confusion is a top blocker — consider simplifying document upload and verification steps.",
    "no_time": "Users say they have no time — offer shorter callbacks or an in-app resume link.",
    "tech_error": "Technical errors during onboarding — investigate form failures and app crashes.",
    "other": "Mixed or unclear reasons — review a sample of call summaries for emerging themes.",
}
OUTCOME_LABEL = {
    "will_complete": "Will complete",
    "callback_requested": "Callback requested",
    "not_interested": "Not interested",
    "opted_out": "Opted out",
    "completed": "Completed",
    "unknown": "Unknown",
}


def parse_range(range_q: Optional[str], days: Optional[int] = None) -> int:
    """Map range=today|7|14 (or legacy days=) to day count."""
    if range_q:
        r = str(range_q).strip().lower()
        if r in {"today", "1", "1d"}:
            return 1
        if r in {"7", "7d", "week"}:
            return 7
        if r in {"14", "14d"}:
            return 14
    if days is not None:
        return max(1, min(int(days), 90))
    return 7


def _pct(n: float, d: float) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def mask_user_id(uid: str) -> str:
    if not uid:
        return ""
    if len(uid) <= 4:
        return uid[0] + "***"
    return uid[:3] + "…" + uid[-2:]


async def _avg_policy_threshold(conn: AsyncConnection) -> float:
    cur = await conn.execute("SELECT AVG(min_score)::float AS t FROM policies")
    row = await cur.fetchone()
    return float(row["t"] or 0.35)


async def _daily_budget(conn: AsyncConnection) -> float:
    cur = await conn.execute("SELECT AVG(daily_budget_inr)::float AS b FROM policies")
    row = await cur.fetchone()
    return float(row["b"] or 500)


async def _conversion_value(conn: AsyncConnection) -> float:
    cur = await conn.execute("SELECT AVG(conversion_value_inr)::float AS v FROM policies")
    row = await cur.fetchone()
    return float(row["v"] or 500)


async def _optout_penalty(conn: AsyncConnection) -> float:
    cur = await conn.execute("SELECT AVG(optout_penalty_inr)::float AS v FROM policies")
    row = await cur.fetchone()
    return float(row["v"] or 200)


async def _stall_converted(
    conn: AsyncConnection,
    *,
    user_id: str,
    from_stage: Optional[str],
    after: datetime,
    within_hours: float = 48.0,
) -> bool:
    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    deadline = after + timedelta(hours=within_hours)
    # Stall closed for progress
    cur = await conn.execute(
        """
        SELECT 1 FROM stalls
         WHERE user_id = %s
           AND closed_at IS NOT NULL
           AND close_reason = 'progress'
           AND closed_at >= %s AND closed_at <= %s
         LIMIT 1
        """,
        (user_id, after, deadline),
    )
    if await cur.fetchone():
        return True
    # Deposit completed event
    cur = await conn.execute(
        """
        SELECT 1 FROM events
         WHERE user_id = %s
           AND (event = 'deposit_completed' OR (stage = 'deposit' AND event = 'completed'))
           AND ts >= %s AND ts <= %s
         LIMIT 1
        """,
        (user_id, after, deadline),
    )
    if await cur.fetchone():
        return True
    # Next stage progress
    if from_stage and from_stage in STAGES:
        later = STAGES[STAGES.index(from_stage) + 1 :]
        if later:
            cur = await conn.execute(
                """
                SELECT 1 FROM events
                 WHERE user_id = %s
                   AND stage = ANY(%s)
                   AND event = 'completed'
                   AND ts >= %s AND ts <= %s
                 LIMIT 1
                """,
                (user_id, later, after, deadline),
            )
            if await cur.fetchone():
                return True
    return False


def _reason_code_from_call(extracted: Any, termination_reason: Optional[str] = None) -> str:
    if isinstance(extracted, str):
        try:
            extracted = json.loads(extracted)
        except json.JSONDecodeError:
            extracted = {}
    extracted = extracted or {}
    rc = extracted.get("reason_code") or extracted.get("reason") or termination_reason
    if not rc:
        return "other"
    rc = str(rc).lower().strip()
    if rc in REASON_LABEL:
        return rc
    for key in REASON_LABEL:
        if key in rc:
            return key
    return "other"


def _outcome_from_call(extracted: Any) -> str:
    if isinstance(extracted, str):
        try:
            extracted = json.loads(extracted)
        except json.JSONDecodeError:
            extracted = {}
    extracted = extracted or {}
    oc = extracted.get("outcome") or extracted.get("result") or "unknown"
    return str(oc).lower().strip()


async def overview(conn: AsyncConnection, *, range_q: Optional[str] = None, days: Optional[int] = None) -> dict[str, Any]:
    days_n = parse_range(range_q, days)
    settings = get_settings()
    inr = float(settings.inr_per_usd)
    now = datetime.now(IST)
    start = now - timedelta(days=days_n)
    prev_start = start - timedelta(days=days_n)
    threshold = await _avg_policy_threshold(conn)
    daily_budget = await _daily_budget(conn)
    conv_value = await _conversion_value(conn)
    optout_pen = await _optout_penalty(conn)

    def to_utc(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=IST).astimezone(timezone.utc)
        return dt.astimezone(timezone.utc)

    start_u, now_u, prev_u = to_utc(start), to_utc(now), to_utc(prev_start)

    cur = await conn.execute(
        "SELECT stage_key, COUNT(*)::int AS n FROM stalls WHERE status = 'open' GROUP BY stage_key"
    )
    open_by_stage = {r["stage_key"]: r["n"] for r in await cur.fetchall()}
    open_now_total = sum(open_by_stage.values())

    async def stalled_opened(lo, hi) -> dict[str, int]:
        c = await conn.execute(
            """
            SELECT stage_key, COUNT(*)::int AS n FROM stalls
             WHERE opened_at >= %s AND opened_at <= %s
             GROUP BY stage_key
            """,
            (lo, hi),
        )
        return {r["stage_key"]: r["n"] for r in await c.fetchall()}

    stalled_cur_map = await stalled_opened(start_u, now_u)
    stalled_prev_map = await stalled_opened(prev_u, start_u)
    stalled_cur = sum(stalled_cur_map.values())
    stalled_prev = sum(stalled_prev_map.values())

    async def period_metrics(lo, hi) -> dict[str, Any]:
        c = await conn.execute(
            """
            SELECT id, stall_id, user_id, action,
                   COALESCE(reason, filter_reason) AS reason,
                   score, holdout, context, created_at, stage_key, playbook_key
              FROM decisions
             WHERE created_at >= %s AND created_at <= %s
            """,
            (lo, hi),
        )
        d_rows = [dict(r) for r in await c.fetchall()]
        c = await conn.execute(
            """
            SELECT c.*, d.holdout AS d_holdout
              FROM calls c
              LEFT JOIN decisions d ON d.id = c.decision_id
             WHERE c.created_at >= %s AND c.created_at <= %s
            """,
            (lo, hi),
        )
        c_rows = [dict(r) for r in await c.fetchall()]

        called_dec = [r for r in d_rows if r["action"] == "call_now"]
        messaged = [r for r in d_rows if r["action"] == "message"]
        skipped = [r for r in d_rows if r["action"] in {"none", "call_later"}]
        holdouts = [r for r in d_rows if r["holdout"]]

        picked = [
            r for r in c_rows
            if (r.get("pickup") is True)
            or (r.get("pickup") is None and int(r.get("conversation_duration") or 0) > 0)
        ]
        spend_cents = sum(int(r.get("cost_cents") or 0) for r in c_rows)
        spend_inr = sum(float(r.get("cost_inr") or 0) for r in c_rows)
        if spend_inr == 0 and spend_cents:
            spend_inr = (spend_cents / 100.0) * inr

        converted_ids: list[int] = []
        for r in c_rows:
            after = r["created_at"]
            if after and after.tzinfo is None:
                after = after.replace(tzinfo=timezone.utc)
            ok = await _stall_converted(
                conn, user_id=r["user_id"], from_stage=r.get("stage_key"), after=after
            )
            if ok:
                converted_ids.append(int(r["id"]))
        converted = len(converted_ids)

        holdout_conv = 0
        for h in holdouts:
            after = h["created_at"]
            if after and after.tzinfo is None:
                after = after.replace(tzinfo=timezone.utc)
            if await _stall_converted(
                conn, user_id=h["user_id"], from_stage=h.get("stage_key"), after=after
            ):
                holdout_conv += 1

        called_n = len(c_rows)
        conv_rate = (converted / called_n) if called_n else 0.0
        holdout_rate = (holdout_conv / len(holdouts)) if holdouts else 0.0
        lift_pp = round((conv_rate - holdout_rate) * 100, 1) if holdouts else None

        by_stage = {s: {"stalled": 0, "called": 0, "converted": 0, "spend_cents": 0, "spend_inr": 0.0} for s in STAGES}
        smap = await stalled_opened(lo, hi)
        for s, n in smap.items():
            if s in by_stage:
                by_stage[s]["stalled"] = n
        for r in c_rows:
            stage = r.get("stage_key") or "signup"
            if stage not in by_stage:
                continue
            by_stage[stage]["called"] += 1
            by_stage[stage]["spend_cents"] += int(r.get("cost_cents") or 0)
            by_stage[stage]["spend_inr"] += float(r.get("cost_inr") or 0)
            if int(r["id"]) in converted_ids:
                by_stage[stage]["converted"] += 1

        drop_counts: dict[str, int] = {}
        for r in skipped + messaged:
            key = (r.get("reason") or "other").split(" — ")[0].split(" - ")[0].strip()
            label = FRIENDLY_DROP.get(key, key.replace("_", " ").title())
            drop_counts[label] = drop_counts.get(label, 0) + 1

        reason_counts: dict[str, int] = {}
        outcome_all: dict[str, int] = {}
        non_converted_picked = 0
        for r in picked:
            oc = _outcome_from_call(r.get("extracted_data"))
            outcome_all[oc] = outcome_all.get(oc, 0) + 1
            if int(r["id"]) in converted_ids:
                continue
            non_converted_picked += 1
            rc = _reason_code_from_call(r.get("extracted_data"), r.get("termination_reason"))
            reason_counts[rc] = reason_counts.get(rc, 0) + 1

        return {
            "stalled": sum(by_stage[s]["stalled"] for s in STAGES),
            "call_decisions": len(called_dec),
            "calls_made": called_n,
            "messaged": len(messaged),
            "skipped": len(skipped),
            "chose_not_to_call": len(skipped) + len(messaged),
            "picked_up": len(picked),
            "converted": converted,
            "conversion_rate": round(conv_rate, 4),
            "holdout_n": len(holdouts),
            "holdout_converted": holdout_conv,
            "holdout_rate": round(holdout_rate, 4),
            "lift_pp": lift_pp,
            "spend_cents": spend_cents,
            "spend_inr": round(spend_inr, 2),
            "cost_per_call_inr": round(spend_inr / called_n, 2) if called_n else None,
            "cost_per_converted_inr": round(spend_inr / converted, 2) if converted else None,
            "by_stage": by_stage,
            "drop_counts": drop_counts,
            "reason_counts": reason_counts,
            "outcome_all": outcome_all,
            "non_converted_picked": non_converted_picked,
        }

    cur_m = await period_metrics(start_u, now_u)
    prev_m = await period_metrics(prev_u, start_u)

    def delta(a, b):
        if a is None or b is None:
            return None
        return a - b

    decided = cur_m["calls_made"] + cur_m["messaged"] + cur_m["skipped"]
    flow_base = decided if decided else 1

    stage_rows = []
    worst_id = None
    worst_score = -1.0
    for s in STAGES:
        b = cur_m["by_stage"][s]
        cost = b["spend_inr"] if b["spend_inr"] else (b["spend_cents"] / 100.0) * inr
        conv_pct = _pct(b["converted"], b["called"]) if b["called"] else 0.0
        score = b["stalled"] * (1.0 - (b["converted"] / b["called"] if b["called"] else 0.0))
        if b["stalled"] > 0 and score > worst_score:
            worst_score = score
            worst_id = s
        stage_rows.append(
            {
                "stage": s,
                "label": s.upper() if s == "kyc" else s.capitalize(),
                "stalled": b["stalled"],
                "called": b["called"],
                "converted": b["converted"],
                "conversion_pct": conv_pct,
                "cost_inr": round(cost, 2),
            }
        )

    reason_total = sum(cur_m["reason_counts"].values()) or 1
    reasons = [
        {
            "code": code,
            "label": REASON_LABEL.get(code, code.replace("_", " ").title()),
            "count": n,
            "pct": _pct(n, reason_total),
            "insight": REASON_INSIGHT.get(code, REASON_INSIGHT["other"]),
        }
        for code, n in sorted(cur_m["reason_counts"].items(), key=lambda x: -x[1])
    ]
    outcomes = [
        {
            "code": k,
            "label": OUTCOME_LABEL.get(k, k.replace("_", " ").title()),
            "count": v,
            "pct": _pct(v, sum(cur_m["outcome_all"].values()) or 1),
        }
        for k, v in sorted(cur_m["outcome_all"].items(), key=lambda x: -x[1])
    ]
    drops = [
        {"label": k, "count": v, "pct": _pct(v, cur_m["chose_not_to_call"] or 1)}
        for k, v in sorted(cur_m["drop_counts"].items(), key=lambda x: -x[1])
    ]

    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    c = await conn.execute(
        """
        SELECT COALESCE(SUM(COALESCE(cost_inr, 0)), 0)::float AS s,
               COALESCE(SUM(COALESCE(cost_cents, 0)), 0)::int AS cents
          FROM calls
         WHERE created_at >= %s
        """,
        (to_utc(day_start),),
    )
    tod = await c.fetchone()
    today_spend = float(tod["s"] or 0)
    if today_spend == 0 and tod["cents"]:
        today_spend = (tod["cents"] / 100.0) * inr
    daily_rate = cur_m["spend_inr"] / days_n if days_n else 0.0
    projected_monthly = round(daily_rate * 30, 2)
    users_stalled = open_now_total
    calls_of_stalled_pct = _pct(cur_m["calls_made"], users_stalled) if users_stalled else _pct(cur_m["calls_made"], cur_m["stalled"] or 1)

    hero = {
        "users_stalled": users_stalled,
        "users_stalled_delta": delta(stalled_cur, stalled_prev),
        "users_stalled_period": stalled_cur,
        "calls_made": cur_m["calls_made"],
        "calls_made_delta": delta(cur_m["calls_made"], prev_m["calls_made"]),
        "calls_pct_of_stalled": calls_of_stalled_pct,
        "chose_not_to_call": cur_m["chose_not_to_call"],
        "converted": cur_m["converted"],
        "converted_delta": delta(cur_m["converted"], prev_m["converted"]),
        "conversion_rate_pct": round(cur_m["conversion_rate"] * 100, 1),
        "lift_pp": cur_m["lift_pp"],
        "spend_inr": cur_m["spend_inr"],
        "spend_delta_inr": round(delta(cur_m["spend_inr"], prev_m["spend_inr"]) or 0, 2),
        "cost_per_converted_inr": cur_m["cost_per_converted_inr"],
    }
    flow = {
        "stalled": stalled_cur if stalled_cur else users_stalled,
        "called": cur_m["calls_made"],
        "messaged": cur_m["messaged"],
        "skipped": cur_m["skipped"],
        "picked_up": cur_m["picked_up"],
        "not_picked_up": max(0, cur_m["calls_made"] - cur_m["picked_up"]),
        "converted": cur_m["converted"],
        "not_converted": max(0, cur_m["picked_up"] - cur_m["converted"])
        if cur_m["picked_up"] >= cur_m["converted"]
        else max(0, cur_m["calls_made"] - cur_m["converted"]),
        "pct_called": _pct(cur_m["calls_made"], flow_base),
        "pct_messaged": _pct(cur_m["messaged"], flow_base),
        "pct_skipped": _pct(cur_m["skipped"], flow_base),
        "pct_picked_up": _pct(cur_m["picked_up"], cur_m["calls_made"] or 1),
        "pct_converted": _pct(cur_m["converted"], cur_m["picked_up"] or 1),
    }
    cost = {
        "spend_inr": cur_m["spend_inr"],
        "cost_per_call_inr": cur_m["cost_per_call_inr"],
        "cost_per_converted_inr": cur_m["cost_per_converted_inr"],
        "projected_monthly_inr": projected_monthly,
        "budget_today_inr": daily_budget,
        "spend_today_inr": round(today_spend, 2),
        "budget_used_pct": min(100.0, round(100.0 * today_spend / daily_budget, 1)) if daily_budget else 0.0,
        "inr_per_usd": inr,
        "conversion_value_inr_assumption": conv_value,
        "optout_penalty_inr_assumption": optout_pen,
        "assumptions_note": "conversion_value_inr / optout_penalty_inr are ASSUMPTIONS (demo defaults).",
    }
    return {
        "days": days_n,
        "range": {"start": start.isoformat(), "end": now.isoformat(), "key": range_q or str(days_n)},
        "as_of": now.isoformat(),
        "hero": hero,
        "flow": flow,
        "stages": stage_rows,
        "worst_stage": worst_id,
        "reasons": reasons,
        "outcomes": outcomes,
        "drops": drops,
        "cost": cost,
        "threshold": threshold,
        "open_stalls_by_stage": {s: int(open_by_stage.get(s, 0)) for s in STAGES},
    }


async def learning(conn: AsyncConnection, *, range_q: Optional[str] = None) -> dict[str, Any]:
    settings = get_settings()
    inr = float(settings.inr_per_usd)
    days_n = parse_range(range_q, None) if range_q else 30
    now = datetime.now(timezone.utc)
    lo = now - timedelta(days=max(days_n, 14))

    def outcome_val(outcome: Optional[str], duration: int, cost_cents: int) -> float:
        cost_pen = ((cost_cents or 0) / 100.0) * inr / 50.0
        if not duration:
            return -cost_pen * 0.2
        table = {
            "will_complete": 1.0,
            "callback_requested": 0.4,
            "not_interested": 0.0,
            "opted_out": -0.5,
            "completed": 1.0,
        }
        return table.get(outcome or "", 0.0) - cost_pen

    cur = await conn.execute(
        """
        SELECT d.created_at, c.conversation_duration, c.cost_cents, c.extracted_data
          FROM decisions d
          JOIN calls c ON c.decision_id = d.id
         WHERE d.action = 'call_now' AND d.created_at >= %s
         ORDER BY d.created_at ASC
        """,
        (lo,),
    )
    rho_rows = await cur.fetchall()
    cur = await conn.execute(
        """
        SELECT AVG(
            CASE
              WHEN COALESCE(conversation_duration,0) <= 0 THEN -0.05
              ELSE 0.2
            END - (COALESCE(cost_cents,0)/100.0)*%s/50.0
        )::float AS avg_r FROM calls WHERE created_at >= %s
        """,
        (inr, lo),
    )
    avg_call_reward = float((await cur.fetchone())["avg_r"] or 0.0)
    cur = await conn.execute(
        """
        SELECT created_at, COALESCE(reason, filter_reason) AS reason, action
          FROM decisions
         WHERE created_at >= %s
           AND COALESCE(reason, filter_reason) NOT IN ('no_consent', 'on_dnd', 'opted_out')
           AND COALESCE(reason, filter_reason) NOT LIKE 'no_consent%%'
           AND COALESCE(reason, filter_reason) NOT LIKE 'on_dnd%%'
           AND COALESCE(reason, filter_reason) NOT LIKE 'opted_out%%'
         ORDER BY created_at ASC
        """,
        (lo,),
    )
    base_rows = await cur.fetchall()

    rho_by_day: dict[str, float] = defaultdict(float)
    for r in rho_rows:
        dk = (r["created_at"].astimezone(IST).date().isoformat() if r["created_at"] else "")
        oc = _outcome_from_call(r["extracted_data"])
        rho_by_day[dk] += outcome_val(oc, int(r["conversation_duration"] or 0), int(r["cost_cents"] or 0))

    base_by_day: dict[str, float] = defaultdict(float)
    for r in base_rows:
        dk = (r["created_at"].astimezone(IST).date().isoformat() if r["created_at"] else "")
        base_by_day[dk] += avg_call_reward

    days = sorted(set(rho_by_day) | set(base_by_day))
    cum_rho = cum_base = 0.0
    series = []
    for d in days:
        cum_rho += rho_by_day.get(d, 0.0)
        cum_base += base_by_day.get(d, 0.0)
        series.append(
            {
                "day": d,
                "rho_daily": round(rho_by_day.get(d, 0.0), 3),
                "baseline_daily": round(base_by_day.get(d, 0.0), 3),
                "rho_cumulative": round(cum_rho, 3),
                "baseline_cumulative": round(cum_base, 3),
            }
        )
    return {
        "avg_call_reward": round(avg_call_reward, 4),
        "series": series,
        "rho_total": round(cum_rho, 3),
        "baseline_total": round(cum_base, 3),
    }


async def list_decisions(
    conn: AsyncConnection, *, limit: int = 12, offset: int = 0
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    cur = await conn.execute("SELECT COUNT(*)::int AS n FROM decisions")
    total = (await cur.fetchone())["n"]
    cur = await conn.execute(
        """
        SELECT id, stall_id, user_id, action,
               COALESCE(reason, filter_reason) AS reason,
               score, holdout, created_at
          FROM decisions
         ORDER BY id DESC
         LIMIT %s OFFSET %s
        """,
        (limit, offset),
    )
    rows = await cur.fetchall()
    items = []
    for r in rows:
        items.append(
            {
                "id": r["id"],
                "stall_id": r["stall_id"],
                "user_id": mask_user_id(r["user_id"]),
                "user_id_masked": mask_user_id(r["user_id"]),
                "action": r["action"],
                "reason": r["reason"],
                "score": r["score"],
                "holdout": r["holdout"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
        )
    return {"total": total, "limit": limit, "offset": offset, "decisions": items}


async def simulate_threshold(conn: AsyncConnection, threshold: float) -> dict[str, Any]:
    """What-if from recent decision scores on open stalls / recent none+call decisions."""
    settings = get_settings()
    inr = float(settings.inr_per_usd)
    th = float(threshold)
    # Recent scores (last 14d) for projection
    cur = await conn.execute(
        """
        SELECT score, action FROM decisions
         WHERE score IS NOT NULL
           AND created_at >= now() - interval '14 days'
        """
    )
    rows = await cur.fetchall()
    would_call = [r for r in rows if float(r["score"]) >= th]
    # Open stalls as volume proxy
    cur = await conn.execute("SELECT COUNT(*)::int AS n FROM stalls WHERE status = 'open'")
    open_n = (await cur.fetchone())["n"]
    # Historical conversion among call_now with score >= th
    cur = await conn.execute(
        """
        SELECT COUNT(*)::int AS n FROM decisions d
         JOIN calls c ON c.decision_id = d.id
         WHERE d.action = 'call_now' AND d.score IS NOT NULL AND d.score >= %s
        """,
        (th,),
    )
    hist_calls = (await cur.fetchone())["n"]
    # Rough expected reactivation rate from rewards / conversions
    cur = await conn.execute(
        """
        SELECT COUNT(*) FILTER (WHERE reward_type = 'conversion')::float
               / NULLIF(COUNT(*),0) AS rate
          FROM rewards
         WHERE reward_type IN ('conversion', 'none')
        """
    )
    row = await cur.fetchone()
    rate = float(row["rate"] or 0.15)
    # Cost estimate: reserved or avg cost
    cur = await conn.execute(
        """
        SELECT AVG(COALESCE(cost_inr, (COALESCE(cost_cents,0)/100.0)*%s))::float AS a
          FROM calls WHERE cost_cents IS NOT NULL OR cost_inr IS NOT NULL
        """,
        (inr,),
    )
    avg_cost = float((await cur.fetchone())["a"] or 15.0)
    # Project onto open stalls using fraction of recent scores that pass
    frac = (len(would_call) / len(rows)) if rows else (0.5 if th <= 0.35 else max(0.05, 1.0 - th))
    calls = int(round(open_n * frac)) if open_n else len(would_call)
    projected_cost = round(calls * avg_cost, 2)
    expected = round(calls * rate, 1)
    return {
        "threshold": th,
        "calls": calls,
        "projected_cost_inr": projected_cost,
        "expected_reactivations": expected,
        "open_stalls": open_n,
        "recent_scores_n": len(rows),
        "recent_passing_n": len(would_call),
        "hist_calls_at_or_above": hist_calls,
        "assumed_conversion_rate": round(rate, 4),
        "assumed_cost_per_call_inr": round(avg_cost, 2),
    }


async def get_threshold(conn: AsyncConnection) -> dict[str, Any]:
    # Use reactivation as primary for dashboard slider; include both
    cur = await conn.execute(
        "SELECT playbook_key, min_score FROM policies ORDER BY playbook_key"
    )
    rows = await cur.fetchall()
    by = {r["playbook_key"]: float(r["min_score"]) for r in rows}
    primary = by.get("reactivation", await _avg_policy_threshold(conn))
    return {
        "threshold": primary,
        "min_score": primary,
        "by_playbook": by,
    }


async def put_threshold(conn: AsyncConnection, threshold: float) -> dict[str, Any]:
    th = max(0.0, min(1.0, float(threshold)))
    await conn.execute(
        "UPDATE policies SET min_score = %s, updated_at = now()",
        (th,),
    )
    return await get_threshold(conn)
