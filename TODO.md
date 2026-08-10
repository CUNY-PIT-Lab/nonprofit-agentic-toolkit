# Next steps — Nonprofit AI Toolkit

This list turns the shipped dynamic-pathway, replay, evaluation, sidecar, and governed-evolution foundations into a safe initial-user beta. It is deliberately ordered: no user-facing adaptation is enabled until authentication, consent, replay, and rollback gates are demonstrably working in the target environment.

## 0. Restore production readiness — block beta invitations

- [ ] **Rotate the model-provider credential that was pasted into chat.** Create a replacement in the provider console, update it only in Railway's production variable store, and revoke the exposed value. Do not put either value in `.env.example`, Git, logs, or this file.
- [ ] **Verify the live Ollama configuration without exposing a secret.** Confirm `MODEL_BACKEND=ollama`, the intended `TOOLKIT_MODEL`, and a successful bounded authenticated model/sidecar smoke test. Record only the model identifier, deployment id, timestamp, and pass/fail result.
- [ ] **Make account email ready.** Configure the approved sender, delivery-provider credentials, and verified domain; then complete registration, email verification, sign-in, reset, and sign-out using a disposable test account. Production health currently reports `auth_ready: false`, so no real-user invitation should go out before this is green.
- [ ] **Prove the deployed database state.** On the target Railway environment, confirm migrations `004` through `009` appear exactly once in `schema_migrations`, take a recoverable backup, and run a restore drill against an isolated database.
- [x] **Write a minimal production incident runbook.** [The runbook](docs/PRODUCTION_INCIDENT_RUNBOOK.md) covers model outage, email outage, failed migration, replay-integrity failure, consent withdrawal, and identity-rollout rollback, with Zach Muhlbauer as the named primary decision maker.

**Exit evidence:** terminal Railway deployment `SUCCESS`; `/health` reports database connected and email ready; the model smoke test is successful; migrations and backup/restore are documented without secrets.

## 1. Establish a safe release lane — before new feature work

- [x] **Create a Railway staging environment with an independent PostgreSQL database.** Environment `staging` has its own `Postgres-31MV` service and private database hostname; the staging application uses in-memory email, the stub model, disabled telemetry, and a fresh database with zero users, organizations, adoption records, or fieldwork events.
- [ ] **Enable PR environments from staging.** Require the full test suite, forward-only migration checks, authenticated smoke tests, and a manual mobile/desktop review before merging.
- [ ] **Create a release checklist and changelog template.** Every release records Git SHA, migration list, model identifier, config/flag versions, test result, accessibility/browser result, approver, and rollback target.
- [ ] **Add a live acceptance script for the release gates.** It should verify health, evaluation status, no recent 5xx spike, a safe authenticated path, and the public identity resolver—without collecting content or credentials.

**Exit evidence:** a synthetic-record deployment passes all checks in staging and can be rolled back cleanly.

## 2. Validate the new review surfaces with staff — Phase 0

- [ ] **Exercise every pathway route on synthetic records.** Cover proceed, negotiate-and-return, pause/resume, non-AI redesign, walk away, reassess, and retire; confirm a return/reassess creates a fresh cycle and preserves the earlier pass.
- [ ] **Run replay and withdrawal drills.** Verify exact-output hashes and deterministic projection hashes across a restart, fieldwork and pathway replay at multiple authorized scales, historical/counterfactual branch isolation, and redaction after withdrawal.
- [ ] **Validate authorization boundaries.** Test owner, reviewer, ordinary-member, and cross-organization access. Confirm scoped evidence, derived outputs, evaluation cards, and sidecar citations all fail closed outside their authorization.
- [ ] **Complete the evaluation-workspace acceptance pass.** Test owner/reviewer access, member denial, placement/note/annotation, exact retry, stale-write conflict, as-of replay, logout state clearing, and mobile/desktop views with no console errors or overflow.
- [ ] **Load-test the informational sidecar boundary.** Confirm capacity overflow fails fast with `503` and `Retry-After`, no content is persisted, and unrelated requests remain responsive while the model is slow or unavailable.

