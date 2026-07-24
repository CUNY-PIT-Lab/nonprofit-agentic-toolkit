import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(
  new URL("../.pi/extensions/concept-map.ts", import.meta.url),
  "utf8",
);
const skill = readFileSync(
  new URL("../.pi/skills/synthesis-concept-map/SKILL.md", import.meta.url),
  "utf8",
);

test("Pi registers the synthesis concept-map tool", () => {
  assert.match(source, /pi\.registerTool\(\{/);
  assert.match(source, /name:\s*"build_synthesis_concept_map"/);
  assert.match(source, /buildConceptMap\(params\)/);
});

test("the tool accepts every required synthesis category", () => {
  for (const name of [
    "MAP_ITEM_KINDS",
    "MAP_EDGE_KINDS",
    "MAP_ITEM_STATUSES",
    "TARGET_USE_PATTERNS",
  ]) {
    assert.match(source, new RegExp(`\\b${name}\\b`));
  }
  assert.match(source, /items:\s*Type\.Array\(itemSchema,\s*\{\s*minItems:\s*1\s*\}\)/);
  assert.match(source, /snippets:\s*Type\.Optional\(Type\.Array\(snippetSchema\)\)/);
});

test("the tool returns the portable map in text and structured details", () => {
  assert.match(source, /text:\s*JSON\.stringify\(map\)/);
  assert.match(source, /details:\s*\{\s*map,/);
  assert.match(source, /warningCount:\s*map\.warnings\.length/);
});

test("Pi discovers a matching synthesis-map skill", () => {
  assert.match(skill, /^---\nname: synthesis-concept-map\n/m);
  assert.match(skill, /allowed-tools: build_synthesis_concept_map/);
  assert.match(skill, /Call `build_synthesis_concept_map` once/);
  assert.match(skill, /one short knowledge snippet for each answered step/i);
});
