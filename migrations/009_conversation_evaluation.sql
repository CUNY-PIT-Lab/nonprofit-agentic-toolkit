-- Reviewer-private, append-only evaluation of canonical guided-stage turns.
-- This ledger does not write pathway, stage, fieldwork, sidecar, or telemetry data.

CREATE TABLE IF NOT EXISTS evaluation_buckets (
    bucket_id VARCHAR(120) PRIMARY KEY,
    reviewer_id VARCHAR(36) NOT NULL
        REFERENCES users(id) ON DELETE CASCADE,
    operation_id VARCHAR(120) NOT NULL,
    label VARCHAR(40) NOT NULL CHECK (length(label) BETWEEN 1 AND 40),
    color_key VARCHAR(24) NOT NULL CHECK (
        color_key IN ('blue', 'green', 'violet', 'red')
    ),
    definition_hash VARCHAR(64) NOT NULL UNIQUE CHECK (
        definition_hash ~ '^[0-9a-f]{64}$'
    ),
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_evaluation_bucket_operation
        UNIQUE (reviewer_id, operation_id),
    CONSTRAINT ck_evaluation_bucket_operation CHECK (
        operation_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{7,119}$'
    )
);

CREATE INDEX IF NOT EXISTS ix_evaluation_buckets_reviewer
    ON evaluation_buckets(reviewer_id, created_at);

CREATE TABLE IF NOT EXISTS conversation_evaluation_events (
    event_id VARCHAR(120) PRIMARY KEY,
    stage_state_id VARCHAR(36) NOT NULL
        REFERENCES stage_states(id) ON DELETE CASCADE,
    organization_id VARCHAR(36) NOT NULL
        REFERENCES organizations(id) ON DELETE CASCADE,
    record_id VARCHAR(36) NOT NULL
        REFERENCES adoption_records(id) ON DELETE CASCADE,
    stage VARCHAR(40) NOT NULL,
    cycle_number INTEGER NOT NULL CHECK (cycle_number >= 1),
    evaluation_version INTEGER NOT NULL CHECK (evaluation_version >= 1),
    operation_id VARCHAR(120) NOT NULL CHECK (
        operation_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{7,119}$'
    ),
    operation_fingerprint VARCHAR(64) NOT NULL CHECK (
        operation_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    reviewer_id VARCHAR(36) NOT NULL
        REFERENCES users(id) ON DELETE RESTRICT,
    event_type VARCHAR(32) NOT NULL CHECK (
        event_type IN (
            'placement_set', 'note_set', 'annotation_set', 'annotation_removed'
        )
    ),
    transcript_checksum VARCHAR(64) NOT NULL CHECK (
        transcript_checksum ~ '^[0-9a-f]{64}$'
    ),
    bucket_id VARCHAR(120),
    note TEXT,
    -- The immutable identifier is validated at insert time below. It is not a
    -- cascading FK: deleting one canonical turn must never punch a hole in an
    -- otherwise valid evaluation hash chain.
    turn_id VARCHAR(36),
    annotation_category VARCHAR(24) CHECK (
        annotation_category IS NULL OR annotation_category IN (
            'helpful', 'unclear', 'incorrect', 'unsafe', 'other'
        )
    ),
    previous_event_hash VARCHAR(64) NOT NULL CHECK (
        previous_event_hash = ''
        OR previous_event_hash ~ '^[0-9a-f]{64}$'
    ),
    event_hash VARCHAR(64) NOT NULL UNIQUE CHECK (
        event_hash ~ '^[0-9a-f]{64}$'
    ),
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_conversation_evaluation_version
        UNIQUE (stage_state_id, reviewer_id, evaluation_version),
    CONSTRAINT uq_conversation_evaluation_operation
        UNIQUE (stage_state_id, reviewer_id, operation_id),
    CONSTRAINT ck_conversation_evaluation_shape CHECK (
        (
            event_type = 'placement_set'
            AND note IS NULL
            AND turn_id IS NULL
            AND annotation_category IS NULL
        ) OR (
            event_type = 'note_set'
            AND bucket_id IS NULL
            AND turn_id IS NULL
            AND annotation_category IS NULL
            AND (note IS NULL OR length(note) <= 1000)
        ) OR (
            event_type = 'annotation_set'
            AND bucket_id IS NULL
            AND turn_id IS NOT NULL
            AND annotation_category IS NOT NULL
            AND (note IS NULL OR length(note) <= 500)
        ) OR (
            event_type = 'annotation_removed'
            AND bucket_id IS NULL
            AND note IS NULL
            AND turn_id IS NOT NULL
            AND annotation_category IS NULL
        )
    )
);

CREATE INDEX IF NOT EXISTS ix_conversation_evaluation_stage_version
    ON conversation_evaluation_events(
        stage_state_id, reviewer_id, evaluation_version
    );
CREATE INDEX IF NOT EXISTS ix_conversation_evaluation_org_time
    ON conversation_evaluation_events(organization_id, created_at);

CREATE OR REPLACE FUNCTION evaluation_validate_event_identity()
RETURNS TRIGGER LANGUAGE plpgsql AS $evaluation_identity$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM stage_states AS state
        JOIN adoption_records AS record_row ON record_row.id = state.record_id
        WHERE state.id = NEW.stage_state_id
          AND state.record_id = NEW.record_id
          AND state.stage = NEW.stage
          AND state.cycle_number = NEW.cycle_number
          AND record_row.organization_id = NEW.organization_id
    ) THEN
        RAISE EXCEPTION 'evaluation event does not match its canonical stage state'
            USING ERRCODE = '23503';
    END IF;
    IF NEW.turn_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM conversation_turns AS turn_row
        WHERE turn_row.id = NEW.turn_id
          AND turn_row.record_id = NEW.record_id
          AND turn_row.stage = NEW.stage
          AND turn_row.cycle_number = NEW.cycle_number
    ) THEN
        RAISE EXCEPTION 'evaluation annotation turn is not canonical to this stage state'
            USING ERRCODE = '23503';
    END IF;
    RETURN NEW;
END;
$evaluation_identity$;

DROP TRIGGER IF EXISTS evaluation_event_identity_guard
    ON conversation_evaluation_events;
CREATE TRIGGER evaluation_event_identity_guard
BEFORE INSERT ON conversation_evaluation_events
FOR EACH ROW EXECUTE FUNCTION evaluation_validate_event_identity();

CREATE OR REPLACE FUNCTION evaluation_reject_mutation()
RETURNS TRIGGER LANGUAGE plpgsql AS $evaluation_mutation$
BEGIN
    -- Foreign-key cascades may remove the derived ledger with its canonical
    -- parent. Direct edits and direct deletes remain forbidden.
    IF TG_OP = 'DELETE' AND pg_trigger_depth() > 1 THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'conversation evaluation is append-only: % is forbidden', TG_OP
        USING ERRCODE = '55000';
END;
$evaluation_mutation$;

DROP TRIGGER IF EXISTS evaluation_append_only ON evaluation_buckets;
CREATE TRIGGER evaluation_append_only
BEFORE UPDATE OR DELETE ON evaluation_buckets
FOR EACH ROW EXECUTE FUNCTION evaluation_reject_mutation();

DROP TRIGGER IF EXISTS evaluation_append_only ON conversation_evaluation_events;
CREATE TRIGGER evaluation_append_only
BEFORE UPDATE OR DELETE ON conversation_evaluation_events
FOR EACH ROW EXECUTE FUNCTION evaluation_reject_mutation();
