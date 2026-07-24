// classify.ts — the governance kernel for the Non-Profit AI Toolkit.
//
// A pure, deterministic implementation of Boryana Ivanova's Genspace AI Decision
// Support Tool (her "AI for What?!?" capstone). It reduces her three-input
// assessment — data tier × use type × tool environment — to a single verdict:
// allowed / restricted / prohibited, with a human-readable rationale.
//
// This module imports nothing from Pi. That is deliberate: the same function
// backs the Pi guardrail extension today and can back server.py or a Hermes
// fork tomorrow. Harness-independence IS the N6 (vendor-neutral) point.
//
// Source of every rule below: Capstone_Genspace AI Policy — §4 (data
// classification), §5 (Allowed / Restricted / Prohibited use), §7 (operational
// rules). Section references are cited inline so the logic stays auditable.

export type Tier = "public" | "restricted" | "sensitive";
export type Environment = "external" | "internal";
export type Verdict = "allowed" | "restricted" | "prohibited";
export type UseType =
  | "drafting"
  | "summarization"
  | "brainstorming"
  | "data_analysis"
  | "image_generation"
  | "meeting_notes"
  | "other";

export interface ClassifyInput {
  text: string;
  /** Optional explicit declaration (Boryana's tool asks the user directly). */
  declared?: { tier?: Tier; useType?: UseType; environment?: Environment };
  /**
   * The tool environment. Defaults to "external": GLM-5.2 on Ollama Cloud is an
   * external system in the capstone's sense (data leaves org-controlled infra),
   * so the §7 rule "do not input Sensitive data into external AI tools" applies.
   * Stage 5 (local hosting) flips this to "internal" — see README.
   */
  environment?: Environment;
}

export interface Classification {
  tier: Tier;
  useType: UseType;
  environment: Environment;
  verdict: Verdict;
  /** One-line, staff-facing explanation of the verdict. */
  rationale: string;
  /** Which detectors/rules fired, for transparency and testing. */
  matchedRules: string[];
}

// ── §4 data-tier detectors ──────────────────────────────────────────────────
// "Choose the most sensitive category that applies." Detectors run high→low and
// the first tier that matches wins.

