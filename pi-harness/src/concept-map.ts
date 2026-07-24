// concept-map.ts — a portable synthesis-to-graph kernel.
//
// This module imports nothing from Pi or a rendering library. It turns a
// structured synthesis into a deterministic, Cytoscape.js-compatible graph.
// The same function can run in Pi, the Python-backed web app through a small
// adapter, or a later service without changing the map logic.

export const MAP_ITEM_KINDS = [
  "current_condition",
  "context",
  "constraint",
  "affordance",
  "infrastructure",
  "target_use",
  "decision",
  "pathway",
  "potential",
  "road_ahead",
] as const;

export const TARGET_USE_PATTERNS = [
  "workflow",
  "knowledge_access",
  "general_chatbot",
  "public_information_guide",
  "other",
] as const;

export const MAP_EDGE_KINDS = [
  "describes",
  "constrains",
  "enables",
  "uses",
  "raises",
  "opens",
  "supports",
  "leads_to",
  "informs",
  "depends_on",
  "relates_to",
] as const;

export const MAP_ITEM_STATUSES = [
  "observed",
  "open",
  "favored",
  "held",
  "approved",
  "declined",
] as const;

export type MapItemKind = (typeof MAP_ITEM_KINDS)[number];
export type TargetUsePattern = (typeof TARGET_USE_PATTERNS)[number];
export type MapEdgeKind = (typeof MAP_EDGE_KINDS)[number];
export type MapItemStatus = (typeof MAP_ITEM_STATUSES)[number];

export type MapSectionId =
  | "current_conditions"
  | "decision_points"
  | "pathways"
  | "potentials"
  | "road_ahead";

export interface SynthesisAnnotationInput {
  id?: string;
  body: string;
  authorId?: string;
  createdAt?: string;
}

export interface SynthesisMapItem {
  /** A stable, local reference such as "privacy-boundary" or "pilot-owner". */
  id: string;
  kind: MapItemKind;
  label: string;
  detail?: string;
  sourceSteps?: number[];
  status?: MapItemStatus;
  owner?: string;
  horizon?: string;
  usePattern?: TargetUsePattern;
  relatedItemIds?: string[];
  annotations?: SynthesisAnnotationInput[];
}

export interface SynthesisRelationship {
  sourceId: string;
  targetId: string;
  kind?: MapEdgeKind;
  label?: string;
}

export interface KnowledgeSnippetInput {
  id?: string;
  sourceStep: number;
  label: string;
  summary: string;
  responseId?: string;
  relatedItemIds?: string[];
}

export interface SynthesisMapInput {
  organization: string;
  title?: string;
  summary?: string;
  mapId?: string;
  items: SynthesisMapItem[];
  relationships?: SynthesisRelationship[];
  snippets?: KnowledgeSnippetInput[];
}

export interface ConceptMapAnnotation {
  id: string;
  targetId: string;
  body: string;
  authorId?: string;
  createdAt?: string;
}

export interface KnowledgeSnippet {
  id: string;
  sourceStep: number;
  label: string;
  summary: string;
  responseId?: string;
  relatedNodeIds: string[];
}

export interface CytoscapeNode {
  group: "nodes";
  data: {
    id: string;
    label: string;
    kind: "organization" | "section" | MapItemKind;
    parent?: string;
    section?: MapSectionId;
    originalId?: string;
    detail?: string;
    sourceSteps?: number[];
    status?: MapItemStatus;
    owner?: string;
    horizon?: string;
    usePattern?: TargetUsePattern;
    annotationIds?: string[];
  };
  classes: string;
}

export interface CytoscapeEdge {
  group: "edges";
  data: {
    id: string;
    source: string;
    target: string;
    kind: MapEdgeKind;
    label: string;
  };
  classes: string;
}

export interface ConceptMap {
  schemaVersion: "1.0";
  mapId: string;
  organization: string;
  title: string;
  summary?: string;
  renderer: {
    library: "cytoscape.js";
    format: "cytoscape-elements";
    layout: {
      preferred: "fcose";
      fallback: "cose";
      compoundNodes: true;
    };
  };
  sections: Array<{ id: MapSectionId; label: string; order: number }>;
  elements: {
    nodes: CytoscapeNode[];
    edges: CytoscapeEdge[];
  };
  snippets: KnowledgeSnippet[];
  annotations: ConceptMapAnnotation[];
  coverage: {
    itemCount: number;
    edgeCount: number;
    snippetCount: number;
    annotationCount: number;
    countsByKind: Record<MapItemKind, number>;
    missingSections: MapSectionId[];
  };
  warnings: string[];
}

