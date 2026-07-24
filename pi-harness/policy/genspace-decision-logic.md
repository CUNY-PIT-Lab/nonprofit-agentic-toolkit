# Genspace decision logic — the guardrail's source of truth

This is the human-readable specification that `src/classify.ts` implements. It is
transcribed from **Boryana Ivanova's** Genspace *AI Use and Data Governance
Policy* and its companion **AI Decision Support Tool** (her "AI for What?!?"
capstone), so every rule the guardrail enforces is traceable and editable by a
non-engineer. When the policy changes, change this file and `classify.ts`
together.

Boryana's tool asks three questions. The verdict is a function of the answers.

## Step 1 — what data will be used? (§4)

Pick the **most sensitive** category that applies.

- **Public (low risk)** — educational resources; non-proprietary lab protocols;
  materials explicitly consented for public use.
- **Restricted (medium risk)** — internal documents (HR: staff policies, job
  descriptions, interview docs; financial: budgets, 990s, donations, insurance;
  grant proposals; meeting notes; internal emails); member-owned protocols /
  instructor-owned curriculum (IP); member stories/interviews unless consented.
- **Sensitive (highest risk)** — any Personally Identifiable Information (names,
  emails, addresses, SSNs, phone numbers, dates of birth) in text/audio/video/
  photos; program-participant data; application data; staff data (resumes, cover
  letters, personnel files, performance evaluations); donor data; passwords.

## Step 2 — what kind of AI use? (§5)

Drafting/writing · summarization · brainstorming · data analysis · image/content
generation · meeting notes · other/unsure. Use type refines the verdict but the
dominant signals are the data tier and the tool environment.

## Step 3 — what tool environment? (§5 footnote)

- **External AI** — any tool outside Genspace-managed infrastructure: personal
  accounts, consumer products (ChatGPT, Claude, Gemini, Copilot), or any
  third-party infrastructure the org does not control. **GLM-5.2 on Ollama Cloud
  is external.**
- **Internal / local AI** — infrastructure the org directly manages and governs.
  This is one possible environment after the Red Line Test documents the privacy
  boundary and later tests confirm capacity, accountability, and review.

## The verdict (§5 + §7 operational rules)

The guardrail defaults `environment = external`, because that is what the toolkit
runs on today. Given that:

| Data tier | External environment | Internal environment |
|---|---|---|
| **Sensitive** | **Prohibited** — block | Restricted — review |
| **Restricted** | Restricted — review | Restricted — review |
| **Public** | **Allowed** | Allowed |

Overriding rules, applied first:

1. **Member/instructor IP used in an external tool → Prohibited** (§5 prohibited
   #4/#5: using member or instructor IP; training/fine-tuning on proprietary
   protocols). Never route it through external AI.
2. **Sensitive data into an external tool → Prohibited** (§7: "do not input
   Sensitive data into external AI tools").
3. **Unclear/ambiguous classification → Restricted** (§5 restricted #5; §7:
   "when unsure, pause and escalate" — assume the higher risk).

Cross-cutting operational rules the agent always carries (§7, §3 transparency):

- Outputs are drafts; a human reviews and verifies before use.
- AI does not make final decisions affecting participants.
- When unsure, pause and escalate to the Director of Operations.

## How the agent acts on each verdict

- **Allowed** → answer, and end substantive answers with a verify-with-your-team
  line.
- **Restricted** → require an explicit human confirmation before answering, and
  attach a review notice.
- **Prohibited** → **block before the model is ever called**, and explain why in
  the policy's own terms.

Boryana's live tool: <https://boryanata.github.io/ai-decision-support-tool/>
(slated for the Genspace wiki). This guardrail turns that self-assessment into an
automatic pre-flight check.
