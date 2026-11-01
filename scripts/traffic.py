#!/usr/bin/env python3
"""Synthetic Sprout traffic against public Rho endpoints (PWA payload shapes).

Uses ONLY public POST /events and POST /users/{id}/profile (+ GET referral-code).
No hidden ground truth — personas are named in logs only.

Generated users:
  phones in reserved non-dialable range +9199999xxxxx (consent=false)
TEAM_PHONES from env:
  consent=true (real dialable team numbers)

Modes:
  --backfill-days 14   emit events with past timestamps (ingest clamps >30d)
  --live               real-time loop, sped up with --speed

Examples:
  python scripts/traffic.py --backfill-days 14 --base-url http://127.0.0.1:8000
  python scripts/traffic.py --live --speed 30 --base-url https://rho-backend.onrender.com
"""

from __future__ import annotations

import argparse
import os
import random
import string
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

STAGES = ["signup", "profile", "kyc", "bank", "plan", "deposit"]

# Reserved non-dialable Indian mobile-looking range for synthetic users.
# Documented: +9199999xxxxx — never placed in team_phones; consent=false.
SYNTH_PREFIX = "+9199999"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def synth_phone(n: int) -> str:
    return f"{SYNTH_PREFIX}{n % 100000:05d}"


def uid(prefix: str = "traf") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@dataclass
class Persona:
    name: str
    weight: float


PERSONAS = [
    Persona("completer", 0.28),
    Persona("kyc_stall_forever", 0.22),
    Persona("kyc_stall_returns_hours_later", 0.18),
    Persona("bank_abandon", 0.17),
    Persona("referral_invitee_stalls", 0.15),
]


def pick_persona() -> str:
    names = [p.name for p in PERSONAS]
    weights = [p.weight for p in PERSONAS]
    return random.choices(names, weights=weights, k=1)[0]


class Client:
    def __init__(self, base: str, timeout: float = 20.0):
        self.base = base.rstrip("/")
        self.http = httpx.Client(base_url=self.base, timeout=timeout)

    def close(self) -> None:
        self.http.close()

    def profile(
        self,
        user_id: str,
        *,
        phone: Optional[str],
        name: str,
        consent: bool,
        ref_code: Optional[str] = None,
        language: str = "hinglish",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "phone": phone,
            "language": language,
            "consent": consent,
            "name": name,
        }
        if ref_code:
            body["ref_code"] = ref_code
        r = self.http.post(f"/users/{user_id}/profile", json=body)
        r.raise_for_status()
        return r.json()

    def event(
        self,
        user_id: str,
        stage: str,
        event: str = "completed",
        *,
        ts: datetime,
        ref_code: Optional[str] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "user_id": user_id,
            "stage": stage,
            "event": event,
            "ts": ts.isoformat().replace("+00:00", "Z"),
        }
        if ref_code:
            body["ref_code"] = ref_code
        r = self.http.post("/events", json=body)
        r.raise_for_status()
        return r.json()

    def referral_code(self, user_id: str) -> str:
        r = self.http.get(f"/users/{user_id}/referral-code")
        r.raise_for_status()
        return r.json()["referral_code"]


def sleep_or_advance(live: bool, seconds: float, speed: float) -> None:
    if not live:
        return
    time.sleep(max(0.0, seconds / max(speed, 0.01)))


