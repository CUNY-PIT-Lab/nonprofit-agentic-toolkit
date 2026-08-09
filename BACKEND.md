# Toolkit backend

FastAPI serves the interface and API from one origin. PostgreSQL stores verified users, organization memberships, adoption records, stage conversations, completed steps, model runs, knowledge snippets, synthesis versions, concept-map versions, annotations, audit events, pinned pathway runs, append-only fieldwork, and governed product-evolution evidence. SQLite and in-memory email are local-development fallbacks.

## Local run

```bash
python3 -m pip install -r requirements.txt
./run.sh
```

Without an Ollama key, `run.sh` selects the deterministic local model adapter. Verification email remains required. Open `/api/dev/outbox` on the loopback server to retrieve the local verification or reset link, then follow the normal token flow. The outbox route and model adapter are unavailable in production.

Run the key-free checks with:

```bash
./run.sh test
```

`run.sh` selects Python 3.11 or newer, preferring the repository `.venv`. CI and Railway use Python 3.12.

### Local authenticated API session

The API keeps the same origin and CSRF contract in development. With the server running, this creates a verified local account and an adoption record. It requires `jq` only for the shell example:

```bash
BASE=http://127.0.0.1:8765
COOKIE_JAR=/tmp/toolkit-cookies.txt

CSRF=$(curl -sS -c "$COOKIE_JAR" "$BASE/api/auth/session" | jq -r .csrf_token)

curl -sS -b "$COOKIE_JAR" -c "$COOKIE_JAR" \
  -H "Origin: $BASE" -H "X-CSRF-Token: $CSRF" \
  -H 'Content-Type: application/json' \
  -d '{"email":"local@example.test","password":"local-development-only"}' \
  "$BASE/api/auth/register"

VERIFY_TOKEN=$(curl -sS "$BASE/api/dev/outbox" | \
  jq -r '.messages[-1].link | split("token=")[1]')

curl -sS -b "$COOKIE_JAR" -c "$COOKIE_JAR" \
  -H "Origin: $BASE" -H "X-CSRF-Token: $CSRF" \
  -H 'Content-Type: application/json' \
  -d "{\"token\":\"$VERIFY_TOKEN\"}" \
  "$BASE/api/auth/verify"

LOGIN=$(curl -sS -b "$COOKIE_JAR" -c "$COOKIE_JAR" \
  -H "Origin: $BASE" -H "X-CSRF-Token: $CSRF" \
  -H 'Content-Type: application/json' \
  -d '{"email":"local@example.test","password":"local-development-only"}' \
  "$BASE/api/auth/login")
CSRF=$(printf '%s' "$LOGIN" | jq -r .csrf_token)

RECORD=$(curl -sS -b "$COOKIE_JAR" -c "$COOKIE_JAR" \
  -H "Origin: $BASE" -H "X-CSRF-Token: $CSRF" \
  -H 'Content-Type: application/json' \
  -d '{"organization_name":"Local test organization","title":"Local replay test","proposed_use":"Explore a bounded internal use."}' \
  "$BASE/api/records")
RECORD_ID=$(printf '%s' "$RECORD" | jq -r .record.id)
```

The development outbox is loopback-only and does not exist in production. Use a disposable local email and password. The follow-on pathway and fieldwork examples are in [Dynamic evolution and replay](docs/DYNAMIC_EVOLUTION_AND_REPLAY.md).

## Production environment

- `APP_ENV=production`
- `DATABASE_URL` from the private Railway PostgreSQL service
- `PUBLIC_APP_URL`, fixed to the canonical HTTPS toolkit origin
- `EMAIL_BACKEND=resend`
- `RESEND_API_KEY`
- `MAIL_FROM`, using a verified sending domain
- `AUTH_PEPPER` or `SESSION_SECRET`, at least 32 random characters
- `MODEL_BACKEND=ollama`
- `OLLAMA_API_KEY`
- `TOOLKIT_MODEL=glm-5.2`
- `PRODUCT_TELEMETRY_ENABLED=false`, unless opt-in collection is approved
- `TELEMETRY_COHORT=beta`
- `TELEMETRY_MIN_CELL_SIZE=3`

Registration returns `503` until the production email configuration is complete. The backend also refuses PostgreSQL-free production startup, a non-HTTPS public URL, an absent authentication pepper, or the local model adapter.

## Browser contract

