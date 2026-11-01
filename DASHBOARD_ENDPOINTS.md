# Dashboard endpoints

The UI at `dashboard/index.html` (also served at `GET /dashboard`) uses a single
config value `API_BASE` (default `https://rho-backend.onrender.com`). Override
with `?api=` (stored in `localStorage` key `rho_api_base`).

Threshold save uses `X-Admin-Key` from the Admin key field
(`localStorage` key `rho_admin_key`).

| Method | Path | Auth | Query / body | Used for |
|--------|------|------|--------------|----------|
| GET | `/dashboard/overview` | public | `?range=today\|7\|14` (legacy `?days=` also accepted) | Hero KPIs, conversion flow, stuck-by-stage, why-not-converted, calls-we-chose-not-to-make, cost & ROI (INR) |
| GET | `/dashboard/learning` | public | `?range=today\|7\|14` | Learning vs call-everyone chart |
| GET | `/dashboard/threshold` | public | — | Current `min_score` (reactivation primary + by_playbook) |
| PUT | `/dashboard/threshold` | `X-Admin-Key` | `{ "threshold": 0.35 }` | Save threshold to all policies |
| POST | `/dashboard/simulate` | public | `{ "threshold": 0.35 }` | What-if: calls, projected_cost_inr, expected_reactivations |
| GET | `/dashboard/decisions` | public | `?limit=&offset=` | Paged decision log (When, User masked, Action, Score, Reason) |
| GET | `/dashboard` / `/dashboard/` | public | — | Serves `dashboard/index.html` |

## Response shapes (overview)

- `hero`: users_stalled, calls_made, converted, lift_pp, spend_inr, cost_per_converted_inr (+ deltas)
- `flow`: stalled / called / messaged / skipped / picked_up / converted (+ pcts)
- `stages[]`: stage, label, stalled, called, converted, conversion_pct, cost_inr
- `reasons[]`: code, label, count, pct, insight (from `calls.extracted_data.reason_code`)
- `outcomes[]`: code, label, count, pct
- `drops[]`: label, count, pct (reason-code counts for non-call decisions)
- `cost`: spend_inr, cost_per_call_inr, cost_per_converted_inr, projected_monthly_inr, budget_today_inr, spend_today_inr, budget_used_pct, inr_per_usd, conversion_value_inr_assumption, optout_penalty_inr_assumption, assumptions_note

## Notes

- Reads are aggregated / masked — safe without an admin key.
- Conversion = later onboarding step or deposit within 48h of the call.
- INR uses `INR_PER_USD` (default 88). Conversion/opt-out ₹ values are ASSUMPTIONS.
