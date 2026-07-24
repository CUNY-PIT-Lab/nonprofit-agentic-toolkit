---
name: synthesis-concept-map
description: Analyze a completed Non-Profit AI Toolkit review and turn its seven steps into an annotatable concept map. Use when the user asks for synthesis, a map of decisions and pathways, or a visual account of current conditions and the road ahead.
allowed-tools: build_synthesis_concept_map
---

# Synthesis concept map

Use the full toolkit record. Include the entry response, the six tests, and any
conditions or ownership decisions added during review.

## Prepare the synthesis

1. Write one short knowledge snippet for each answered step. Translate the
   response into a useful summary. Keep the step number and a stable response id
   when one is available.
2. Keep names, raw records, identifying details, credentials, and confidential
   text out of snippets and map labels. Record data categories and governance
   conditions instead.
3. Separate the analysis into these item kinds:
   - `context`: mission, people affected, current practice, timing, and capacity.
   - `constraint`: privacy, consent, policy, contract, labor, access, or resource
     limits.
   - `affordance`: useful capacity, existing staff knowledge, trusted process, or
     available support.
   - `infrastructure`: AI features, software, data systems, websites, search, or
     governance practices already in use.
   - `target_use`: the proposed use, with one `usePattern`.
   - `decision`: a choice that staff or a named governance group still controls.
   - `pathway`: a viable course of action and its conditions.
   - `potential`: a benefit or new capacity that a pathway could support.
   - `road_ahead`: an owned next action, review point, or later decision.
4. Use `current_condition` only when a current fact does not fit the more
   specific kinds above.

## Classify each target use

Choose one value:

- `workflow` for drafting, summarizing, analysis, translation, or staff process.
- `knowledge_access` for legibility, interpretability, search, and discovery
  across organization-held knowledge.
- `general_chatbot` for an open-ended assistant.
- `public_information_guide` for a source-bounded guide or informational sidecar
  on a public website.
- `other` when the use has been specified but does not fit those four.

Treat separate uses as separate `target_use` items.

## Connect the map

- Give every item a short, stable id.
- Add `sourceSteps` to show where each point came from.
- Use `relatedItemIds` when several conditions inform one decision.
- Add explicit relationships for important causal or conditional links.
- Connect constraints to the decisions they shape, decisions to viable
  pathways, pathways to potentials, and potentials to owned next actions.
- State uncertainty in the label or detail. Do not invent an owner, approval,
  benefit, or route.

## Build the graph

Call `build_synthesis_concept_map` once with the complete synthesis. The tool
returns stable Cytoscape.js elements, compound sections, knowledge snippets, and
annotation records. Its preferred layout is fCoSE, with the built-in CoSE layout
as a fallback.

Review the returned warnings. Explain any missing section briefly. Preserve the
map id, node ids, snippet ids, and annotation target ids when the map is saved so
later edits remain attached to the same records.
