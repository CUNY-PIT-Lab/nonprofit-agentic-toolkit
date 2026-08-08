-- Persist each user's pseudonymous telemetry-consent scope independently of
-- authentication secrets. Existing branch-build consent history keeps its
-- last scope so a later withdrawal still governs the associated events.

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS telemetry_scope_id VARCHAR(120);

WITH latest_legacy_scope AS (
    SELECT DISTINCT ON (actor_id)
        actor_id,
        consent_scope_id
    FROM product_telemetry_consents
    WHERE actor_id <> ''
      AND consent_scope_id <> ''
    ORDER BY actor_id, decided_at DESC, decision_id DESC
)
UPDATE users AS user_row
SET telemetry_scope_id = latest_legacy_scope.consent_scope_id
FROM latest_legacy_scope
WHERE user_row.telemetry_scope_id IS NULL
  AND latest_legacy_scope.actor_id = user_row.id;

UPDATE users
SET telemetry_scope_id = 'scope.' || gen_random_uuid()::text
WHERE telemetry_scope_id IS NULL OR telemetry_scope_id = '';

ALTER TABLE users
    ALTER COLUMN telemetry_scope_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'uq_users_telemetry_scope_id'
          AND conrelid = 'users'::regclass
    ) THEN
        ALTER TABLE users
            ADD CONSTRAINT uq_users_telemetry_scope_id
            UNIQUE (telemetry_scope_id);
    END IF;
END
$$;