Call `GET /api/auth/session` first. It sets a readable CSRF cookie and returns the same token as `csrf_token`. Every state-changing request must send the exact application `Origin`, cookies, and `X-CSRF-Token`. Production uses host-only `__Host-` cookie names with `Secure`, `SameSite=Lax`, and root paths.

Verification and reset links place the opaque token in the URL fragment:

```text
/#verify?token=...
/#reset?token=...
```

The browser extracts the fragment and submits the token to `POST /api/auth/verify` or `POST /api/auth/reset-password`. Tokens do not enter query strings or routine access logs.

Stage messages use `POST /api/records/{record_id}/stages/{stage}/messages` with `content` and an `idempotency_key`. The browser creates one stable key for each submission and reuses it when retrying the same request. Turns, state, completions, and retry keys are scoped by the pathway `cycle_number`, so negotiate-and-return or reassessment can revisit a stage without colliding with its earlier pass. Pause/resume preserves the current pass. The server enforces the pinned node and loads membership, cycle-local history, completed-stage context, and the system prompt.

Stage completion now returns `route_required=true`, `cycle_number`, and pathway state. It records readiness bound to that node and cycle but leaves the route to a separate attributed approval and transition. A blocked stage records `stage_blocked=true` and cannot Proceed. Clients must not advance through a hardcoded stage list after completion.

## Versioned pathway API

Every newly created or explicitly initialized pathway run is pinned to the checksum of an immutable pathway family and version. Record creation can select the `author`, `reviewer`, or `monitor` entry point; selecting a non-author entry requires an owner or invited reviewer. The current evidence view resolves the latest append-only fact and approval for each key. Proposed facts never satisfy transition conditions. Transition rows store the exact confirmed facts and approved gates used, then form a SHA-256 decision chain that is verified on load.

Record-scoped routes:

- `POST /api/records/{record_id}/pathway` ensures a run exists and selects an author, reviewer, or monitor entry role.
- `GET /api/records/{record_id}/pathway` returns the pinned definition, run, confirmed facts, approved gates, available transitions, and journal.
- `POST /api/records/{record_id}/pathway/facts` appends a proposed, confirmed, or rejected fact.
- `POST /api/records/{record_id}/pathway/approvals` appends an owner/reviewer approval, rejection, or request for changes.
- `POST /api/records/{record_id}/pathway/checkpoints` records server-owned, node- and cycle-bound readiness for synthesis or pilot after explicit human confirmation.
- `POST /api/records/{record_id}/pathway/transitions` chooses one eligible outcome with a rationale and idempotency key.

Proceed requires current confirmed `stage_ready=true`, matching `stage_ready_node` and `stage_ready_cycle`, `stage_blocked=false`, and the evidence-bound owner gate for the current node. Guided completion owns readiness through internal/external review. Synthesis and pilot use the dedicated checkpoint route; the generic fact route cannot write reserved readiness keys. Negotiate-and-return and reassessment start a new cycle with false readiness; pause/resume preserves the cycle, and resume is invalid while already active. Non-AI, walk away, and retire remain explicit terminal routes. Retrying one successful checkpoint or transition with the same key returns the stored result without appending another fact, decision, or audit event.

## Fieldwork and replay API

Fieldwork is an append-only event ledger scoped to an adoption record. Each event retains actor role, three-part chronology, causal ids, source versions, epistemic layer, sensitivity, allowed scales, consent basis, scope nodes, and a version manifest. Actor ids come from authentication and actor roles are resolved from organization membership after record access, never from request data. PostgreSQL triggers reject updates and deletes, while reconstitution verifies branch chains and content hashes.

The API supports:

- cycle creation and listing;
- typed observations, participant accounts, reflexive memos, positionality memos, interpretations, member checks, decisions, interventions, and after-effects;
- versioned acyclic scope graphs;
- canonical consent grants and withdrawals;
- historical and counterfactual forks;
- deterministic replay by branch, access scale, and optional `as_of_event_id`; and
- exact storage and retrieval of nondeterministic outputs with their input event ids and generator version.

Current canonical consent is applied to every projection, including historical views. A later withdrawal therefore redacts earlier consent-bound evidence. The HTTP API retrieves stored model output exactly; a regeneration callback exists in the domain layer but no HTTP model-regeneration runner is exposed.

Stored outputs cannot weaken their derivation inputs. The ledger recursively resolves nonempty backward inputs and enforces maximum sensitivity, the input-scale intersection, tag/scope/consent-subject unions, and consent-basis dominance. New outputs cannot use pending, withdrawn, unauthorized-cycle, or otherwise restricted inputs. Projection and exact replay repeat the dependency check, so a later withdrawal or access change redacts even a legacy weak-manifest output. Authorized forks may use inherited evidence inside their cutoff without gaining canonical effect.

