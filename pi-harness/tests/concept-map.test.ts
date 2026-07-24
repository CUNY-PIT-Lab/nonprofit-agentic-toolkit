import test from "node:test";
import assert from "node:assert/strict";
import {
  buildConceptMap,
  type SynthesisMapInput,
} from "../src/concept-map.ts";

const COMPLETE_INPUT: SynthesisMapInput = {
  organization: "Demo community center",
  title: "AI review synthesis",
  summary: "A bounded public information guide is under review.",
  items: [
    {
      id: "staff-capacity",
      kind: "context",
      label: "Staff capacity is limited",
      sourceSteps: [1, 3],
    },
    {
      id: "privacy-boundary",
      kind: "constraint",
      label: "Participant records stay out of external tools",
      sourceSteps: [2],
      relatedItemIds: ["staff-capacity"],
    },
    {
      id: "trusted-review",
      kind: "affordance",
      label: "Program staff already review public guidance",
      sourceSteps: [4],
    },
    {
      id: "current-site",
      kind: "infrastructure",
      label: "The public website is the source of service information",
      sourceSteps: [1],
    },
    {
      id: "public-guide",
      kind: "target_use",
      label: "Source-bounded public information guide",
      usePattern: "public_information_guide",
      sourceSteps: [1, 4],
    },
    {
      id: "pilot-approval",
      kind: "decision",
      label: "Will program and data owners approve a public-content pilot?",
      status: "open",
      sourceSteps: [2, 6],
      annotations: [
        {
          id: "governance-note",
          body: "Confirm the review owner before a pilot starts.",
          authorId: "user-17",
        },
      ],
    },
    {
      id: "bounded-pilot",
      kind: "pathway",
      label: "Run a source-bounded pilot with staff review",
      status: "favored",
      sourceSteps: [5, 6],
    },
    {
      id: "service-discovery",
      kind: "potential",
      label: "People can find current services more easily",
      sourceSteps: [3],
    },
    {
      id: "thirty-day-review",
      kind: "road_ahead",
      label: "Review corrections and search gaps after 30 days",
      horizon: "30 days after launch",
      owner: "Program and data owners",
      sourceSteps: [6, 7],
    },
  ],
  relationships: [
    {
      sourceId: "privacy-boundary",
      targetId: "pilot-approval",
      kind: "constrains",
      label: "sets a data boundary for",
    },
    {
      sourceId: "current-site",
      targetId: "public-guide",
      kind: "informs",
      label: "supplies approved sources for",
    },
    {
      sourceId: "pilot-approval",
      targetId: "bounded-pilot",
      kind: "opens",
      label: "opens after approval",
    },
    {
      sourceId: "bounded-pilot",
      targetId: "service-discovery",
      kind: "supports",
      label: "could support",
    },
    {
      sourceId: "service-discovery",
      targetId: "thirty-day-review",
      kind: "leads_to",
      label: "is checked at",
    },
  ],
  snippets: [
    {
      id: "entry-use",
      sourceStep: 1,
      label: "Proposed use",
      summary: "The organization is considering a guide over approved public content.",
      responseId: "response-1",
      relatedItemIds: ["current-site", "public-guide"],
    },
    {
      id: "final-review",
      sourceStep: 7,
      label: "Synthesis review",
      summary: "A pilot remains conditional on program and data-owner approval.",
      relatedItemIds: ["pilot-approval", "bounded-pilot"],
    },
  ],
};

test("identical synthesis input produces an identical map", () => {
  const first = buildConceptMap(COMPLETE_INPUT);
  const second = buildConceptMap(COMPLETE_INPUT);
  assert.deepEqual(second, first);
  assert.match(first.mapId, /^map-demo-community-center-/);
});

test("output is a compound Cytoscape.js graph with all five sections", () => {
  const map = buildConceptMap(COMPLETE_INPUT);
  assert.equal(map.renderer.library, "cytoscape.js");
  assert.equal(map.renderer.layout.preferred, "fcose");
  assert.equal(map.renderer.layout.fallback, "cose");
  assert.equal(map.sections.length, 5);
  assert.deepEqual(map.coverage.missingSections, []);

  const publicGuide = map.elements.nodes.find(
    (node) => node.data.originalId === "public-guide",
  );
  assert.equal(publicGuide?.data.parent, "section-current-conditions");
  assert.equal(publicGuide?.data.usePattern, "public_information_guide");
});

