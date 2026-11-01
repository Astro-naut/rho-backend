-- Rho v2 full schema (all batches). Idempotent via IF NOT EXISTS / constraints.

CREATE TABLE IF NOT EXISTS schema_migrations (
    filename TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id                TEXT PRIMARY KEY,
    name              TEXT,
    phone             TEXT,
    language          TEXT NOT NULL DEFAULT 'hinglish',
    consent           BOOLEAN NOT NULL DEFAULT false,
    dnd               BOOLEAN NOT NULL DEFAULT false,
    opted_out         BOOLEAN NOT NULL DEFAULT false,
    ref_code_used     TEXT,
    ref_status        TEXT,  -- resolved | ambiguous | unknown | NULL
    referral_code     TEXT UNIQUE,
    referrer_user_id  TEXT REFERENCES users(id) ON DELETE SET NULL,
    current_stage     TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_event_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_users_phone ON users (phone);
CREATE INDEX IF NOT EXISTS idx_users_referral_code ON users (referral_code);
CREATE INDEX IF NOT EXISTS idx_users_referrer ON users (referrer_user_id);

CREATE TABLE IF NOT EXISTS events (
    id          BIGSERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    stage       TEXT NOT NULL,
    event       TEXT NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,
    ref_code    TEXT,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_events_user_ts ON events (user_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_stage ON events (stage, event);

CREATE TABLE IF NOT EXISTS user_stage_state (
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    stage_key     TEXT NOT NULL,
    entered_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at  TIMESTAMPTZ,
    PRIMARY KEY (user_id, stage_key)
);

CREATE TABLE IF NOT EXISTS playbooks (
    key               TEXT PRIMARY KEY,
    label             TEXT NOT NULL,
    agent_id_env_var  TEXT NOT NULL,
    active            BOOLEAN NOT NULL DEFAULT true,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS playbook_stages (
    playbook_key       TEXT NOT NULL REFERENCES playbooks(key) ON DELETE CASCADE,
    stage_key          TEXT NOT NULL,
    ordinal            INT NOT NULL,
    label              TEXT NOT NULL,
    goal               TEXT NOT NULL,
    threshold_minutes  INT NOT NULL CHECK (threshold_minutes > 0),
    PRIMARY KEY (playbook_key, stage_key),
    UNIQUE (playbook_key, ordinal)
);

CREATE TABLE IF NOT EXISTS script_hints (
    id            BIGSERIAL PRIMARY KEY,
    playbook_key  TEXT NOT NULL,
    stage_key     TEXT NOT NULL,
    hint_index    INT NOT NULL CHECK (hint_index BETWEEN 1 AND 3),
    text          TEXT NOT NULL,
    UNIQUE (playbook_key, stage_key, hint_index),
    FOREIGN KEY (playbook_key, stage_key)
        REFERENCES playbook_stages(playbook_key, stage_key) ON DELETE CASCADE
);

-- Per-playbook policy. conversion_value_inr / optout_penalty_inr are ASSUMPTIONS
-- (demo defaults 500 / 200) and are editable via the admin API.
CREATE TABLE IF NOT EXISTS policies (
    playbook_key           TEXT PRIMARY KEY REFERENCES playbooks(key) ON DELETE CASCADE,
    min_score              DOUBLE PRECISION NOT NULL DEFAULT 0.35,
    daily_budget_inr       DOUBLE PRECISION NOT NULL DEFAULT 500,
    call_window_start      TIME NOT NULL DEFAULT '10:00',
    call_window_end        TIME NOT NULL DEFAULT '19:00',
    timezone               TEXT NOT NULL DEFAULT 'Asia/Kolkata',
    per_user_cap           INT NOT NULL DEFAULT 2,
    per_user_cap_days      INT NOT NULL DEFAULT 7,
    holdout_pct            DOUBLE PRECISION NOT NULL DEFAULT 0.10,
    conversion_value_inr   DOUBLE PRECISION NOT NULL DEFAULT 500,  -- ASSUMPTION
    optout_penalty_inr     DOUBLE PRECISION NOT NULL DEFAULT 200,  -- ASSUMPTION
    reward_window_hours    INT NOT NULL DEFAULT 48,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS stalls (
    id            BIGSERIAL PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    stage_key     TEXT NOT NULL,
    playbook_key  TEXT NOT NULL REFERENCES playbooks(key),
    status        TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    opened_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at     TIMESTAMPTZ,
    close_reason  TEXT,
    CONSTRAINT stalls_closed_consistency CHECK (
        (status = 'open' AND closed_at IS NULL)
        OR (status = 'closed' AND closed_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_stalls_one_open_per_user
    ON stalls (user_id) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_stalls_open_opened
    ON stalls (opened_at) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_stalls_user_stage ON stalls (user_id, stage_key);

CREATE TABLE IF NOT EXISTS decisions (
    id            BIGSERIAL PRIMARY KEY,
    stall_id      BIGINT NOT NULL REFERENCES stalls(id) ON DELETE CASCADE,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    playbook_key  TEXT NOT NULL,
    stage_key     TEXT NOT NULL,
    action        TEXT NOT NULL,  -- none | call_now | call_later | holdout | filtered
    filter_reason TEXT,
    score         DOUBLE PRECISION,
    holdout       BOOLEAN NOT NULL DEFAULT false,
    hint_id       BIGINT REFERENCES script_hints(id),
    context       JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_decisions_stall ON decisions (stall_id, created_at DESC);

CREATE TABLE IF NOT EXISTS calls (
    id                     BIGSERIAL PRIMARY KEY,
    stall_id               BIGINT NOT NULL UNIQUE REFERENCES stalls(id) ON DELETE CASCADE,
    decision_id            BIGINT REFERENCES decisions(id) ON DELETE SET NULL,
    user_id                TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    playbook_key           TEXT NOT NULL,
    stage_key              TEXT NOT NULL,
    phone                  TEXT NOT NULL,
    agent_id               TEXT,
    execution_id           TEXT,
    payload_sent           JSONB NOT NULL DEFAULT '{}'::jsonb,
    status                 TEXT NOT NULL DEFAULT 'pending',
    cost_cents             INT,
    conversation_duration  INT,
    transcript             TEXT,
    summary                TEXT,
    extracted_data         JSONB,
    dry_run                BOOLEAN NOT NULL DEFAULT true,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_calls_execution ON calls (execution_id);
CREATE INDEX IF NOT EXISTS idx_calls_user_created ON calls (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS bandit_arms (
    id            BIGSERIAL PRIMARY KEY,
    arm_key       TEXT NOT NULL,          -- none | call_now
    context_key   TEXT NOT NULL,          -- case|stage|hour_bucket
    alpha         DOUBLE PRECISION NOT NULL DEFAULT 1,
    beta          DOUBLE PRECISION NOT NULL DEFAULT 1,
    pulls         INT NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (arm_key, context_key)
);

CREATE TABLE IF NOT EXISTS hint_arms (
    id            BIGSERIAL PRIMARY KEY,
    hint_id       BIGINT NOT NULL REFERENCES script_hints(id) ON DELETE CASCADE,
    playbook_key  TEXT NOT NULL,
    stage_key     TEXT NOT NULL,
    alpha         DOUBLE PRECISION NOT NULL DEFAULT 1,
    beta          DOUBLE PRECISION NOT NULL DEFAULT 1,
    pulls         INT NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (hint_id)
);

CREATE TABLE IF NOT EXISTS rewards (
    id            BIGSERIAL PRIMARY KEY,
    decision_id   BIGINT NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
    call_id       BIGINT REFERENCES calls(id) ON DELETE SET NULL,
    stall_id      BIGINT NOT NULL REFERENCES stalls(id) ON DELETE CASCADE,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reward_type   TEXT NOT NULL,  -- conversion | optout | none
    value_inr     DOUBLE PRECISION NOT NULL DEFAULT 0,
    observed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    window_ends_at TIMESTAMPTZ,
    finalized     BOOLEAN NOT NULL DEFAULT false,
    UNIQUE (decision_id, reward_type)
);

CREATE TABLE IF NOT EXISTS referral_shares (
    id            BIGSERIAL PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code          TEXT NOT NULL,
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_referral_shares_user ON referral_shares (user_id, ts DESC);

-- Call allowlist. Real calls (later) need DRY_RUN=0 AND phone present here.
CREATE TABLE IF NOT EXISTS team_phones (
    phone       TEXT PRIMARY KEY,  -- E.164
    label       TEXT,
    source      TEXT NOT NULL DEFAULT 'admin',  -- env_seed | admin
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- DND numbers (our table only — no external list). Also mirrored by users.dnd.
CREATE TABLE IF NOT EXISTS dnd_numbers (
    phone       TEXT PRIMARY KEY,  -- E.164
    source      TEXT NOT NULL DEFAULT 'admin',  -- admin | import | user_flag
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id          BIGSERIAL PRIMARY KEY,
    who         TEXT NOT NULL DEFAULT 'admin',
    action      TEXT NOT NULL,
    entity      TEXT,
    entity_id   TEXT,
    before      JSONB,
    after       JSONB,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_admin_audit_ts ON admin_audit_log (ts DESC);