Scope-graph nodes describe field topology; they do not grant themselves. Owners and exact reviewer memberships currently receive current-cycle record-wide scope-node authority. Ordinary members receive none until trusted per-principal assignments exist, so scoped replay redacts and scoped writes return `403` for them.

See [Dynamic evolution and replay](docs/DYNAMIC_EVOLUTION_AND_REPLAY.md) for endpoint tables and a local walkthrough.

The browser exposes a deliberately narrower workspace: cycle creation; observation, reflexive-memo, and positionality-memo entry; canonical replay by local scale and event boundary; an ephemeral informational sidecar for that selected replay context; and voluntary product-signal consent and name preference. Consent-bound and causal fieldwork entry types remain API-only.

Fieldwork consent changes use a separate server-derived authority contract. The request may identify the consent subject and reason, but it cannot choose actor role, sensitivity, disclosure scale, authorization tags, or scope. Organization owners and invited reviewers may record an on-behalf-of grant or withdrawal. Ordinary members have no participant identity binding in the current account schema and therefore fail closed. The router contract also supports a verified participant binding that can grant or withdraw only for that authenticated subject; the current application does not yet provision such bindings.

## Informational sidecar API

`POST /api/records/{record_id}/sidecar/chat` accepts a message, bounded user/assistant history, fieldwork cycle, branch, and access scale. The application verifies record membership, derives the ordinary fieldwork authorization context, and passes only the detached, current-consent fieldwork projection for that selected scale to the configured model. It does not append the broader adoption working record because that record has no field-level scale labels.

The router has no store or audit-writer dependency. It cannot write fieldwork, pathway facts, approvals, transitions, or proposals. Returned citations are filtered to event and source ids present in the snapshot. The response includes the selection, context hash, model version, and explicit `false` values for canonical effect, write authority, persistence, and exact replay.

The sidecar currently runs in the web process with the web service’s model adapter. Its prompt and answer are not stored. A process-local nonblocking capacity gate admits four model calls by default; overload returns `503` plus `Retry-After: 1` rather than occupying the shared worker pool. A separate private Railway service, service identity, and distributed capacity limit remain future defense-in-depth work, not current infrastructure.

## Governed product evolution

`GET /api/product-evolution/identity` is a public, read-only resolver for the current display identity.

Product-signal collection is disabled unless `PRODUCT_TELEMETRY_ENABLED` is true. It still requires each authenticated user to opt in through these endpoints:

- `GET /api/product-evolution/consent`
- `POST /api/product-evolution/consent`
- `POST /api/product-evolution/signals`

The signal body is a closed schema. It requires one allowlisted signal and an 8–120 character `idempotency_key`, with optional `helpful`, `elapsed_ms`, `pathway_stage`, `interface_state`, `route`, and `scale` fields. Each dimension value must also match a server-registered category; a short arbitrary token is not accepted. The event id is a deterministic hash of the pseudonymous consent scope, signal, and idempotency key, making an identical retry safe. It rejects free text and extra keys. Stored events contain only numeric/boolean measures, registered categorical dimensions, a categorical cohort, a server-generated random consent-scope id, purpose and sensitivity metadata, versions, chronology, and a hash-chain link. The scope is stored once on the account, never returned by the account API, and does not change when `AUTH_PEPPER` rotates. Consent rows use that pseudonym for actor provenance rather than storing the raw user id.

Current withdrawal removes earlier consent-bound events from later authorized aggregates without updating or deleting the stored event. Aggregate projections are deterministic, purpose/cohort/sensitivity bounded, and small-cell suppressed. `TELEMETRY_MIN_CELL_SIZE` is clamped between 3 and 100.

The domain worker accepts only this de-identified projection. The manual maintenance command builds the authorized projection and saves qualifying proposals:

```bash
PRODUCT_TELEMETRY_ENABLED=true \
TELEMETRY_COHORT=beta \
TELEMETRY_MIN_CELL_SIZE=3 \
python -m backend.evolve
```

