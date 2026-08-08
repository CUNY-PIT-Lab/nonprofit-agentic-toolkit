-- Durable, replayable ethnographic fieldwork core (PostgreSQL).
-- All domain tables are append-only; consent withdrawal is a new event, never a rewrite.

CREATE TABLE IF NOT EXISTS fieldwork_projects (
    project_id VARCHAR(120) PRIMARY KEY,
    title VARCHAR(240) NOT NULL,
    canonical_branch_id VARCHAR(160) NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS fieldwork_cycles (
    project_id VARCHAR(120) NOT NULL REFERENCES fieldwork_projects(project_id) ON DELETE RESTRICT,
    cycle_id VARCHAR(120) NOT NULL,
    label VARCHAR(240) NOT NULL,
    opened_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (project_id, cycle_id)
);

CREATE TABLE IF NOT EXISTS fieldwork_branches (
    branch_id VARCHAR(160) PRIMARY KEY,
    project_id VARCHAR(120) NOT NULL REFERENCES fieldwork_projects(project_id) ON DELETE RESTRICT,
    cycle_id VARCHAR(120),
    mode VARCHAR(24) NOT NULL CHECK (mode IN ('canonical', 'historical', 'counterfactual')),
    parent_branch_id VARCHAR(160) REFERENCES fieldwork_branches(branch_id) ON DELETE RESTRICT,
    base_event_id VARCHAR(160),
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT fk_fieldwork_branch_cycle FOREIGN KEY (project_id, cycle_id)
        REFERENCES fieldwork_cycles(project_id, cycle_id) ON DELETE RESTRICT,
    CONSTRAINT ck_fieldwork_branch_shape CHECK (
        (mode = 'canonical' AND parent_branch_id IS NULL AND base_event_id IS NULL)
        OR
        (mode <> 'canonical' AND parent_branch_id IS NOT NULL
            AND base_event_id IS NOT NULL AND cycle_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS ix_fieldwork_branches_project_cycle
    ON fieldwork_branches(project_id, cycle_id);

CREATE TABLE IF NOT EXISTS fieldwork_events (
    event_id VARCHAR(160) PRIMARY KEY,
    project_id VARCHAR(120) NOT NULL REFERENCES fieldwork_projects(project_id) ON DELETE RESTRICT,
    project_position INTEGER NOT NULL CHECK (project_position >= 1),
    cycle_id VARCHAR(120),
    branch_id VARCHAR(160) NOT NULL REFERENCES fieldwork_branches(branch_id) ON DELETE RESTRICT,
    branch_sequence INTEGER NOT NULL CHECK (branch_sequence >= 1),
    kind VARCHAR(64) NOT NULL,
    epistemic_layer VARCHAR(40) NOT NULL CHECK (epistemic_layer IN (
        'observation', 'participant_account', 'researcher_record', 'reflexive_memo',
        'positionality', 'member_check', 'interpretation', 'synthesis', 'decision',
        'intervention', 'after_effect', 'counterfactual'
    )),
    actor_id VARCHAR(160) NOT NULL,
    actor_role VARCHAR(80) NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    committed_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    causal_event_ids JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(causal_event_ids) = 'array'),
    source_refs JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(source_refs) = 'array'),
    manifest JSONB NOT NULL CHECK (
        jsonb_typeof(manifest) = 'object'
        AND jsonb_typeof(manifest->'allowed_scales') = 'array'
        AND jsonb_typeof(manifest->'scope_node_ids') = 'array'
    ),
    canonical_effect BOOLEAN NOT NULL,
    previous_event_hash VARCHAR(64) NOT NULL,
    event_hash VARCHAR(64) NOT NULL UNIQUE,
    CONSTRAINT uq_fieldwork_project_position UNIQUE (project_id, project_position),
    CONSTRAINT uq_fieldwork_branch_sequence UNIQUE (branch_id, branch_sequence),
    CONSTRAINT fk_fieldwork_event_cycle FOREIGN KEY (project_id, cycle_id)
        REFERENCES fieldwork_cycles(project_id, cycle_id) ON DELETE RESTRICT,
    CONSTRAINT ck_fieldwork_event_chronology CHECK (
        observed_at <= recorded_at AND recorded_at <= committed_at
    ),
    CONSTRAINT ck_fieldwork_event_hashes CHECK (
        event_hash ~ '^[0-9a-f]{64}$'
        AND (previous_event_hash = '' OR previous_event_hash ~ '^[0-9a-f]{64}$')
    )
);

CREATE INDEX IF NOT EXISTS ix_fieldwork_events_project_commit
    ON fieldwork_events(project_id, committed_at);
CREATE INDEX IF NOT EXISTS ix_fieldwork_events_cycle_kind
    ON fieldwork_events(project_id, cycle_id, kind);
CREATE INDEX IF NOT EXISTS ix_fieldwork_events_actor
    ON fieldwork_events(project_id, actor_id, actor_role);

CREATE TABLE IF NOT EXISTS fieldwork_scope_versions (
    event_id VARCHAR(160) PRIMARY KEY REFERENCES fieldwork_events(event_id) ON DELETE RESTRICT,
    project_id VARCHAR(120) NOT NULL,
    cycle_id VARCHAR(120) NOT NULL,
    branch_id VARCHAR(160) NOT NULL,
    graph_version INTEGER NOT NULL CHECK (graph_version >= 1),
    graph JSONB NOT NULL CHECK (
        jsonb_typeof(graph) = 'object'
        AND jsonb_typeof(graph->'nodes') = 'array'
        AND jsonb_typeof(graph->'edges') = 'array'
    ),
    committed_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_fieldwork_scope_version
        UNIQUE (project_id, cycle_id, branch_id, graph_version)
);

CREATE OR REPLACE FUNCTION fieldwork_reject_mutation()
RETURNS TRIGGER LANGUAGE plpgsql AS $fieldwork_mutation$
BEGIN
    RAISE EXCEPTION 'fieldwork tables are append-only: % is forbidden', TG_OP
        USING ERRCODE = '55000';
END;
$fieldwork_mutation$;

CREATE OR REPLACE FUNCTION fieldwork_validate_event_branch()
RETURNS TRIGGER LANGUAGE plpgsql AS $fieldwork_branch_guard$
DECLARE
    branch_mode VARCHAR(24);
BEGIN
    SELECT mode INTO branch_mode
      FROM fieldwork_branches
     WHERE branch_id = NEW.branch_id AND project_id = NEW.project_id;
    IF branch_mode IS NULL THEN
        RAISE EXCEPTION 'fieldwork event branch is missing or belongs to another project';
    END IF;
    IF NEW.canonical_effect <> (branch_mode = 'canonical') THEN
        RAISE EXCEPTION 'fork events cannot write canonical effects';
    END IF;
    IF branch_mode = 'counterfactual'
       AND NEW.kind <> 'branch.forked'
       AND NEW.epistemic_layer <> 'counterfactual' THEN
        RAISE EXCEPTION 'counterfactual events must remain epistemically explicit';
    END IF;
    RETURN NEW;
END;
$fieldwork_branch_guard$;

DROP TRIGGER IF EXISTS fieldwork_event_branch_guard ON fieldwork_events;
CREATE TRIGGER fieldwork_event_branch_guard
BEFORE INSERT ON fieldwork_events
FOR EACH ROW EXECUTE FUNCTION fieldwork_validate_event_branch();

DO $fieldwork_triggers$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'fieldwork_projects', 'fieldwork_cycles', 'fieldwork_branches',
        'fieldwork_events', 'fieldwork_scope_versions'
    ] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS fieldwork_append_only ON %I', table_name);
        EXECUTE format(
            'CREATE TRIGGER fieldwork_append_only BEFORE UPDATE OR DELETE ON %I '
            'FOR EACH ROW EXECUTE FUNCTION fieldwork_reject_mutation()',
            table_name
        );
    END LOOP;
END;
$fieldwork_triggers$;

CREATE OR REPLACE VIEW fieldwork_current_consent AS
SELECT DISTINCT ON (events.project_id, events.payload->>'subject_id')
    events.project_id,
    events.payload->>'subject_id' AS subject_id,
    CASE WHEN events.kind = 'consent.granted' THEN 'granted' ELSE 'withdrawn' END AS status,
    events.event_id,
    events.committed_at
FROM fieldwork_events AS events
JOIN fieldwork_branches AS branches ON branches.branch_id = events.branch_id
WHERE branches.mode = 'canonical'
  AND events.kind IN ('consent.granted', 'consent.withdrawn')
  AND COALESCE(events.payload->>'subject_id', '') <> ''
ORDER BY
    events.project_id,
    events.payload->>'subject_id',
    events.project_position DESC;