**Exit evidence:** a signed Phase 0 test record, results for every scenario above, and any defects fixed through the normal PR/staging path.

## 3. Run a consented initial-user beta — Phase 1

- [ ] **Approve the beta protocol and onboarding.** State what the toolkit stores, what the sidecar does not store, who can access each scale, how withdrawal works, and the always-available non-AI path.
- [ ] **Invite a named small cohort only after explicit consent.** Give participants a human support/contact route and a clear way to pause, withdraw, export, or request correction.
- [ ] **Keep product telemetry off by default.** If it is enabled for a beta cohort, verify opt-in/withdrawal, allowed signal keys, small-cell suppression, retention, and that no free text or fieldwork content enters telemetry.
- [ ] **Hold a weekly evidence review.** Product owner and research lead review fieldwork observations, reflexive memos, member checks, failure modes, accessibility feedback, and a no-AI alternative—not raw sidecar chat.
- [ ] **Keep evolution proposals inert.** Run `python -m backend.evolve` manually only with the approved cohort, purpose, threshold, and reviewer present. A proposal is evidence for review, never an automatic deployment or rename.

**Exit evidence:** documented consent, an opt-out/withdrawal drill, a weekly review cadence, and no automatic changes to pathways, prompts, or identity.

## 4. Govern iteration, naming, and promotion — Phase 2+

- [ ] **Adopt a proposal review board.** Require evidence checksum, affected pathway/version, equity and accessibility impact, participant/staff feedback, evaluation plan, named approver, rollout cohort, and rollback target.
- [ ] **Use a controlled change sequence.** `proposal → human review → implementation PR → staging evaluation → named rollout → outcome evaluation → retain or rollback`. Pin existing records to their historical pathway/version unless an accountable human accepts migration.
- [ ] **Make naming changes reversible and historical.** Only an approved, separately recorded name rollout may update the identity resolver. Preserve aliases in replays; never rename stable IDs, routes, event names, or checksums.
- [ ] **Publish a versioned decision log.** Link each applied change to its proposal, de-identified evidence, test results, rollout cohort, and rollback outcome. Do not convert clickstream into ethnographic interpretation.
- [ ] **Define cross-organization governance before enabling it.** No shared raw corpus, cross-organization projection, or network-scale comparison without agreements, purpose limits, authorization design, and qualitative-context review.

**Exit evidence:** one complete, reversible change rehearsal on synthetic/staff data with a human-approved rollback.

## 5. Add infrastructure only after the beta gates hold

- [ ] **Implement a fail-closed server-side feature-flag adapter.** Start with staff and named organizations; log only flag resolution metadata. A flag may gate new use, never rewrite evidence or consent history.
- [ ] **Add privacy-reviewed observability.** Prefer sanitized operational metrics/traces; block bodies, cookies, authorization headers, identifiers, free text, and source content. Test with deliberately sensitive synthetic payloads before enabling export.
- [ ] **Decide whether to split the sidecar into a private Railway service.** If adopted, give it a separate identity and a narrow, read-only projection API—never the primary application database credential.
- [ ] **Add a bounded Railway cron job only after governance approval.** It may run idempotent maintenance such as proposal generation, integrity sampling, or backup reminders; it must not collect interaction events or deploy changes.
- [ ] **Implement trusted per-principal scope assignments and participant-to-subject bindings before expanding fieldwork access.** Until then, retain the current fail-closed policy for ordinary-member scoped access and consent on behalf of a participant.

**Exit evidence:** each infrastructure addition has its own threat model, data-flow/retention decision, staging proof, owner, monitoring, and tested rollback.

## Current boundaries to preserve

- No credentials, participant content, sidecar prompts/answers, or raw fieldwork payloads in Git, telemetry, or third-party observability.
- No automatic pathway, prompt, interface, or identity rollout from a model, cron job, or product signal.
- No claim of a separately isolated sidecar, targeted Railway flags, cron automation, or third-party observability until it is actually configured and verified.
- No production promotion based only on local tests: require the live checks and named human approval above.