const SECTION_DEFINITIONS: Array<{
  id: MapSectionId;
  label: string;
  kinds: MapItemKind[];
}> = [
  {
    id: "current_conditions",
    label: "Current conditions",
    kinds: [
      "current_condition",
      "context",
      "constraint",
      "affordance",
      "infrastructure",
      "target_use",
    ],
  },
  { id: "decision_points", label: "Decision points", kinds: ["decision"] },
  { id: "pathways", label: "Pathways", kinds: ["pathway"] },
  { id: "potentials", label: "Potentials", kinds: ["potential"] },
  { id: "road_ahead", label: "Road ahead", kinds: ["road_ahead"] },
];

const SECTION_BY_KIND = new Map<MapItemKind, MapSectionId>(
  SECTION_DEFINITIONS.flatMap((section) =>
    section.kinds.map((kind) => [kind, section.id] as const),
  ),
);

const DEFAULT_EDGE: Record<
  MapItemKind,
  { kind: MapEdgeKind; label: string }
> = {
  current_condition: { kind: "describes", label: "describes" },
  context: { kind: "describes", label: "sets context" },
  constraint: { kind: "constrains", label: "constrains" },
  affordance: { kind: "enables", label: "enables" },
  infrastructure: { kind: "uses", label: "already uses" },
  target_use: { kind: "uses", label: "targets" },
  decision: { kind: "raises", label: "raises" },
  pathway: { kind: "opens", label: "opens" },
  potential: { kind: "supports", label: "could support" },
  road_ahead: { kind: "leads_to", label: "leads to" },
};

function cleanText(value: unknown): string {
  return String(value ?? "").replace(/\s+/g, " ").trim();
}

function requireText(value: unknown, field: string): string {
  const text = cleanText(value);
  if (!text) throw new Error(`${field} is required`);
  return text;
}

function slug(value: string, fallback: string): string {
  const normalized = value
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
  return normalized || fallback;
}

function stableValue(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value) ?? "null";
  if (Array.isArray(value)) return `[${value.map(stableValue).join(",")}]`;
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record)
    .filter((key) => record[key] !== undefined)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${stableValue(record[key])}`)
    .join(",")}}`;
}