test("important conditions, decisions, pathways, potentials, and next actions stay connected", () => {
  const map = buildConceptMap(COMPLETE_INPUT);
  const byOriginalId = new Map(
    map.elements.nodes
      .filter((node) => node.data.originalId)
      .map((node) => [node.data.originalId!, node.data.id]),
  );
  const expected = [
    ["privacy-boundary", "pilot-approval", "constrains"],
    ["pilot-approval", "bounded-pilot", "opens"],
    ["bounded-pilot", "service-discovery", "supports"],
    ["service-discovery", "thirty-day-review", "leads_to"],
  ];
  for (const [source, target, kind] of expected) {
    assert.ok(
      map.elements.edges.some(
        (edge) =>
          edge.data.source === byOriginalId.get(source) &&
          edge.data.target === byOriginalId.get(target) &&
          edge.data.kind === kind,
      ),
      `${source} should connect to ${target}`,
    );
  }
});

test("unconnected items receive deterministic fallback edges", () => {
  const map = buildConceptMap({
    organization: "Fallback org",
    items: [
      { id: "decision", kind: "decision", label: "Approve a pilot?" },
      { id: "path", kind: "pathway", label: "Run a bounded pilot" },
      { id: "later", kind: "road_ahead", label: "Review the pilot" },
    ],
  });
  const itemId = (originalId: string) =>
    map.elements.nodes.find((node) => node.data.originalId === originalId)!.data.id;
  assert.ok(
    map.elements.edges.some(
      (edge) =>
        edge.data.source === itemId("decision") &&
        edge.data.target === itemId("path"),
    ),
  );
  assert.ok(
    map.elements.edges.some(
      (edge) =>
        edge.data.source === itemId("path") &&
        edge.data.target === itemId("later"),
    ),
  );
});

test("knowledge snippets and annotations use stable graph references", () => {
  const map = buildConceptMap(COMPLETE_INPUT);
  const decision = map.elements.nodes.find(
    (node) => node.data.originalId === "pilot-approval",
  )!;
  assert.deepEqual(decision.data.annotationIds, ["annotation-governance-note"]);
  assert.equal(map.annotations[0].targetId, decision.data.id);
  assert.equal(map.annotations[0].authorId, "user-17");

  const snippet = map.snippets.find((entry) => entry.id === "snippet-final-review")!;
  assert.equal(snippet.sourceStep, 7);
  assert.ok(snippet.relatedNodeIds.includes(decision.data.id));
});

test("the builder does not mutate its input", () => {
  const input = structuredClone(COMPLETE_INPUT);
  const before = structuredClone(input);
  buildConceptMap(input);
  assert.deepEqual(input, before);
});

test("missing relationship and snippet references are reported and skipped", () => {
  const map = buildConceptMap({
    organization: "Reference check",
    items: [{ id: "known", kind: "decision", label: "Known decision" }],
    relationships: [
      { sourceId: "missing", targetId: "known", kind: "informs" },
    ],
    snippets: [
      {
        sourceStep: 7,
        label: "Review",
        summary: "A decision remains open.",
        relatedItemIds: ["missing"],
      },
    ],
  });
  assert.ok(map.warnings.some((warning) => warning.includes('"missing" was not found')));
  assert.ok(map.warnings.some((warning) => warning.includes('missing item "missing"')));
  assert.equal(map.snippets[0].relatedNodeIds.length, 0);
});

test("duplicate item ids are rejected before graph construction", () => {
  assert.throws(
    () =>
      buildConceptMap({
        organization: "Duplicate check",
        items: [
          { id: "same", kind: "context", label: "One" },
          { id: "same", kind: "decision", label: "Two" },
        ],
      }),
    /duplicate synthesis item id: same/,
  );
});

test("an empty synthesis cannot produce a map", () => {
  assert.throws(
    () => buildConceptMap({ organization: "Empty org", items: [] }),
    /items must contain at least one synthesis item/,
  );
});
