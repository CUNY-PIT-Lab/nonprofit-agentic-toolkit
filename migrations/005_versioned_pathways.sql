-- Immutable cross-stage pathway definitions, confirmed facts, approvals, and decisions.

CREATE TABLE IF NOT EXISTS pathway_versions (
    definition_checksum VARCHAR(64) PRIMARY KEY,
    family_key VARCHAR(80) NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    definition JSONB NOT NULL CHECK (jsonb_typeof(definition) = 'object'),
    status VARCHAR(20) NOT NULL CHECK (status IN ('draft', 'approved', 'retired')),
    created_by VARCHAR(120) NOT NULL,
    approved_by VARCHAR(120) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    approved_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_pathway_family_version UNIQUE (family_key, version),
    CONSTRAINT ck_pathway_definition_hash CHECK (
        definition_checksum ~ '^[0-9a-f]{64}$'
    )
);

CREATE TABLE IF NOT EXISTS pathway_runs (
    record_id VARCHAR(36) PRIMARY KEY
        REFERENCES adoption_records(id) ON DELETE CASCADE,
    definition_checksum VARCHAR(64) NOT NULL
        REFERENCES pathway_versions(definition_checksum) ON DELETE RESTRICT,
    family_key VARCHAR(80) NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    current_node VARCHAR(80) NOT NULL,
    entry_role VARCHAR(40) NOT NULL,
    status VARCHAR(24) NOT NULL CHECK (
        status IN ('active', 'paused', 'complete', 'walked_away', 'non_ai', 'retired')
    ),
    cycle_number INTEGER NOT NULL CHECK (cycle_number >= 1),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_pathway_runs_definition
    ON pathway_runs(definition_checksum, status);

CREATE TABLE IF NOT EXISTS pathway_facts (
    id VARCHAR(36) PRIMARY KEY,
    record_id VARCHAR(36) NOT NULL
        REFERENCES adoption_records(id) ON DELETE CASCADE,
    fact_key VARCHAR(120) NOT NULL,
    value JSONB NOT NULL,
    status VARCHAR(20) NOT NULL CHECK (status IN ('proposed', 'confirmed', 'rejected')),
    source_event_ids JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(source_event_ids) = 'array'),
    proposed_by VARCHAR(120) NOT NULL,
    confirmed_by VARCHAR(120) NOT NULL DEFAULT '',
    supersedes_id VARCHAR(36) REFERENCES pathway_facts(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_pathway_facts_record_key
    ON pathway_facts(record_id, fact_key, created_at);

CREATE TABLE IF NOT EXISTS pathway_approvals (
    id VARCHAR(36) PRIMARY KEY,
    record_id VARCHAR(36) NOT NULL
        REFERENCES adoption_records(id) ON DELETE CASCADE,
    gate_key VARCHAR(120) NOT NULL,
    status VARCHAR(24) NOT NULL CHECK (
        status IN ('approved', 'rejected', 'changes_requested')
    ),
    actor_id VARCHAR(120) NOT NULL,
    subject_checksum VARCHAR(64) NOT NULL CHECK (
        subject_checksum ~ '^[0-9a-f]{64}$'
    ),
    rationale TEXT NOT NULL DEFAULT '',
    supersedes_id VARCHAR(36) REFERENCES pathway_approvals(id) ON DELETE RESTRICT,
    decided_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_pathway_approvals_record_gate
    ON pathway_approvals(record_id, gate_key, created_at);

CREATE TABLE IF NOT EXISTS pathway_transitions (
    id VARCHAR(36) PRIMARY KEY,
    record_id VARCHAR(36) NOT NULL
        REFERENCES adoption_records(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    edge_id VARCHAR(80) NOT NULL,
    from_node VARCHAR(80) NOT NULL,
    to_node VARCHAR(80) NOT NULL,
    outcome VARCHAR(32) NOT NULL,
    actor_id VARCHAR(120) NOT NULL,
    rationale TEXT NOT NULL,
    evidence JSONB NOT NULL CHECK (jsonb_typeof(evidence) = 'object'),
    evidence_checksum VARCHAR(64) NOT NULL,
    pathway_checksum VARCHAR(64) NOT NULL,
    previous_decision_hash VARCHAR(64) NOT NULL,
    decision_hash VARCHAR(64) NOT NULL UNIQUE,
    idempotency_key VARCHAR(120),
    decided_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_pathway_transition_sequence UNIQUE (record_id, sequence),
    CONSTRAINT uq_pathway_transition_idempotency UNIQUE (record_id, idempotency_key),
    CONSTRAINT ck_pathway_transition_hashes CHECK (
        evidence_checksum ~ '^[0-9a-f]{64}$'
        AND pathway_checksum ~ '^[0-9a-f]{64}$'
        AND decision_hash ~ '^[0-9a-f]{64}$'
        AND (previous_decision_hash = '' OR previous_decision_hash ~ '^[0-9a-f]{64}$')
    )
);

CREATE INDEX IF NOT EXISTS ix_pathway_transitions_record_time
    ON pathway_transitions(record_id, decided_at);

CREATE OR REPLACE FUNCTION pathway_reject_mutation()
RETURNS TRIGGER LANGUAGE plpgsql AS $pathway_mutation$
BEGIN
    RAISE EXCEPTION 'pathway evidence is append-only: % is forbidden', TG_OP
        USING ERRCODE = '55000';
END;
$pathway_mutation$;

DO $pathway_triggers$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'pathway_versions', 'pathway_facts',
        'pathway_approvals', 'pathway_transitions'
    ] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS pathway_append_only ON %I', table_name);
        EXECUTE format(
            'CREATE TRIGGER pathway_append_only BEFORE UPDATE OR DELETE ON %I '
            'FOR EACH ROW EXECUTE FUNCTION pathway_reject_mutation()',
            table_name
        );
    END LOOP;
END;
$pathway_triggers$;
