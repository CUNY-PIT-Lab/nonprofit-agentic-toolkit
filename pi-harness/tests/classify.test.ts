// Deterministic tests for the governance kernel. No API key, no model — the
// classifier is a pure function, so every verdict is asserted exactly.
// Run: npm test  (tsx --test)
import test from "node:test";
import assert from "node:assert/strict";
import { classify, type Verdict } from "../src/classify.ts";

// ── The core matrix: (message) → verdict ────────────────────────────────────
const CASES: Array<{ name: string; text: string; verdict: Verdict; tier?: string }> = [
  {
    name: "public drafting → allowed",
    text: "Draft a warm thank-you note for volunteers at our public open house.",
    verdict: "allowed",
    tier: "public",
  },
  {
    name: "public summarization → allowed",
    text: "Summarize this public workshop description: we host free intro biology classes on Saturdays.",
    verdict: "allowed",
    tier: "public",
  },
  {
    name: "PII (email + DOB + case #) into external tool → prohibited",
    text: "Draft an email to maria.lopez@example.org; her DOB is 03/04/1990 and case #A12345.",
    verdict: "prohibited",
    tier: "sensitive",
  },
  {
    name: "phone number → prohibited",
    text: "Text John at 212-555-0198 about the workshop schedule.",
    verdict: "prohibited",
    tier: "sensitive",
  },
  {
    name: "password/credential → prohibited",
    text: "Save the wifi password in a note for the front desk.",
    verdict: "prohibited",
    tier: "sensitive",
  },
  {
    name: "internal HR document → restricted",
    text: "Summarize our internal HR staff policy on time off.",
    verdict: "restricted",
    tier: "restricted",
  },
  {
    name: "grant proposal → restricted",
    text: "Help me tighten the budget narrative in our grant proposal draft.",
    verdict: "restricted",
    tier: "restricted",
  },
  {
    name: "de-identified participant data → restricted (unclear classification)",
    text: "Analyze this de-identified participant dataset for attendance trends.",
    verdict: "restricted",
  },
  {
    name: "instructor IP used in external tool → prohibited",
    text: "Use the instructor's curriculum to draft a new handout for the class.",
    verdict: "prohibited",
  },
  {
    name: "fine-tuning on proprietary protocol → prohibited",
    text: "Fine-tune a model on our proprietary lab protocols so it answers like us.",
    verdict: "prohibited",
  },
  {
    name: "ambiguous internal reference → restricted (escalate)",
    text: "Can you take a look at this document and tell me what to fix?",
    verdict: "restricted",
  },
];

for (const c of CASES) {
  test(c.name, () => {
    const r = classify({ text: c.text });
    assert.equal(r.verdict, c.verdict, `${c.name}\n  rationale: ${r.rationale}\n  matched: ${r.matchedRules.join(", ")}`);
    if (c.tier) assert.equal(r.tier, c.tier);
  });
}

// ── The environment axis: sensitive data is prohibited on external tools but
//    only restricted on org-controlled infra (the stage 5 local-hosting path). ──
test("sensitive + internal environment → restricted, not prohibited", () => {
  const external = classify({ text: "email to a.person@example.org", environment: "external" });
  const internal = classify({ text: "email to a.person@example.org", environment: "internal" });
  assert.equal(external.verdict, "prohibited");
  assert.equal(internal.verdict, "restricted");
});

// ── Every result is well-formed. ──
test("result is always well-formed", () => {
  const r = classify({ text: "hello" });
  assert.ok(["public", "restricted", "sensitive"].includes(r.tier));
  assert.ok(["allowed", "restricted", "prohibited"].includes(r.verdict));
  assert.equal(r.environment, "external");
  assert.ok(typeof r.rationale === "string" && r.rationale.length > 0);
  assert.ok(Array.isArray(r.matchedRules));
});

// ── A declared tier (explicit self-assessment) overrides text detection. ──
test("declared sensitive tier forces the sensitive path even on innocuous text", () => {
  const r = classify({ text: "just a friendly note", declared: { tier: "sensitive" } });
  assert.equal(r.verdict, "prohibited"); // sensitive + external
});
