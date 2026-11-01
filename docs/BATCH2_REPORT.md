# Rho v2 — Batch 2 report

**Date:** 2026-11-01 (Asia/Calcutta)  
**Commit:** `46ee324` (feature `b7336c6` + report/smoke follow-ups)  
**Artifact:** `/workspace/rho-v2-batch2.zip` (full repo)

## Delivered

1. **Decision engine** (`app/decision.py`)  
   Hard filters in order, each logging a reason code: `stall_resolved`, `no_consent`, `opted_out`, `on_dnd`, `outside_window` (action `call_later` + `next_window_open` in Asia/Kolkata), `user_cap`, `daily_budget`. Then holdout (policy %). Then Thompson sampling (`random.betavariate`) over `none` / `call_now` in `bandit_arms` keyed by `context_key = playbook|stage|hour_bucket`. Score = normalised uplift × `exp(-minutes_since_stall / half_life_minutes)`. Call if `score >= min_score`. Features/score/action/reason logged on every decision. Open stalls claimed with `FOR UPDATE SKIP LOCKED`.

2. **Hint bandit** — Beta per `hint_arms` row; Thompson pick. Compliance wording stays fixed in agent prompts (Khushi / Ruhi), never learned.

3. **Call context** (`app/call_context.py`) — all-string `user_data`. Reactivation: `Customer_Name`, `stage_label`, `goal`, `script_hint`. Referral adds `referrer_first_name`. `Customer_Name` = first token title-cased; empty → `there`; `John Doe` → `John`. Agent id from playbook `agent_id_env_var`. `GET /users/{id}/call-preview` returns `{agent_env, agent_id_configured, playbook, user_data}` with no call.

4. **Bolna client** (`app/bolna_client.py`) — `POST /call` with Bearer key; retries+backoff on 5xx/network only; idempotent `calls` insert (unique `stall_id`) **before** dial; `DRY_RUN=1` → status `dry_run`, log payload, no HTTP; real only if `DRY_RUN=0` **and** phone in `team_phones`. `get_execution` → `GET /executions/{id}`.

5. **Results** — tick polls non-terminal calls; updates status, `termination_reason`, duration, `cost_cents`, `cost_inr` (= cents/100×INR_PER_USD), transcript, summary, extracted_data; `pickup = duration > 0`. `POST /webhooks/bolna` verifies `X-Rho-Webhook-Secret` (constant-time) or `?secret=`; accepts `id` or `execution_id`. Opt-out outcome sets `users.opted_out`.

6. **Rewards** (`app/rewards.py`) — after `reward_window_hours`: reward = conversion_value_inr (if `deposit_completed` in window) − cost_inr − optout_penalty_inr. Updates `call_now` arm + hint arm; holdout/none update `none` arm. Idempotent via `UNIQUE(decision_id, reward_type)`.

7. **Worker** — `run_tick`: stalls → decisions → dial → poll → rewards, each under its own advisory lock. `POST /dev/trigger/{user_id}` when `DEV=1` (admin key): force-evaluate open stall, filters except score threshold.

8. **Bolna agent scripts (separate)**  
   - `agents/khushi_reactivation.md` — **Khushi** (reactivation)  
   - `agents/ruhi_referral.md` — **Ruhi** (referral)

## Smoke tests (`tests/test_batch2_smoke.py`)

| Test | Result |
|------|--------|
| `test_customer_first_name` | PASSED |
| `test_webhook_secret_constant_time` | PASSED |
| `test_dry_run_end_to_end` (team phone + consent → dry_run, payload, no HTTP) | PASSED |
| `test_mocked_bolna_dial_poll_webhook` (respx: dial + poll + webhook opt-out) | PASSED |
| `test_no_double_dial_concurrent_ticks` | PASSED |
| `test_call_preview_keys` | PASSED |
| `test_referral_user_data_has_referrer` | PASSED |

**7/7 passed** (2026-11-01 Asia/Calcutta).

## Schema adds (`migrations/003_batch2.sql`)

- `policies.half_life_minutes`, `policies.reserved_call_cost_inr`
- `calls.termination_reason`, `cost_inr`, `pickup`, `hint_id`, `rewarded_at`
- `decisions.arm_key`, `decisions.reason`
- Seed `hint_arms` from `script_hints`

## Notes / assumptions

- Bandit storage uses existing `(arm_key, context_key)` unique pair (`none`|`call_now` × `playbook|stage|hour_bucket`).
- Decision “reason” is written to both `filter_reason` and `reason` columns.
- Real dials still require `DRY_RUN=0` **and** `team_phones` membership.
- Dashboard remains Batch 3. Full unit/integration suite also Batch 3; Batch 2 ships smoke only.
