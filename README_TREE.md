# Rho v2 — repository tree

Local git repo (intended remote name: `rho-backend`). Batch 1+2 complete.

```
rho-v2/
├── agents/
│   ├── khushi_reactivation.md   # Bolna script: Khushi (reactivation)
│   └── ruhi_referral.md         # Bolna script: Ruhi (referral)
├── app/
│   ├── admin_routes.py
│   ├── auth.py
│   ├── bolna_client.py          # Batch 2: dial, poll, dry-run gate
│   ├── call_context.py          # Batch 2: user_data + call-preview
│   ├── config.py
│   ├── db.py
│   ├── decision.py              # Batch 2: filters, holdout, Thompson
│   ├── dnd.py
│   ├── ingest.py
│   ├── main.py                  # + webhook, call-preview, /dev/trigger
│   ├── migrate.py
│   ├── models.py
│   ├── phones.py
│   ├── rate_limit.py
│   ├── rewards.py               # Batch 2: bandit/hint rewards
│   ├── stalls.py
│   ├── team_phones.py
│   ├── webhooks.py              # Batch 2: Bolna webhook verify/handle
│   └── worker.py                # Batch 2: per-step advisory locks
├── migrations/
│   ├── 001_init.sql
│   ├── 002_seed.sql
│   └── 003_batch2.sql
├── tests/
│   └── test_batch2_smoke.py
├── .env.example
├── BATCH1_REPORT.md
├── BATCH2_REPORT.md
├── pytest.ini
├── requirements.txt
└── README_TREE.md
```