def run_persona(
    client: Client,
    persona: str,
    *,
    anchor: datetime,
    live: bool,
    speed: float,
    team_phones: list[str],
    counter: list[int],
) -> None:
    counter[0] += 1
    n = counter[0]
    # Prefer team phone rarely for live demos
    use_team = bool(team_phones) and random.random() < 0.08
    phone = random.choice(team_phones) if use_team else synth_phone(n)
    consent = use_team
    name = random.choice(
        ["Asha Verma", "Rahul Mehta", "Priya Nair", "John Doe", "Neha", "arjun singh", "  "]
    ).strip() or "Neha"
    user_id = uid(persona[:6])

    def at(offset_min: float) -> datetime:
        return anchor + timedelta(minutes=offset_min)

    t = 0.0
    client.profile(user_id, phone=phone, name=name, consent=consent)
    client.event(user_id, "signup", ts=at(t))
    sleep_or_advance(live, 40, speed)
    t += 2
    client.event(user_id, "profile", ts=at(t))
    sleep_or_advance(live, 90, speed)
    t += 4

    if persona == "completer":
        for stage, gap in [("kyc", 8), ("bank", 12), ("plan", 10), ("deposit", 15)]:
            t += gap
            client.event(user_id, stage, ts=at(t))
            sleep_or_advance(live, gap * 60 / 4, speed)
        return

    if persona == "kyc_stall_forever":
        # Enter kyc stage via profile complete; never finish kyc
        return

    if persona == "kyc_stall_returns_hours_later":
        # Stall then complete hours later
        t += 6 * 60
        client.event(user_id, "kyc", ts=at(t))
        sleep_or_advance(live, 20, speed)
        t += 20
        client.event(user_id, "bank", ts=at(t))
        return

    if persona == "bank_abandon":
        t += 10
        client.event(user_id, "kyc", ts=at(t))
        sleep_or_advance(live, 30, speed)
        # Enter bank via kyc complete; abandon
        return

    if persona == "referral_invitee_stalls":
        # Create referrer first, fetch code, then invitee with ref_code
        ref_id = uid("ref")
        ref_phone = synth_phone(n + 90000)
        client.profile(ref_id, phone=ref_phone, name="Referrer Friend", consent=False)
        client.event(ref_id, "signup", ts=at(t - 30))
        client.event(ref_id, "profile", ts=at(t - 25))
        try:
            code = client.referral_code(ref_id)
        except Exception:
            code = None
        # Invitee
        invitee = uid("inv")
        inv_phone = synth_phone(n + 50000)
        client.profile(
            invitee,
            phone=inv_phone,
            name="Invitee User",
            consent=False,
            ref_code=code,
        )
        client.event(invitee, "signup", ts=at(t), ref_code=code)
        sleep_or_advance(live, 50, speed)
        client.event(invitee, "profile", ts=at(t + 3), ref_code=code)
        # Stall at kyc
        return


def backfill(client: Client, days: int, team_phones: list[str], per_day: int) -> None:
    now = _now()
    counter = [0]
    for day in range(days, 0, -1):
        day_anchor = now - timedelta(days=day)
        # Spread within the day during IST daytime-ish
        for _ in range(per_day):
            persona = pick_persona()
            jitter = random.uniform(2.5, 12.5)  # hours into the day
            anchor = day_anchor + timedelta(hours=jitter)
            # Keep within 30-day clamp
            if (now - anchor).days >= 29:
                anchor = now - timedelta(days=29, hours=1)
            try:
                run_persona(
                    client,
                    persona,
                    anchor=anchor,
                    live=False,
                    speed=1,
                    team_phones=team_phones,
                    counter=counter,
                )
                print(f"[backfill] day-{day} {persona} ok")
            except Exception as e:
                print(f"[backfill] day-{day} {persona} FAIL {e}", file=sys.stderr)


def live_loop(client: Client, speed: float, team_phones: list[str], count: int) -> None:
    counter = [0]
    for i in range(count):
        persona = pick_persona()
        try:
            run_persona(
                client,
                persona,
                anchor=_now(),
                live=True,
                speed=speed,
                team_phones=team_phones,
                counter=counter,
            )
            print(f"[live] {i+1}/{count} {persona} ok")
        except Exception as e:
            print(f"[live] {i+1}/{count} {persona} FAIL {e}", file=sys.stderr)
        sleep_or_advance(True, random.uniform(15, 45), speed)


def parse_team_phones(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    out = []
    for part in raw.replace(";", ",").split(","):
        p = part.strip()
        if p:
            out.append(p)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Rho synthetic traffic (public PWA shapes)")
    ap.add_argument("--base-url", default=os.environ.get("RHO_API", "http://127.0.0.1:8000"))
    ap.add_argument("--backfill-days", type=int, default=0)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--speed", type=float, default=20.0, help="Live speed multiplier")
    ap.add_argument("--per-day", type=int, default=8, help="Personas per backfill day")
    ap.add_argument("--count", type=int, default=20, help="Live persona count")
    ap.add_argument(
        "--team-phones",
        default=os.environ.get("TEAM_PHONES", ""),
        help="Comma-separated; override TEAM_PHONES env",
    )
    args = ap.parse_args()
    if not args.backfill_days and not args.live:
        ap.error("specify --backfill-days and/or --live")

    team = parse_team_phones(args.team_phones)
    print(
        f"base={args.base_url} synth_prefix={SYNTH_PREFIX}xxxxx "
        f"team_phones={len(team)} backfill={args.backfill_days} live={args.live}"
    )
    client = Client(args.base_url)
    try:
        if args.backfill_days:
            backfill(client, args.backfill_days, team, args.per_day)
        if args.live:
            live_loop(client, args.speed, team, args.count)
    finally:
        client.close()


if __name__ == "__main__":
    main()