The shipped registry has three rules: interface route confusion, preference for the “Fieldwork Loop” display name, and repeated negotiate-and-return selection. Each requires at least three distinct consent scopes and remains subject to the configured minimum-cell threshold; repeated clicks by one scope do not satisfy it. Proposal evidence is scoped to the rule's signal, and suppressed signals reveal no counts, hash root, or time window. An unrelated signal therefore cannot create another proposal for a threshold already crossed. The store reuses an unreviewed or approved proposal for the same component and target, and reconsiders a rejected target only after that signal's eligible evidence changes. New version targets start from the checksum-validated active rollout/rollback state. The command is deterministic and idempotent and prints only aggregate/projection checksums and counts plus proposal ids, types, and checksums. It cannot review, apply, rename, roll out, evaluate, or deploy a proposal.

The domain supports inert semantic-version proposals for a pathway, prompt, interface, or name. Each proposal binds evidence checksums to exact component/version rollout and rollback targets. Before append, the store reconstructs canonical proposal provenance, rejects incompatible component/type metadata, and rejects a review that predates proposal creation. An authorized human review is required before a separate rollout action. Rollout writers must receive an explicit trusted component baseline. A component-wide process lock plus PostgreSQL advisory transaction lock validates proposal/review/action checksums, approval chronology, active semantic version, rollback stack and restore target, and append chronology before insertion. Invalid or stale proposals, reviews, or actions add no immutable row and leave the identity resolver usable. Evaluations and rollbacks are also append-only. These records do not change code, prompts, pathways, Railway flags, or deployments on their own. For a name proposal, the public identity resolver changes only after explicit rollout and restores the previous or default identity after rollback. The response supplies the display name, aliases, semantic version, proposal checksum, ordered action checksums, and its source. The resolver validates the proposal, review, action, target, checksum, and semantic-version chain and fails closed on corrupt history. At boot, the browser uses it to update the document title and product-name labels. No rollout is created automatically or seeded in this branch.

The public HTTP API stops at consent and signal collection. Proposal generation is an operator command; review, rollout, and evaluation remain operator/domain-store workflows. No Railway cron invokes the command in this repository.

## Conversation evaluation workspace

`/evaluation` is a separate, protected reviewer surface over saved guided-stage passes. The server lists only stage conversations in organizations where the authenticated user has an exact `owner` or `reviewer` membership. Aggregate responses contain metadata only; transcript content is returned only after the same record-level authorization and inactivity checks run again.

Each reviewer has an independent append-only stream for a stage pass. Placement, note, and turn-annotation writes bind the canonical transcript checksum, the expected reviewer-stream version, a stable operation id, actor identity, stage/cycle identity, and the previous event hash. Exact retries return the original event result, stale versions or changed transcripts return `409`, and `as_of_evaluation_version` reconstructs an earlier reviewer state. Evaluation rows do not write adoption records, guided turns, pathway facts or transitions, fieldwork evidence, sidecar state, product telemetry, or evolution proposals. See [Conversation evaluation workspace](docs/EVALUATION_WORKSPACE.md) for the endpoint and release contract.

## Migrations and Railway

`python -m backend.migrate` creates the base schema, then leaves extension tables to numbered PostgreSQL migrations applied once in filename order. This ensures their PostgreSQL-specific constraints and triggers are installed. `004_fieldwork_replay.sql` adds the fieldwork ledger and append-only triggers. `005_versioned_pathways.sql` adds pathway definitions, facts, approvals, transitions, and immutable-row triggers. `006_governed_evolution.sql` adds product signals, signal-consent decisions, proposals, reviews, rollout/rollback actions, evaluations, and append-only triggers. `007_guided_stage_cycles.sql` gives guided turns, stage state, and completions a positive cycle number, backfills existing rows to cycle 1, and replaces their uniqueness and lookup indexes with cycle-aware definitions. `008_stable_telemetry_scope.sql` backfills a hidden, unique, non-null telemetry-consent pseudonym independently of authentication secrets. `009_conversation_evaluation.sql` adds reviewer-owned buckets, reviewer-private hash-chained evaluation events, optimistic-version uniqueness, and append-only mutation guards. SQLite development uses equivalent guarded migrations for existing databases.

`railway.json` runs this command as Railway’s pre-deploy step before starting Uvicorn and uses `/health` as the deployment healthcheck. CI applies the PostgreSQL migrations twice and verifies their versions, append-only triggers, hash/consent constraints, guided-cycle constraints, and stable telemetry-scope column. The current migrations are additive. A Railway image rollback does not reverse migrations or delete evidence. No Railway flags, cron, separate sidecar service, or third-party observability are configured. See [Railway beta operations](docs/RAILWAY_BETA_OPERATIONS.md) before enabling fieldwork or product-signal collection for beta users.
