# Conversation evaluation workspace

The toolkit serves a protected reviewer surface at `/evaluation`. It adapts the Fortune Digital Equity conversation-review board to the toolkit's versioned pathways and guided stage cycles without changing the meaning of the underlying adoption record.

## Review unit and access boundary

One evaluation card represents one saved guided stage pass:

```text
(organization, adoption record, stage, cycle number)
```

The card's stable identifier is the corresponding `StageState.id`. A pass becomes reviewable only after it contains at least one user turn and one assistant turn and has been inactive for the configured minimum interval.

Only a verified user with an `owner` or `reviewer` membership in the record's organization can list, open, place, note, or annotate that conversation. An ordinary member receives no aggregate evaluation workspace. Cross-organization access requires an explicit owner or reviewer membership in every organization; the application has no implicit global evaluator.

The list endpoint returns metadata only. Transcript content is loaded only from the scoped detail endpoint after the same membership and eligibility checks run again.

## What evaluation can and cannot do

Reviewers can:

- place a conversation in `Success`, `Needs work`, `Handoff`, `Not yet reviewed`, or one of their own bounded custom buckets;
- add a bounded reviewer note to the stage pass;
- annotate a canonical conversation turn as `Helpful`, `Unclear`, `Incorrect`, `Safety concern`, or `Other`; and
- reconstruct the evaluation state at an earlier evaluation version.

Evaluation never approves a pathway gate, executes a transition, changes stage coverage, writes a fieldwork interpretation, edits a transcript, or emits product telemetry on behalf of the conversation author. These labels describe conversation quality, not the organization's `Proceed`, `Negotiate and return`, `Walk Away`, or other pathway decision.

The informational sidecar remains ephemeral and is not included. This workspace reviews the guided conversations that the toolkit already saves as part of an adoption record; it does not introduce a second transcript-capture system.

## Replay and concurrency

Every evaluation mutation is an append-only event. It binds:

- a caller-generated operation ID for exact retry handling;
- the reviewer and stage-state identity;
- the record, stage, and cycle;
- the expected and resulting evaluation version;
- a checksum of the ordered canonical transcript;
- the prior event hash and the new event hash; and
- the bounded placement, note, or annotation fields needed to rebuild the review state.

The server reduces these events to produce current state or an `as_of_evaluation_version` projection. Current detail always pairs the current canonical turns with their current `transcript_checksum`, which is the token for the next write. When an earlier evaluation version was created against an older transcript, `evaluated_transcript_checksum` and the event history preserve that distinction rather than presenting the old checksum as current. The server rejects a write if another evaluation event has advanced the version or if the guided transcript changed after the reviewer opened it. An exact operation retry returns its original result; the same operation ID cannot identify different content. PostgreSQL guards prevent update or deletion of evaluation events.

Annotations retain the validated `ConversationTurn.id`; they do not copy transcript text into the evaluation ledger. An individual turn deletion cannot delete one event and puncture the reviewer stream. Deleting an adoption record cascades its complete evaluation event stream through the record and stage-state relationships. Custom buckets remain reviewer-owned and independent of any one record.

## HTTP surface

The page reuses the toolkit's verified account, opaque session cookie, and same-origin CSRF protection.

```text
GET  /api/evaluation/status
GET  /api/evaluation/organizations
GET  /api/evaluation/buckets
POST /api/evaluation/buckets
GET  /api/evaluation/conversations
GET  /api/evaluation/conversations/{stage_state_id}
PUT  /api/evaluation/conversations/{stage_state_id}/placement
PUT  /api/evaluation/conversations/{stage_state_id}/note
PUT  /api/evaluation/conversations/{stage_state_id}/annotations/{turn_id}
```

The browser supports search, bucket visibility and ordering controls, comfortable and compact layouts, drag-and-drop with a select control as the accessible fallback, transcript review, notes, and per-message annotations. View preferences are local to the browser; evaluation facts remain server-side.

## Configuration and release

```text
TOOLKIT_EVALUATION_ENABLED=1
TOOLKIT_EVALUATION_MIN_INACTIVE_SECONDS=60
```

Migration `009_conversation_evaluation.sql` creates the reviewer buckets and append-only evaluation event ledger. Railway runs numbered migrations in the pre-deploy phase before starting the new application release.

Release verification should include:

1. the complete automated suite and migration checks;
2. anonymous `/evaluation` rendering only the login surface;
3. owner/reviewer access and ordinary-member/cross-organization denial;
4. a placement, note, annotation, exact retry, stale-version rejection, and as-of replay;
5. desktop and mobile rendered checks with no console errors or horizontal overflow;
6. live `/health`, `/api/evaluation/status`, and `/evaluation` readback after Railway reports a terminal `SUCCESS`; and
7. bounded Railway runtime and HTTP-error logs.
