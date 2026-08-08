-- Make guided review state replayable across explicit pathway cycles.
-- Existing rows become cycle 1; all prior evidence stays in place.

ALTER TABLE conversation_turns
    ADD COLUMN IF NOT EXISTS cycle_number INTEGER NOT NULL DEFAULT 1;
ALTER TABLE stage_states
    ADD COLUMN IF NOT EXISTS cycle_number INTEGER NOT NULL DEFAULT 1;
ALTER TABLE completed_steps
    ADD COLUMN IF NOT EXISTS cycle_number INTEGER NOT NULL DEFAULT 1;

ALTER TABLE conversation_turns DROP CONSTRAINT IF EXISTS uq_turn_ordinal;
ALTER TABLE conversation_turns DROP CONSTRAINT IF EXISTS uq_turn_idempotency;
ALTER TABLE stage_states DROP CONSTRAINT IF EXISTS uq_stage_state;
ALTER TABLE completed_steps DROP CONSTRAINT IF EXISTS uq_completed_stage;

ALTER TABLE conversation_turns
    ADD CONSTRAINT uq_turn_ordinal
    UNIQUE (record_id, stage, cycle_number, ordinal);
ALTER TABLE conversation_turns
    ADD CONSTRAINT uq_turn_idempotency
    UNIQUE (record_id, stage, cycle_number, idempotency_key);
ALTER TABLE stage_states
    ADD CONSTRAINT uq_stage_state
    UNIQUE (record_id, stage, cycle_number);
ALTER TABLE completed_steps
    ADD CONSTRAINT uq_completed_stage
    UNIQUE (record_id, stage, cycle_number);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_turn_cycle_number'
          AND conrelid = 'conversation_turns'::regclass
    ) THEN
        ALTER TABLE conversation_turns
            ADD CONSTRAINT ck_turn_cycle_number CHECK (cycle_number >= 1);
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_stage_state_cycle_number'
          AND conrelid = 'stage_states'::regclass
    ) THEN
        ALTER TABLE stage_states
            ADD CONSTRAINT ck_stage_state_cycle_number CHECK (cycle_number >= 1);
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_completed_cycle_number'
          AND conrelid = 'completed_steps'::regclass
    ) THEN
        ALTER TABLE completed_steps
            ADD CONSTRAINT ck_completed_cycle_number CHECK (cycle_number >= 1);
    END IF;
END
$$;

DROP INDEX IF EXISTS uq_turn_idempotency_idx;
DROP INDEX IF EXISTS ix_turns_record_stage;
CREATE INDEX ix_turns_record_stage
    ON conversation_turns (record_id, stage, cycle_number, ordinal);

DROP INDEX IF EXISTS ix_stage_states_record;
CREATE INDEX ix_stage_states_record
    ON stage_states (record_id, stage, cycle_number);

DROP INDEX IF EXISTS ix_completed_record;
CREATE INDEX ix_completed_record
    ON completed_steps (record_id, cycle_number, completed_at);
