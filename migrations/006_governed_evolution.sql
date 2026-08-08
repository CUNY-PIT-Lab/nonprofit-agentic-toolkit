-- Privacy-bounded product telemetry and human-governed evolution proposals.
-- Raw fieldwork and AuditEvent data do not enter these tables.

CREATE TABLE IF NOT EXISTS product_telemetry_events (
    event_id VARCHAR(120) PRIMARY KEY,
    sequence INTEGER NOT NULL UNIQUE CHECK (sequence >= 1),
    event_type VARCHAR(120) NOT NULL,
    product_area VARCHAR(24) NOT NULL CHECK (
        product_area IN ('pathway', 'prompt', 'interface', 'name')
    ),
    cohort_key VARCHAR(120) NOT NULL,
    metrics JSONB NOT NULL CHECK (jsonb_typeof(metrics) = 'object'),
    dimensions JSONB NOT NULL CHECK (jsonb_typeof(dimensions) = 'object'),
    consent_basis VARCHAR(24) NOT NULL CHECK (
        consent_basis IN ('not_required', 'granted')
    ),
    consent_scope_id VARCHAR(120) NOT NULL DEFAULT '',
    sensitivity VARCHAR(24) NOT NULL CHECK (
        sensitivity IN ('public', 'internal', 'restricted')
    ),
    allowed_purposes JSONB NOT NULL CHECK (
        jsonb_typeof(allowed_purposes) = 'array'
    ),
    deidentified BOOLEAN NOT NULL CHECK (deidentified IS TRUE),
    schema_version VARCHAR(120) NOT NULL,
    app_version VARCHAR(120) NOT NULL,
    policy_version VARCHAR(120) NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    committed_at TIMESTAMPTZ NOT NULL,
    previous_event_hash VARCHAR(64) NOT NULL,
    event_hash VARCHAR(64) NOT NULL UNIQUE,
    CONSTRAINT ck_product_telemetry_chronology CHECK (occurred_at <= committed_at),
    CONSTRAINT ck_product_telemetry_consent_scope CHECK (
        (consent_basis = 'granted' AND consent_scope_id <> '')
        OR (consent_basis = 'not_required' AND consent_scope_id = '')
    ),
    CONSTRAINT ck_product_telemetry_hashes CHECK (
        event_hash ~ '^[0-9a-f]{64}$'
        AND (previous_event_hash = '' OR previous_event_hash ~ '^[0-9a-f]{64}$')
    )
);

CREATE INDEX IF NOT EXISTS ix_product_telemetry_type_time
    ON product_telemetry_events(event_type, occurred_at);
CREATE INDEX IF NOT EXISTS ix_product_telemetry_cohort_time
    ON product_telemetry_events(cohort_key, occurred_at);

CREATE TABLE IF NOT EXISTS product_telemetry_consents (
    decision_id VARCHAR(120) PRIMARY KEY,
    consent_scope_id VARCHAR(120) NOT NULL,
    status VARCHAR(24) NOT NULL CHECK (status IN ('granted', 'withdrawn')),
    actor_id VARCHAR(120) NOT NULL,
    actor_role VARCHAR(80) NOT NULL,
    reason_code VARCHAR(120) NOT NULL,
    supersedes_id VARCHAR(120)
        REFERENCES product_telemetry_consents(decision_id) ON DELETE RESTRICT,
    decided_at TIMESTAMPTZ NOT NULL,
    decision_hash VARCHAR(64) NOT NULL UNIQUE CHECK (
        decision_hash ~ '^[0-9a-f]{64}$'
    )
);

CREATE INDEX IF NOT EXISTS ix_product_consent_scope_time
    ON product_telemetry_consents(consent_scope_id, decided_at);

