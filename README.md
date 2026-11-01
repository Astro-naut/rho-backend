# Rho

**Rho** is a config-driven voice reactivation layer for the Sprout signup funnel.
When a user stalls mid-onboarding (or a referral invitee stalls), Rho decides
whether a Bolna voice call is worth making, places the call, learns from
outcomes, and exposes an operations dashboard.

## Architecture

```
Sprout PWA  --events/profile-->  Rho (FastAPI + Postgres)
                                      |
                                      +--> stall detection
                                      +--> decision engine (filters → holdout → Thompson bandit)
                                      +--> Bolna dial / webhook / poll
                                      +--> rewards + bandit update
                                      +--> dashboard (aggregated reads)
```

- **Playbooks:** `reactivation` (default) and `referral` (when `ref_code` resolves).
- **Agents:** Khushi (reactivation) / Ruhi (referral) — see `agents/`.
- **Safety:** real dials only when `DRY_RUN=0` **and** the phone is in `team_phones`.
- **Synthetic traffic:** `scripts/traffic.py` uses reserved non-dialable `+9199999xxxxx` with `consent=false`.

## Environment variables

| Var | Purpose | Demo default |
|-----|---------|--------------|
| `DATABASE_URL` | Postgres (Neon on Render) | required |
| `ADMIN_API_KEY` | `X-Admin-Key` for admin + threshold PUT | `change-me-admin` |
| `TICK_SECRET` | `?secret=` for `/internal/tick` | required in prod |
| `TEAM_PHONES` | Comma-separated E.164 allowlist for real calls | — |
| `DRY_RUN` | `1` = never HTTP to Bolna | `1` |
| `DEV` | Enable `POST /dev/trigger/{user_id}` | `0` |
| `WORKER_ENABLED` | In-process tick loop | `0` on Render (use cron) |
| `BOLNA_API_KEY` | Bolna bearer token | — |
| `BOLNA_API_BASE` | Bolna API host | `https://api.bolna.ai` |
| `BOLNA_AGENT_ID_REACTIVATION` | Khushi agent id | — |
| `BOLNA_AGENT_ID_REFERRAL` | Ruhi agent id | — |
| `BOLNA_WEBHOOK_SECRET` | `X-Rho-Webhook-Secret` / `?secret=` | — |
| `INR_PER_USD` | USD→INR for cost display | `88` **assumption** |
| `CORS_EXTRA` | Extra allowed origins (comma) | — |

Policy ASSUMPTIONS (seeded, editable via admin): `conversion_value_inr=₹500`, `optout_penalty_inr=₹200`.

## Local run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# Postgres: Neon URL, or pgembed socket as in tests
export DATABASE_URL=postgresql://...
export DRY_RUN=1 DEV=1 ADMIN_API_KEY=test-admin-key TICK_SECRET=test-tick-secret
export TEAM_PHONES=+91XXXXXXXXXX
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Dashboard: open `http://localhost:8000/dashboard` (or `dashboard/index.html` with `?api=http://localhost:8000`).

Traffic backfill (public endpoints only):

```bash
python scripts/traffic.py --backfill-days 14 --base-url http://127.0.0.1:8000
python scripts/traffic.py --live --speed 30 --base-url http://127.0.0.1:8000
```

Tests:

```bash
pytest -q
```

## Render + Neon deploy

1. Create a Neon Postgres database; copy the connection string → `DATABASE_URL`.
2. Create a Render Web Service from this repo (see `render.yaml`).
3. Set env vars above in the Render dashboard. Prefer `WORKER_ENABLED=0`.
4. **Verify current Render / Neon free-tier terms before the demo** — quotas and sleep policies change.

`render.yaml` build/start:

- build: `pip install -r requirements.txt`
- start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`

## Cron (cron-job.org)

Point a 1-minute (or 2-minute) job at:

```
GET https://<your-service>.onrender.com/internal/tick?secret=<TICK_SECRET>
```

This runs stall detection → decisions → dial → poll → rewards under advisory locks.

## Bolna webhook

```
POST https://<your-service>.onrender.com/webhooks/bolna
Header: X-Rho-Webhook-Secret: <BOLNA_WEBHOOK_SECRET>
```

(Also accepts `?secret=`.)

## Quick Cloudflare tunnel fallback

If Render is asleep or you need a public URL for the PWA / Bolna webhooks locally:

```bash
cloudflared tunnel --url http://localhost:8000
```

Point `RHO_API` / dashboard `?api=` at the printed `https://*.trycloudflare.com` URL.

## Dashboard

- Static UI: `dashboard/index.html` (layout matches the prior Sprout Rho dashboard).
- `API_BASE` default `https://rho-backend.onrender.com`; override with `?api=` (localStorage).
- Threshold **PUT** needs admin key (field stores `rho_admin_key` in localStorage).
- Endpoint list: `DASHBOARD_ENDPOINTS.md`.

## PWA integration

Do not push to the Sprout remote from this repo. See `pwa/PWA_DIFFS.md` and `pwa/patched/`.

## Test plan (demo)

1. **Dry run** — `DRY_RUN=1`, run `scripts/traffic.py --backfill-days 7`, open dashboard.
2. **Call preview** — `GET /users/{id}/call-preview` for a stalled team user.
3. **DEV trigger** — `DEV=1`, `POST /dev/trigger/{user_id}` with `X-Admin-Key` (still dry-run unless step 4).
4. **One real call** — set `DRY_RUN=0`, ensure the phone is in `TEAM_PHONES` / `team_phones`, trigger once to your own team number, then set `DRY_RUN=1` again.

## Assumptions (label in dashboard)

- `INR_PER_USD = 88`
- `conversion_value_inr = ₹500`, `optout_penalty_inr = ₹200`
