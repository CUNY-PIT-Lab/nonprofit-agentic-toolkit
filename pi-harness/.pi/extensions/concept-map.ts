// concept-map.ts — Pi adapter for the portable synthesis map builder.
//
// Pi supplies the tool call. src/concept-map.ts owns validation, stable ids,
// graph structure, snippets, and annotation records.
import { Type } from "typebox";
import { StringEnum } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import {
  MAP_EDGE_KINDS,
  MAP_ITEM_KINDS,
  MAP_ITEM_STATUSES,
  TARGET_USE_PATTERNS,
  buildConceptMap,
} from "../../src/concept-map.ts";

const annotationSchema = Type.Object({
  id: Type.Optional(Type.String()),
  body: Type.String(),
  authorId: Type.Optional(Type.String()),
  createdAt: Type.Optional(Type.String()),
});

const itemSchema = Type.Object({
  id: Type.String(),
  kind: StringEnum(MAP_ITEM_KINDS),
  label: Type.String(),
  detail: Type.Optional(Type.String()),
  sourceSteps: Type.Optional(Type.Array(Type.Integer({ minimum: 1 }))),
  status: Type.Optional(StringEnum(MAP_ITEM_STATUSES)),
  owner: Type.Optional(Type.String()),
  horizon: Type.Optional(Type.String()),
  usePattern: Type.Optional(StringEnum(TARGET_USE_PATTERNS)),
  relatedItemIds: Type.Optional(Type.Array(Type.String())),
  annotations: Type.Optional(Type.Array(annotationSchema)),
});

const relationshipSchema = Type.Object({
  sourceId: Type.String(),
  targetId: Type.String(),
  kind: Type.Optional(StringEnum(MAP_EDGE_KINDS)),
  label: Type.Optional(Type.String()),
});

const snippetSchema = Type.Object({
  id: Type.Optional(Type.String()),
  sourceStep: Type.Integer({ minimum: 1 }),
  label: Type.String(),
  summary: Type.String(),
  responseId: Type.Optional(Type.String()),
  relatedItemIds: Type.Optional(Type.Array(Type.String())),
});

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "build_synthesis_concept_map",
    label: "Build synthesis concept map",
    description:
      "Turn a completed seven-step toolkit synthesis into a deterministic, annotatable Cytoscape.js graph of current conditions, decision points, pathways, potentials, and the road ahead.",
    promptSnippet:
      "Build an annotatable Cytoscape.js concept map from a completed toolkit synthesis",
    promptGuidelines: [
      "Use build_synthesis_concept_map only after the organization has completed the review and you have synthesized its responses.",
      "Give build_synthesis_concept_map concise summaries instead of raw participant, client, staff, or donor records.",
      "When calling build_synthesis_concept_map, connect items with stable ids and include one knowledge snippet for each answered toolkit step.",
    ],
    parameters: Type.Object({
      organization: Type.String(),
      title: Type.Optional(Type.String()),
      summary: Type.Optional(Type.String()),
      mapId: Type.Optional(Type.String()),
      items: Type.Array(itemSchema, { minItems: 1 }),
      relationships: Type.Optional(Type.Array(relationshipSchema)),
      snippets: Type.Optional(Type.Array(snippetSchema)),
    }),
    async execute(_toolCallId, params) {
      const map = buildConceptMap(params);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(map),
          },
        ],
        details: {
          map,
          warningCount: map.warnings.length,
        },
      };
    },
  });
}