CREATE TABLE IF NOT EXISTS evolution_proposals (
    proposal_id VARCHAR(120) PRIMARY KEY,
    proposal_checksum VARCHAR(64) NOT NULL UNIQUE CHECK (
        proposal_checksum ~ '^[0-9a-f]{64}$'
    ),
    proposal_type VARCHAR(24) NOT NULL CHECK (
        proposal_type IN ('pathway', 'prompt', 'interface', 'name')
    ),
    component_key VARCHAR(120) NOT NULL,
    evidence_checksum VARCHAR(64) NOT NULL CHECK (
        evidence_checksum ~ '^[0-9a-f]{64}$'
    ),
    document JSONB NOT NULL CHECK (jsonb_typeof(document) = 'object'),
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_evolution_proposal_component
    ON evolution_proposals(component_key, created_at);

CREATE TABLE IF NOT EXISTS evolution_reviews (
    review_id VARCHAR(120) PRIMARY KEY,
    proposal_id VARCHAR(120) NOT NULL UNIQUE
        REFERENCES evolution_proposals(proposal_id) ON DELETE RESTRICT,
    proposal_checksum VARCHAR(64) NOT NULL,
    outcome VARCHAR(24) NOT NULL CHECK (outcome IN ('approved', 'rejected')),
    actor_id VARCHAR(120) NOT NULL,
    actor_role VARCHAR(40) NOT NULL CHECK (
        actor_role IN ('owner', 'reviewer', 'admin', 'maintainer')
    ),
    rationale TEXT NOT NULL,
    decided_at TIMESTAMPTZ NOT NULL,
    review_checksum VARCHAR(64) NOT NULL UNIQUE CHECK (
        review_checksum ~ '^[0-9a-f]{64}$'
    )
);

CREATE INDEX IF NOT EXISTS ix_evolution_review_proposal
    ON evolution_reviews(proposal_id, decided_at);

CREATE TABLE IF NOT EXISTS evolution_rollout_actions (
    action_id VARCHAR(120) PRIMARY KEY,
    proposal_id VARCHAR(120) NOT NULL
        REFERENCES evolution_proposals(proposal_id) ON DELETE RESTRICT,
    proposal_checksum VARCHAR(64) NOT NULL,
    review_checksum VARCHAR(64) NOT NULL,
    action VARCHAR(24) NOT NULL CHECK (action IN ('rollout', 'rollback')),
    target VARCHAR(160) NOT NULL,
    actor_id VARCHAR(120) NOT NULL,
    actor_role VARCHAR(40) NOT NULL CHECK (
        actor_role IN ('owner', 'reviewer', 'admin', 'maintainer')
    ),
    performed_at TIMESTAMPTZ NOT NULL,
    action_checksum VARCHAR(64) NOT NULL UNIQUE CHECK (
        action_checksum ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT uq_evolution_rollout_action UNIQUE (proposal_id, action)
);

CREATE INDEX IF NOT EXISTS ix_evolution_rollout_proposal
    ON evolution_rollout_actions(proposal_id, performed_at);

CREATE TABLE IF NOT EXISTS evolution_evaluations (
    evaluation_id VARCHAR(120) PRIMARY KEY,
    proposal_id VARCHAR(120) NOT NULL
        REFERENCES evolution_proposals(proposal_id) ON DELETE RESTRICT,
    rollout_action_id VARCHAR(120) NOT NULL
        REFERENCES evolution_rollout_actions(action_id) ON DELETE RESTRICT,
    outcome VARCHAR(24) NOT NULL CHECK (
        outcome IN ('met', 'not_met', 'inconclusive')
    ),
    metrics JSONB NOT NULL CHECK (jsonb_typeof(metrics) = 'object'),
    evaluator_id VARCHAR(120) NOT NULL,
    evaluator_role VARCHAR(40) NOT NULL CHECK (
        evaluator_role IN ('owner', 'reviewer', 'admin', 'maintainer')
    ),
    rationale TEXT NOT NULL,
    evidence_projection_checksum VARCHAR(64) NOT NULL CHECK (
        evidence_projection_checksum ~ '^[0-9a-f]{64}$'
    ),
    recorded_at TIMESTAMPTZ NOT NULL,
    evaluation_checksum VARCHAR(64) NOT NULL UNIQUE CHECK (
        evaluation_checksum ~ '^[0-9a-f]{64}$'
    )
);

CREATE INDEX IF NOT EXISTS ix_evolution_evaluation_proposal
    ON evolution_evaluations(proposal_id, recorded_at);

CREATE OR REPLACE FUNCTION evolution_reject_mutation()
RETURNS TRIGGER LANGUAGE plpgsql AS $evolution_mutation$
BEGIN
    RAISE EXCEPTION 'product evolution evidence is append-only: % is forbidden', TG_OP
        USING ERRCODE = '55000';
END;
$evolution_mutation$;

DO $evolution_triggers$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'product_telemetry_events',
        'product_telemetry_consents',
        'evolution_proposals',
        'evolution_reviews',
        'evolution_rollout_actions',
        'evolution_evaluations'
    ] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS evolution_append_only ON %I', table_name);
        EXECUTE format(
            'CREATE TRIGGER evolution_append_only BEFORE UPDATE OR DELETE ON %I '
            'FOR EACH ROW EXECUTE FUNCTION evolution_reject_mutation()',
            table_name
        );
    END LOOP;
END;
$evolution_triggers$;