function shortHash(value: unknown): string {
  const input = stableValue(value);
  let hash = 0x811c9dc5;
  for (let index = 0; index < input.length; index += 1) {
    hash ^= input.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(36).padStart(7, "0");
}

function allocateId(base: string, used: Set<string>): string {
  if (!used.has(base)) {
    used.add(base);
    return base;
  }
  let suffix = 2;
  while (used.has(`${base}-${suffix}`)) suffix += 1;
  const id = `${base}-${suffix}`;
  used.add(id);
  return id;
}

function uniquePositiveSteps(steps: number[] | undefined): number[] | undefined {
  if (!steps?.length) return undefined;
  const normalized = [...new Set(steps.filter((step) => Number.isInteger(step) && step > 0))];
  normalized.sort((a, b) => a - b);
  return normalized.length ? normalized : undefined;
}

function sectionFor(kind: MapItemKind): MapSectionId {
  const section = SECTION_BY_KIND.get(kind);
  if (!section) throw new Error(`unsupported map item kind: ${kind}`);
  return section;
}

/**
 * Build a stable graph from model- or staff-produced synthesis data.
 *
 * The function performs no I/O and does not call a model. Identical input yields
 * identical ids, nodes, edges, snippets, annotations, and warnings.
 */
export function buildConceptMap(input: SynthesisMapInput): ConceptMap {
  const organization = requireText(input?.organization, "organization");
  if (!Array.isArray(input?.items) || input.items.length === 0) {
    throw new Error("items must contain at least one synthesis item");
  }

  const normalizedInput = {
    organization,
    title: cleanText(input.title),
    summary: cleanText(input.summary),
    mapId: cleanText(input.mapId),
    items: input.items,
    relationships: input.relationships ?? [],
    snippets: input.snippets ?? [],
  };
  const mapId = normalizedInput.mapId
    ? `map-${slug(normalizedInput.mapId, shortHash(normalizedInput))}`
    : `map-${slug(organization, "organization")}-${shortHash(normalizedInput)}`;
  const title = normalizedInput.title || `${organization} synthesis map`;
  const usedNodeIds = new Set<string>();
  const usedEdgeIds = new Set<string>();
  const usedAnnotationIds = new Set<string>();
  const usedSnippetIds = new Set<string>();
  const inputIds = new Set<string>();
  const referenceToNode = new Map<string, string>();
  const warnings: string[] = [];
  const annotations: ConceptMapAnnotation[] = [];

  const rootId = allocateId(
    `organization-${slug(organization, "organization")}`,
    usedNodeIds,
  );
  const nodes: CytoscapeNode[] = [
    {
      group: "nodes",
      data: {
        id: rootId,
        label: organization,
        kind: "organization",
        detail: normalizedInput.summary || undefined,
      },
      classes: "map-node organization",
    },
  ];

  for (const section of SECTION_DEFINITIONS) {
    const id = `section-${section.id.replaceAll("_", "-")}`;
    usedNodeIds.add(id);
    nodes.push({
      group: "nodes",
      data: {
        id,
        label: section.label,
        kind: "section",
        section: section.id,
      },
      classes: `map-section section-${section.id.replaceAll("_", "-")}`,
    });
  }

  const itemNodeByKind = new Map<MapItemKind, string[]>();

  for (const [index, item] of input.items.entries()) {
    const originalId = requireText(item.id, `items[${index}].id`);
    if (inputIds.has(originalId)) {
      throw new Error(`duplicate synthesis item id: ${originalId}`);
    }
    inputIds.add(originalId);

    const kind = item.kind;
    if (!MAP_ITEM_KINDS.includes(kind)) {
      throw new Error(`items[${index}].kind is unsupported: ${String(kind)}`);
    }
    if (item.usePattern && !TARGET_USE_PATTERNS.includes(item.usePattern)) {
      throw new Error(`items[${index}].usePattern is unsupported: ${item.usePattern}`);
    }
    if (item.usePattern && kind !== "target_use") {
      warnings.push(`Use pattern on "${originalId}" was kept, though its kind is not target_use.`);
    }

    const section = sectionFor(kind);
    const nodeId = allocateId(
      `item-${slug(originalId, `item-${index + 1}`)}`,
      usedNodeIds,
    );
    referenceToNode.set(originalId, nodeId);
    referenceToNode.set(nodeId, nodeId);
    const annotationIds: string[] = [];

    for (const [annotationIndex, annotation] of (item.annotations ?? []).entries()) {
      const body = requireText(
        annotation.body,
        `items[${index}].annotations[${annotationIndex}].body`,
      );
      const requestedId = cleanText(annotation.id);
      const annotationId = allocateId(
        `annotation-${slug(
          requestedId || `${originalId}-${shortHash(body)}`,
          `${index + 1}-${annotationIndex + 1}`,
        )}`,
        usedAnnotationIds,
      );
      annotationIds.push(annotationId);
      annotations.push({
        id: annotationId,
        targetId: nodeId,
        body,
        authorId: cleanText(annotation.authorId) || undefined,
        createdAt: cleanText(annotation.createdAt) || undefined,
      });
    }

    nodes.push({
      group: "nodes",
      data: {
        id: nodeId,
        label: requireText(item.label, `items[${index}].label`),
        kind,
        parent: `section-${section.replaceAll("_", "-")}`,
        section,
        originalId,
        detail: cleanText(item.detail) || undefined,
        sourceSteps: uniquePositiveSteps(item.sourceSteps),
        status: item.status,
        owner: cleanText(item.owner) || undefined,
        horizon: cleanText(item.horizon) || undefined,
        usePattern: item.usePattern,
        annotationIds: annotationIds.length ? annotationIds : undefined,
      },
      classes: `map-node kind-${kind.replaceAll("_", "-")}${
        item.status ? ` status-${item.status}` : ""
      }`,
    });
    itemNodeByKind.set(kind, [...(itemNodeByKind.get(kind) ?? []), nodeId]);
  }

  const resolveReference = (reference: string): string | undefined => {
    const cleaned = cleanText(reference);
    return referenceToNode.get(cleaned);
  };

  const edges: CytoscapeEdge[] = [];
  const edgeKeys = new Set<string>();

  const addEdge = (
    source: string,
    target: string,
    kind: MapEdgeKind,
    label: string,
  ): void => {
    if (source === target) {
      warnings.push(`Self-reference on "${source}" was skipped.`);
      return;
    }
    const key = `${source}\u0000${target}\u0000${kind}\u0000${label}`;
    if (edgeKeys.has(key)) return;
    edgeKeys.add(key);
    edges.push({
      group: "edges",
      data: {
        id: allocateId(`edge-${shortHash(key)}`, usedEdgeIds),
        source,
        target,
        kind,
        label,
      },
      classes: `map-edge edge-${kind.replaceAll("_", "-")}`,
    });
  };

  for (const item of input.items) {
    const target = resolveReference(item.id)!;
    for (const relatedId of item.relatedItemIds ?? []) {
      const source = resolveReference(relatedId);
      if (!source) {
        warnings.push(`Related item "${relatedId}" referenced by "${item.id}" was not found.`);
        continue;
      }
      addEdge(source, target, "informs", "informs");
    }
  }

  for (const [index, relationship] of (input.relationships ?? []).entries()) {
    const source = resolveReference(relationship.sourceId);
    const target = resolveReference(relationship.targetId);
    if (!source || !target) {
      warnings.push(
        `Relationship ${index + 1} was skipped because "${
          !source ? relationship.sourceId : relationship.targetId
        }" was not found.`,
      );
      continue;
    }
    const kind = relationship.kind ?? "relates_to";
    if (!MAP_EDGE_KINDS.includes(kind)) {
      throw new Error(`relationships[${index}].kind is unsupported: ${String(kind)}`);
    }
    addEdge(source, target, kind, cleanText(relationship.label) || kind.replaceAll("_", " "));
  }

  const itemNodes = nodes.filter(
    (node) => node.data.kind !== "organization" && node.data.kind !== "section",
  ) as Array<CytoscapeNode & { data: CytoscapeNode["data"] & { kind: MapItemKind } }>;
  const hasIncoming = (nodeId: string): boolean =>
    edges.some((edge) => edge.data.target === nodeId);
  const firstNode = (...kinds: MapItemKind[]): string | undefined => {
    for (const kind of kinds) {
      const nodeId = itemNodeByKind.get(kind)?.[0];
      if (nodeId) return nodeId;
    }
    return undefined;
  };

  let priorRoadAhead: string | undefined;
  for (const node of itemNodes) {
    if (hasIncoming(node.data.id)) {
      if (node.data.kind === "road_ahead") priorRoadAhead = node.data.id;
      continue;
    }

    const fallback = DEFAULT_EDGE[node.data.kind];
    let source = rootId;
    if (node.data.kind === "pathway") {
      source = firstNode("decision") ?? rootId;
    } else if (node.data.kind === "potential") {
      source = firstNode("pathway", "decision") ?? rootId;
    } else if (node.data.kind === "road_ahead") {
      source =
        priorRoadAhead ??
        firstNode("potential", "pathway", "decision") ??
        rootId;
      priorRoadAhead = node.data.id;
    }
    addEdge(source, node.data.id, fallback.kind, fallback.label);
  }

  const snippets: KnowledgeSnippet[] = [];
  for (const [index, snippet] of (input.snippets ?? []).entries()) {
    if (!Number.isInteger(snippet.sourceStep) || snippet.sourceStep < 1) {
      throw new Error(`snippets[${index}].sourceStep must be a positive integer`);
    }
    const label = requireText(snippet.label, `snippets[${index}].label`);
    const summary = requireText(snippet.summary, `snippets[${index}].summary`);
    const requestedId = cleanText(snippet.id);
    const id = allocateId(
      `snippet-${slug(
        requestedId || `step-${snippet.sourceStep}-${shortHash({ label, summary })}`,
        `${index + 1}`,
      )}`,
      usedSnippetIds,
    );
    const relatedNodeIds: string[] = [];
    for (const relatedId of snippet.relatedItemIds ?? []) {
      const nodeId = resolveReference(relatedId);
      if (!nodeId) {
        warnings.push(`Snippet "${id}" references missing item "${relatedId}".`);
        continue;
      }
      if (!relatedNodeIds.includes(nodeId)) relatedNodeIds.push(nodeId);
    }
    snippets.push({
      id,
      sourceStep: snippet.sourceStep,
      label,
      summary,
      responseId: cleanText(snippet.responseId) || undefined,
      relatedNodeIds,
    });
  }

  const countsByKind = Object.fromEntries(
    MAP_ITEM_KINDS.map((kind) => [kind, itemNodeByKind.get(kind)?.length ?? 0]),
  ) as Record<MapItemKind, number>;
  const missingSections = SECTION_DEFINITIONS.filter((section) =>
    section.kinds.every((kind) => countsByKind[kind] === 0),
  ).map((section) => section.id);
  if (missingSections.length) {
    warnings.push(
      `No items were supplied for: ${missingSections
        .map((id) => SECTION_DEFINITIONS.find((section) => section.id === id)!.label)
        .join(", ")}.`,
    );
  }

  return {
    schemaVersion: "1.0",
    mapId,
    organization,
    title,
    summary: normalizedInput.summary || undefined,
    renderer: {
      library: "cytoscape.js",
      format: "cytoscape-elements",
      layout: {
        preferred: "fcose",
        fallback: "cose",
        compoundNodes: true,
      },
    },
    sections: SECTION_DEFINITIONS.map((section, index) => ({
      id: section.id,
      label: section.label,
      order: index + 1,
    })),
    elements: { nodes, edges },
    snippets,
    annotations,
    coverage: {
      itemCount: input.items.length,
      edgeCount: edges.length,
      snippetCount: snippets.length,
      annotationCount: annotations.length,
      countsByKind,
      missingSections,
    },
    warnings,
  };
}
