# Rho v2 Batch 1 — report

**Repo:** `/workspace/rho-v2` (local git `main` @ f0c296e, no push)  
**Zip:** `/workspace/rho-v2-batch1.zip`  
**Timezone note:** verification times below are Asia/Calcutta (UTC+5:30) wall clock derived from box UTC logs.

## Planned tree (full) + Batch 1 implemented

See `README_TREE.md`. Implemented now: config, db, migrations (full schema), ingest, stalls, worker tick, admin policy/playbooks, team_phones, dnd.

Deferred: decision/Bolna (Batch 2), **dashboard (Batch 3)**.

## Endpoints (Batch 1)

| Method | Path | Auth |
|--------|------|------|
| GET | `/health` | none (no DB) |
| GET/POST | `/internal/tick?secret=` | `TICK_SECRET` (const-time) |
| POST | `/events` | rate limit + 4KB body |
| POST | `/users/{id}/profile` | rate limit + 4KB body |
| GET | `/users/{id}/referral-code` | none (404 if no profile/code) |
| POST | `/users/{id}/optout` | rate limit |
| PUT | `/users/{id}/dnd` | `X-Admin-Key` |
| GET/PUT | `/policy`, `/policy/{playbook}` | admin |
| GET/PUT | `/playbooks`, `/playbooks/{key}` | admin |
| GET/POST/DELETE | `/admin/team-phones`[+`/{phone}`] | admin |
| POST | `/admin/dnd/import` | admin (CSV body or upload) |
| OPTIONS | `/{path}` | CORS preflight |

## Clarifications applied

1. `team_phones` table seeded from `TEAM_PHONES` (+ deprecated `TEST_PHONE_ALLOWLIST` → same table). Admin CRUD + `admin_audit_log`. Real calls later: `DRY_RUN=0` AND phone in table.
2. `dnd_numbers` + `users.dnd`; `dnd_check(phone)` in `app/dnd.py` reads only our table. Admin PUT dnd + CSV import.
3. Referral: server `SPR-*` codes; legacy `SPROUT-NAME` only if exactly one user; else `ambiguous`/`unknown`, no referrer → reactivation. Only referrer first name exposed.
4. `conversion_value_inr` / `optout_penalty_inr` per-playbook in `policies`, editable via admin API; seed comments mark ASSUMPTION (500/200).
5. Dashboard explicitly Batch 3 — not in this batch.

## Verified

- Deps installed in `.venv` (Python 3.13 on box; target 3.11 compatible).
- Local Postgres via `pgembed` (apt postgres server unavailable); `DATABASE_URL=postgresql://postgres@/rho?host=/workspace/rho-v2/.pgdata`.
- Migrations applied twice: first applied `001`+`002`, second “no pending”.
- App on `:8010`: health OK; profile issues `SPR-*`; events complete→next stage; referral shared; invitee resolves referrer first name; ambiguous legacy → reactivation; policy GET/PUT; playbooks 6 stages / 18 hints; team phone CRUD + audit; DND flag + CSV import; optout; threshold override → tick opens stall (reactivation & referral playbooks); second tick idempotent (0 opens); completion closes stall; future ts → 400; bad tick secret → 401; CORS preflight allows `https://astro-naut.github.io`.

## Deviations

- Host Python is 3.13 (spec 3.11); code is 3.11-compatible; no `pyproject.toml` (requirements.txt only).
- Local DB via pgembed sockets, not Neon (no Neon URL provided); same `DATABASE_URL` env switch works for Neon/Render.
- Port 8000 busy → verified on 8010.
- `OPTIONS` preflight returns 200 via CORSMiddleware (also have 204 catch-all).
- Full README with Render/Neon/tunnel commands deferred to later batch (tree notes it).
