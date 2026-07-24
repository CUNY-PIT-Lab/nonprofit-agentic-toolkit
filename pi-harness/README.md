# Non-Profit AI Toolkit — the governed agent (GLM-5.2 on Pi)

The toolkit's **governed agent**: a live GLM-5.2 assistant running in
[Pi](https://pi.dev) (Mario Zechner's minimal, MIT-licensed harness), with
**Boryana Ivanova's Genspace decision logic** as a **pre-flight guardrail**. It is
the counterpart to the stdlib prototype in `../toolkit-app/` — that one *soft-warns*
about sensitive data; this one *blocks* a prohibited request before the model is
ever called.

Building it on Pi does two jobs at once: it delivers the guardrail, and it
discharges the executive summary's **N6 vendor-neutrality check** — the same
decision logic runs unchanged in a second harness. The decision logic itself
(`src/classify.ts`) imports nothing from Pi, so it can back a Hermes fork or the
stdlib server next.

## What it does

Every message is classified — **data tier × use type × tool environment →
allowed / restricted / prohibited** — before it reaches GLM-5.2:

| Verdict | What happens |
|---|---|
| **prohibited** | Blocked. The model is never called. (e.g. PII → an external tool) |
| **restricted** | You must confirm; on approval the answer is framed as a reviewable draft |
| **allowed** | Passes through; the answer ends with a verify-with-your-team line |

The rules are transcribed from Boryana's capstone in
[`policy/genspace-decision-logic.md`](policy/genspace-decision-logic.md) and
implemented in [`src/classify.ts`](src/classify.ts). The Pi wiring lives in
[`.pi/extensions/`](.pi/extensions/) (`provider.ts` registers GLM-5.2 on Ollama
Cloud; `guardrail.ts` is the pre-flight check).

## Synthesis concept maps

The seventh toolkit step can turn the completed review into one connected map.
[`src/concept-map.ts`](src/concept-map.ts) converts structured synthesis data
into stable nodes, edges, knowledge snippets, and annotation records. It covers
current conditions, decision points, pathways, potentials, and the road ahead.
The function is deterministic and imports nothing from Pi.

Pi exposes that function through
[`build_synthesis_concept_map`](.pi/extensions/concept-map.ts). The matching
[`synthesis-concept-map`](.pi/skills/synthesis-concept-map/SKILL.md) skill tells
the agent how to analyze context, constraints, affordances, existing
infrastructure, target uses, and each review response. Invoke it directly with
`/skill:synthesis-concept-map`.

The returned elements use
[Cytoscape.js](https://js.cytoscape.org/), an MIT-licensed, JSON-serializable
graph library with desktop and touch interaction. The map recommends fCoSE for
compound layout and keeps CoSE as a built-in fallback. Stable map, node, snippet,
and annotation ids give a later database adapter durable records to attach to.

## Run it

```bash
export OLLAMA_API_KEY=...     # your ollama cloud key — env only, never on disk
./run.sh                      # launches pi with the guardrail + glm-5.2
```

`run.sh` loads the provider, guardrail, and synthesis-map tool automatically
(Pi auto-discovers `.pi/extensions/`), sets the `SYSTEM.md` persona, and turns
Pi's built-in coding tools **off** (`--no-builtin-tools`) — this is a chat-only
staff agent, not a coding agent, so it never touches the filesystem. Remove that
flag to re-enable them.

Try it:
- *allowed* — "summarize this public workshop blurb: …" → answers, ends with a verify line.
- *prohibited* — "draft an email to a client; her DOB is … and case #…" → **blocked**, with the policy reason.
- *restricted* — "summarize our internal HR staff policy" → asks you to confirm first.

Or run all three at once, non-interactively:

```bash
./run.sh demo        # runs the three prompts through pi -p, each labelled with its verdict
```

Demo mode prints each prompt's guardrail verdict (from the same `classify()` pi
uses — print mode otherwise shows a block as silence) and then pi's actual
output: a real completion for the allowed prompt, nothing for the blocked ones.

Key is env-only: `run.sh` prompts for it hidden if unset; `provider.ts` reads
`$OLLAMA_API_KEY` at request time; it is never written to disk.

## Test it

```bash
./run.sh test        # or: npm install && npm test
```

The classifier and concept-map builder are pure functions, so their results are
asserted **exactly** — offline, no key, no model. 34 tests cover the governance
matrix, Pi guardrail wiring, stable graph construction, connections, snippets,
annotations, tool registration, and skill discovery.

Verified end-to-end: a prohibited prompt is blocked before any model call; an
allowed prompt passes the guardrail and reaches `ollama.com/v1` (returns `401`
without a key, a completion with one).

## Layout

```
toolkit-agent-pi/
├── src/
│   ├── classify.ts                  # governance kernel — pure, harness-independent
│   └── concept-map.ts               # synthesis graph — pure, harness-independent
├── .pi/extensions/
│   ├── provider.ts                  # registers glm-5.2 on ollama cloud
│   ├── guardrail.ts                 # pre-flight check (input → block / escalate)
│   └── concept-map.ts               # build_synthesis_concept_map tool
├── .pi/skills/
│   └── synthesis-concept-map/
│       └── SKILL.md                 # seven-step synthesis workflow
├── SYSTEM.md                        # the agent persona (org-agnostic, plain, trauma-informed)
├── policy/genspace-decision-logic.md# auditable source of the matrix (capstone §4/§5/§7)
├── tests/                           # deterministic governance + concept-map tests
├── run.sh                           # launcher (./run.sh, ./run.sh test)
└── package.json
```

## Requirements

Node 18+ and Pi (`npm i -g @earendil-works/pi-coding-agent`; `pi` was already on
PATH during development at v0.74.0). An Ollama Cloud key for live runs;
[ollama.com](https://ollama.com).