// Sensitive — any Personally Identifiable Information, participant/staff/donor
// data, or passwords (§4 "Sensitive (highest risk)").
const PII_PATTERNS: Array<[RegExp, string]> = [
  [/\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/i, "email address"],
  [/\b\d{3}-\d{2}-\d{4}\b/, "ssn"],
  [/\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b/, "phone number"],
  [/\b\d{1,5}\s+[A-Za-z0-9.\s]{2,30}\b(?:st|street|ave|avenue|rd|road|blvd|boulevard|lane|ln|drive|dr|court|ct|apt)\b/i, "street address"],
  [/\b(?:dob|date of birth)\b/i, "date of birth"],
  [/\bcase\s*(?:#|no\.?|number)\s*\w+/i, "case number"],
];
const SENSITIVE_MARKERS: Array<[RegExp, string]> = [
  [/\b(participant|client|applicant|program participant)\b.*\b(data|record|info|information|name|list)\b/i, "participant data"],
  [/\bapplication data\b/i, "application data"],
  [/\bdonor\b/i, "donor data"],
  [/\b(resume|résumé|cover letter|personnel file|performance evaluation)\b/i, "staff/personnel data"],
  [/\bpassword\b/i, "password/credential"],
];

// Restricted — internal documents, financials, grants, meeting notes, member or
// instructor IP (§4 "Restricted (medium risk)").
const RESTRICTED_MARKERS: Array<[RegExp, string]> = [
  [/\b(internal|hr|human resources|staff polic|job description)\b/i, "internal/HR document"],
  [/\b(budget|tax return|990|financial statement|insurance document)\b/i, "financial document"],
  [/\bgrant\s+(proposal|draft|application|report)\b/i, "grant proposal"],
  [/\b(meeting notes|meeting minutes|minutes of|internal email)\b/i, "internal meeting notes/email"],
  [/\b(member[-\s]?owned|instructor[-\s]?owned|proprietary protocol|instructor(?:'s)? curriculum|lab protocol)\b/i, "member/instructor material"],
];

// Instructor/member IP being *used* is prohibited outright (§5 prohibited #4/#5),
// distinct from merely being restricted data.
const IP_USE_MARKERS: Array<[RegExp, string]> = [
  [/\b(instructor|member)['’s]*\s+(ip|intellectual property|protocol|curriculum|teaching material)\b/i, "member/instructor IP"],
  [/\b(fine[-\s]?tune|train (?:a |the )?model)\b.*\b(protocol|curriculum|proprietary)\b/i, "training on proprietary protocol"],
];

// Ambiguity markers — a document is referenced but its classification is unclear.
// §5 restricted #5: "any use involving unclear data classification or ambiguous
// case." §7: "when unsure, pause and escalate."
const AMBIGUOUS_MARKERS: Array<[RegExp, string]> = [
  [/\b(this document|our records|the attached|the file|our internal|de[-\s]?identified)\b/i, "unclear/internal reference"],
];

// ── §5 use-type detectors (secondary; the verdict keys mainly off tier×env) ──
const USE_PATTERNS: Array<[RegExp, UseType]> = [
  [/\b(draft|write|compose|rewrite|translate|email|letter)\b/i, "drafting"],
  [/\b(summari[sz]e|summary|tl;?dr|condense)\b/i, "summarization"],
  [/\b(brainstorm|ideas?|suggest|come up with)\b/i, "brainstorming"],
  [/\b(analy[sz]e|analysis|trends?|dataset|statistics|data)\b/i, "data_analysis"],
  [/\b(image|logo|graphic|flyer|poster|generate a picture)\b/i, "image_generation"],
  [/\b(meeting notes|minutes)\b/i, "meeting_notes"],
];

function firstMatch<T>(pairs: Array<[RegExp, T]>, text: string): [T, string] | null {
  for (const [re, label] of pairs) {
    if (re.test(text)) return [label, String(label)];
  }
  return null;
}

function detectTier(text: string): { tier: Tier; hits: string[] } {
  const hits: string[] = [];
  for (const [re, label] of [...PII_PATTERNS, ...SENSITIVE_MARKERS]) {
    if (re.test(text)) hits.push(`sensitive:${label}`);
  }
  if (hits.length) return { tier: "sensitive", hits };
  for (const [re, label] of RESTRICTED_MARKERS) {
    if (re.test(text)) hits.push(`restricted:${label}`);
  }
  if (hits.length) return { tier: "restricted", hits };
  return { tier: "public", hits };
}

function detectUseType(text: string): UseType {
  const m = firstMatch(USE_PATTERNS, text);
  return m ? m[0] : "other";
}

/**
 * Classify a proposed AI use. Deterministic and side-effect free.
 * When in doubt the result escalates (higher risk) per §7.
 */
export function classify(input: ClassifyInput): Classification {
  const text = (input.text || "").trim();
  const environment: Environment = input.declared?.environment ?? input.environment ?? "external";
  const matched: string[] = [];

  // A declared tier (from an explicit self-assessment) overrides detection.
  const detected = detectTier(text);
  const tier: Tier = input.declared?.tier ?? detected.tier;
  matched.push(...detected.hits);
  if (input.declared?.tier) matched.push(`declared:${input.declared.tier}`);

  const useType: UseType = input.declared?.useType ?? detectUseType(text);

  // ── Rule 1 — using member/instructor IP is prohibited on external tools
  //    (§5 prohibited #4/#5), independent of tier detection. ──
  const ip = firstMatch(IP_USE_MARKERS, text);
  if (ip && environment === "external") {
    matched.push(`prohibited-rule:${ip[1]}`);
    return {
      tier: tier === "public" ? "restricted" : tier,
      useType,
      environment,
      verdict: "prohibited",
      rationale:
        "Using member or instructor intellectual property (protocols, curriculum) in an external AI tool is prohibited. Keep it out of AI; route it to the Director of Operations.",
      matchedRules: matched,
    };
  }

  // ── Rule 2 — sensitive data into an external tool is prohibited
  //    (§7: "do not input Sensitive data into external AI tools"). ──
  if (tier === "sensitive" && environment === "external") {
    return {
      tier,
      useType,
      environment,
      verdict: "prohibited",
      rationale:
        `Blocked: this looks like Sensitive data (${detected.hits.map((h) => h.split(":")[1]).join(", ") || "PII"}) and this is an external AI tool. Remove the identifying details, or use an org-controlled tool. Sensitive data must not go into external AI.`,
      matchedRules: matched,
    };
  }

  // ── Rule 3 — sensitive data on internal/controlled infra still needs review
  //    (§5 restricted; participant data is never simply "allowed"). ──
  if (tier === "sensitive") {
    return {
      tier,
      useType,
      environment,
      verdict: "restricted",
      rationale:
        "This involves Sensitive data. Even on org-controlled infrastructure it needs review/approval before use. Confirm with your team.",
      matchedRules: matched,
    };
  }

  // ── Rule 4 — restricted-tier data requires review/approval (§5 restricted). ──
  if (tier === "restricted") {
    return {
      tier,
      useType,
      environment,
      verdict: "restricted",
      rationale:
        "This touches Restricted material (internal, financial, grant, or member/instructor documents). It's allowed with review — check with the Director of Operations before relying on the result.",
      matchedRules: matched,
    };
  }

  // ── Rule 5 — an ambiguous/unclear reference escalates (§5 #5, §7). ──
  const amb = firstMatch(AMBIGUOUS_MARKERS, text);
  if (amb) {
    matched.push(`ambiguous:${amb[1]}`);
    return {
      tier: "restricted",
      useType,
      environment,
      verdict: "restricted",
      rationale:
        "The data classification here is unclear. When unsure, assume the higher risk: have someone confirm this isn't Restricted or Sensitive before proceeding.",
      matchedRules: matched,
    };
  }

  // ── Rule 6 — public, low-risk use is allowed (§5 allowed). ──
  return {
    tier: "public",
    useType,
    environment,
    verdict: "allowed",
    rationale:
      "Low-risk: no Restricted or Sensitive data detected. Proceed — and remember the output is a draft to verify with your team.",
    matchedRules: matched,
  };
}
