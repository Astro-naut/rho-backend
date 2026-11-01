-- Batch 2: policy half-life / reserved cost; call result fields; decision arm_key; seed hint_arms.

ALTER TABLE policies
    ADD COLUMN IF NOT EXISTS half_life_minutes DOUBLE PRECISION NOT NULL DEFAULT 120,
    ADD COLUMN IF NOT EXISTS reserved_call_cost_inr DOUBLE PRECISION NOT NULL DEFAULT 15;

ALTER TABLE calls
    ADD COLUMN IF NOT EXISTS termination_reason TEXT,
    ADD COLUMN IF NOT EXISTS cost_inr DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS pickup BOOLEAN,
    ADD COLUMN IF NOT EXISTS hint_id BIGINT REFERENCES script_hints(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS rewarded_at TIMESTAMPTZ;

ALTER TABLE decisions
    ADD COLUMN IF NOT EXISTS arm_key TEXT,
    ADD COLUMN IF NOT EXISTS reason TEXT;

-- Backfill reason from filter_reason where missing
UPDATE decisions SET reason = filter_reason WHERE reason IS NULL AND filter_reason IS NOT NULL;

-- Seed hint_arms for every script_hint (idempotent).
INSERT INTO hint_arms (hint_id, playbook_key, stage_key, alpha, beta, pulls)
SELECT h.id, h.playbook_key, h.stage_key, 1, 1, 0
  FROM script_hints h
ON CONFLICT (hint_id) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_calls_status_updated
    ON calls (status, updated_at)
    WHERE execution_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_calls_non_terminal
    ON calls (updated_at)
    WHERE execution_id IS NOT NULL
      AND lower(status) NOT IN (
          'completed','failed','busy','no-answer','no_answer',
          'canceled','cancelled','error','stopped','dry_run'
      );
