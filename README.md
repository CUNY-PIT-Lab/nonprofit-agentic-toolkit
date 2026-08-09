# Nonprofit AI toolkit

<div align="center">

**[▶ Live deployment — zmuhls.github.io/nonprofit-agentic-toolkit](https://zmuhls.github.io/nonprofit-agentic-toolkit/)**

Built and deployed by [@zmuhls](https://github.com/zmuhls) · [CUNY AI Lab](https://github.com/CUNY-AI-Lab)

</div>

The toolkit helps nonprofit staff examine a proposed use of AI before the organization commits to it. A guided conversation records context, constraints, evidence, owners, and open questions. Six review steps lead to a synthesis that connects the organization’s responses in an interactive concept map. The conversation now follows structured coverage rather than fixed answer counts, and a versioned pathway records what the organization decides to do next.

A verified account is required. Staff can return to saved reviews, inspect the responses behind each map node, add annotations, and export the synthesis.

## The review

The entry screen records the proposed use and current concerns. The guide then asks one question at a time through:

1. Red line test
2. Stress test
3. Costs and benefits
4. Hidden curriculum
5. Accountability
6. Internal and external review
7. Synthesis

The synthesis reviews the full adoption record. It examines context, constraints, affordances, existing AI infrastructure, and four possible use patterns: workflow support; company-knowledge discovery and interpretation; a general-purpose chatbot; and a public information guide or website sidecar. It maps current conditions, decision points, pathways, potentials, and open questions.

The interface renders the map with the self-hosted MIT-licensed Cytoscape.js library. Model claims and map elements remain tied to saved response identifiers.

## Dynamic pathways and fieldwork replay

Each new or pathway-initialized adoption record is pinned to an immutable pathway definition. Confirmed facts and human approvals determine which transitions are eligible. Proceed, negotiate and return, pause/resume, non-AI redesign, walk away, reassessment, and retirement are first-class outcomes. Completing a review stage records readiness but does not let the model choose the organization’s route.

The backend also provides append-only ethnographic fieldwork cycles. Observations, participant accounts, reflexive and positionality memos, member checks, interpretations, decisions, interventions, and after-effects retain actor, chronology, source, consent, scale, and software-version provenance. Authorized clients can replay an exact stored output, derive a deterministic projection at a selected scale and event boundary, or fork a historical or explicitly counterfactual branch without changing canonical history. Derived outputs inherit every direct and nested input restriction, so withdrawal or later authorization loss also redacts their projection and exact replay.

The browser includes pathway decisions; a bounded fieldwork workspace for cycles, observations, reflexive and positionality memos, and canonical replay; an ephemeral informational sidecar; voluntary product-signal consent and name preference controls; and a protected conversation-evaluation board for organization owners and reviewers. The evaluation board classifies saved guided-stage passes, stores reviewer-private notes and turn annotations in a replayable ledger, and never changes the adoption record, pathway, fieldwork evidence, sidecar, or telemetry. The HTTP API carries the advanced fieldwork operations. The sidecar answers from an authorized cycle without writing or persisting its answer. Separately, the opt-in product-evolution API stores only allowlisted categorical and numeric signals with idempotent retry keys. The manual `python -m backend.evolve` command can turn a consent-filtered, small-cell-suppressed aggregate into deterministic, inert interface, pathway, or display-name proposals; the domain also supports prompt proposals. Human review, rollout/rollback records, evaluation, and a public current-identity resolver are implemented. Only an explicit governed rollout activates a proposed name. The browser reads the resolver at boot to update its document title and product-name labels. Applying or deploying other proposal types remains an external release step. See [Conversation evaluation workspace](docs/EVALUATION_WORKSPACE.md), [Dynamic evolution and replay](docs/DYNAMIC_EVOLUTION_AND_REPLAY.md), and [Railway beta operations](docs/RAILWAY_BETA_OPERATIONS.md) for the exact boundaries.

## Saved work

PostgreSQL stores:

- verified users, sessions, organizations, and memberships
- adoption records, stage conversations, and completed steps
- immutable pathway definitions, confirmed facts, approvals, and transition journals
- append-only fieldwork cycles, branches, scope graphs, consent events, and stored outputs
- reviewer-private, hash-chained conversation placements, notes, and turn annotations
- hash-chained product signals, signal-consent decisions, evolution proposals, human reviews, rollout/rollback records, and evaluations
- model runs and response-based knowledge snippets
- synthesis and concept-map versions
- map annotations and audit events

The browser receives an opaque host-only session cookie. Passwords use Argon2id. Email verification and password-reset tokens are single-use, hashed in the database, and placed in URL fragments so routine request logs do not receive them.

## Local development

Install the Python dependencies and start the same-origin app:

```bash
python3 -m pip install -r requirements.txt
./run.sh
```

Open `http://127.0.0.1:8765`. Local development uses SQLite, a deterministic model adapter when `OLLAMA_API_KEY` is absent, and an in-memory email outbox at `/api/dev/outbox`. Those adapters and the outbox are unavailable in production.

Run the key-free checks:

```bash
./run.sh test
python3 -m unittest discover -s tests -p 'test_*.py'
```

## Pi concept-map tool

`pi-harness/` ships the governed Pi agent alongside the web app. It includes:

- `src/concept-map.ts`, a harness-independent map builder
- `.pi/extensions/concept-map.ts`, the `build_synthesis_concept_map` Pi tool
- `.pi/skills/synthesis-concept-map/SKILL.md`, the agent skill
- deterministic guardrail and map tests

The tool produces stable, annotatable node and edge identifiers that the web interface can render with Cytoscape.js.

## Production

Railway runs FastAPI and PostgreSQL on the same private project network. The app serves its interface and API from one HTTPS origin.

Before each deployment, Railway runs `python -m backend.migrate`. The forward-only runner applies the checked-in PostgreSQL fieldwork, pathway, governed-evolution, and conversation-evaluation migrations once, then Railway gates traffic on `/health`. The sidecar is currently a route in this web service, not a separate Railway service. No cron service, Railway feature flag, or third-party observability integration is configured by this repository.

Required variables:

- `APP_ENV=production`
- `DATABASE_URL`, using the Railway PostgreSQL reference
- `PUBLIC_APP_URL`, set to the canonical Railway HTTPS origin
- `AUTH_PEPPER`, with at least 32 random characters
- `MODEL_BACKEND=ollama`
- `OLLAMA_API_KEY`
- `TOOLKIT_MODEL=glm-5.2`
- `EMAIL_BACKEND=resend`
- `RESEND_API_KEY`
- `MAIL_FROM`, using a verified sending domain

Optional governed product-signal settings:

- `PRODUCT_TELEMETRY_ENABLED=false`
- `TELEMETRY_COHORT=beta`
- `TELEMETRY_MIN_CELL_SIZE=3`

Optional conversation-evaluation settings:

- `TOOLKIT_EVALUATION_ENABLED=false`
- `TOOLKIT_EVALUATION_MIN_INACTIVE_SECONDS=300`

Production startup stops when PostgreSQL, HTTPS, the authentication pepper, or the live model adapter is missing. Registration returns `503` until email delivery is ready. `/health` checks the database connection and reports whether account email is configured.

GitHub Pages remains a stable public entry URL and redirects to the canonical same-origin Railway app. Verification and reset fragments are preserved during that redirect; other fragments and query data are discarded.

## Privacy boundary

The guide asks for categories, policies, practices, and open questions. It does not ask for names, raw participant records, identifying details, confidential text, or uploads. Organization membership controls access to each saved review. Product signals are disabled by default, require explicit opt-in, reject free text, and remain distinct from fieldwork evidence.
