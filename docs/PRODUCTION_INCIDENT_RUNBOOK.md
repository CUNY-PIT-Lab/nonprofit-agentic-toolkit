# Production incident runbook

This is the minimum operational response for the production Nonprofit AI Toolkit. It covers service safety and decision authority; it does not authorize rewriting evidence, deleting ledger rows, exposing secrets, or treating a backup as an automatic consent exception.

## Authority and first response

**Zach Muhlbauer is the primary on-call decision maker for every incident in this runbook.** Until a replacement roster is approved in writing, only Zach may authorize a production rollback, emergency configuration change, data recovery, release hold, or return to service. A research or organization stakeholder can supply context, but cannot override this safety boundary.

For every incident:

1. Record UTC time, environment, service/deployment id, symptom, scope, and the decision made. Do not record participant text, source content, cookies, tokens, email addresses, or secret values.
2. Confirm the issue is in `production`, not the isolated staging environment. Pause nonessential deployments and manual evolution runs while the incident is assessed.
3. Use bounded Railway readbacks: deployment status, runtime logs, HTTP-error logs, and `/health`. A healthy `/health` only proves the database connection and email readiness; it does not prove model completion or replay integrity.
4. Preserve append-only evidence. Do not edit or delete pathway, fieldwork, evaluation, telemetry, evolution, or audit rows to make an incident disappear.
5. Zach records the go/no-go decision and the verification result before normal operation resumes.

Useful read-only commands (do not pass secrets and keep log windows bounded):

```bash
railway status --project nonprofit-agentic-toolkit --environment production --json
railway deployment list --project nonprofit-agentic-toolkit --environment production --service toolkit-api --limit 5 --json
railway logs --project nonprofit-agentic-toolkit --environment production --service toolkit-api --since 1h --lines 200 --json
railway logs --project nonprofit-agentic-toolkit --environment production --service toolkit-api --http --status '>=400' --lines 100 --json
curl --fail --silent --show-error https://toolkit-api-production-535d.up.railway.app/health
```

## Incident actions

| Incident | On-call decision maker | Immediate containment | Verify before closing |
|---|---|---|---|
| Model outage or unsafe model behavior | Zach Muhlbauer | Leave saved records, pathway decisions, and fieldwork evidence intact. Guided routing and synthesis have bounded deterministic fallbacks; the sidecar returns a non-persisting error. Tell affected users that model-backed responses are unavailable; do not substitute the production model with the local stub adapter. Pause sidecar use if its response cannot be trusted. If a credential is suspected, rotate it in the provider and Railway—not in source control. | A bounded synthetic-record model and sidecar request succeeds with the approved model identifier; no response was silently written as canonical evidence; Railway errors return to baseline. |
| Email delivery outage | Zach Muhlbauer | Pause invitations and tell users not to expect verification or reset delivery until recovery. Configuration-ready health does not prove delivery, and runtime delivery failures can still return generic responses. Do not switch production to the in-memory email backend. Check sender/domain/provider status without placing recipient data in logs. | A disposable account completes registration, verification, sign-in, password-reset, and sign-out; `/health` reports `auth_ready: true`; no real-user email was used as a test fixture. |
| Failed migration or pre-deploy failure | Zach Muhlbauer | Stop the release and keep the last successful deployment serving traffic. Capture the failed deployment id and bounded build/pre-deploy logs. Do not run ad hoc DDL or manually alter `schema_migrations` in production. | The repair has passed twice against staging PostgreSQL; the production pre-deploy migration reaches terminal `SUCCESS`; `schema_migrations` contains each expected version once; `/health` passes. An image rollback is not described as a database rollback. |
| Replay-integrity failure | Zach Muhlbauer | Treat it as an evidence-integrity incident. Stop relying on the affected replay, cycle, branch, output, or pathway decision; prevent further interpretation or publication from it. There is no runtime flag adapter or record-write pause control, so use a service/deployment hold only if the impact requires broader containment. Preserve the original append-only ledger and capture only ids/checksums in the incident record. | An authorized full replay and checkpoint-plus-tail replay yield the expected canonical state hash; pathway decision hashes and stored-output hashes match; the corrective release passes the replay, withdrawal, authorization, and counterfactual suites in staging before production promotion. |
| Consent withdrawal | Zach Muhlbauer | Authenticate the requester and confirm the authorized subject/record scope. Append the appropriate canonical withdrawal—product-signal opt-out and fieldwork withdrawal are distinct flows—and never overwrite the earlier grant. Ordinary members currently fail closed for fieldwork withdrawal on behalf of a subject. Stop sharing the affected projection or derived output while the result is checked. | Current and historical authorized projections redact the withdrawn material; exact output replay also redacts/denies any direct or nested derivative; current telemetry aggregates omit the withdrawn consent scope. Follow the approved retention/erasure policy separately for backups and third-party systems. |
| Identity-rollout rollback | Zach Muhlbauer | Freeze further name proposals, reviews, and rollout actions. Do not change display text, stable ids, routes, event names, or database rows by hand. There is no public HTTP or CLI rollback command: only a reviewed `EvolutionStore.record_rollout_action(..., action="rollback")` operator invocation with a trusted baseline may append the rollback. If that procedure has not been vetted, identity rollouts remain disabled. | `GET /api/product-evolution/identity` returns the previous valid identity or checked-in default with a valid action-checksum chain; aliases remain available for historical context; browser readback and rollback evaluation pass. |

## Recovery guardrails

- Railway deployment rollback restores a prior image and its custom variables; it does not reverse database migrations or erase newer evidence. Use forward repair unless an explicitly approved recovery plan says otherwise.
- Product evolution is inert by design. A proposal or approval does not deploy code or rename the toolkit. Keep all evolution work paused until Zach explicitly resumes it.
- Consent withdrawal changes present access and downstream projection eligibility. It does not by itself delete protected content from backups; follow the separately approved retention, erasure, and backup-recovery procedure.
- Do not use production records, participant text, or real email recipients to prove recovery. Use staging and synthetic or expressly consented test data.

## Closeout record

Zach records: incident id; decision time; affected environment/service/deployment; containment; validation steps and results; data/consent impact; rollout or recovery action; and follow-up owner/date. Link the record to the release or governance decision without copying sensitive content into it.
