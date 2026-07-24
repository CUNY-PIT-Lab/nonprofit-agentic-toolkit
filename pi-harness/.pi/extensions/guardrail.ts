// guardrail.ts — the governance guardrail for the Non-Profit AI Toolkit's Pi agent.
//
// A pre-flight check that runs Boryana Ivanova's Genspace decision logic
// (../../src/classify.ts) on every user message BEFORE it reaches GLM-5.2:
//   - prohibited  → block; the model is never called.
//   - restricted  → require an explicit human confirmation; on approval, tell the
//                    model to keep the answer a reviewable draft.
//   - allowed     → pass through (SYSTEM.md carries the standing verify-with-team rule).
//
// This is the thin Pi adapter; all the decision logic lives in the harness-
// independent classifier, so the same rules can back a Hermes fork or server.py.
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { classify, type Classification } from "../../src/classify.ts";

export default function (pi: ExtensionAPI) {
  // Threads a restricted-but-approved verdict from `input` into the turn context.
  let pendingReview: Classification | null = null;

  pi.on("input", async (event, ctx) => {
    // Messages we injected ourselves, slash-commands, and blank input pass through.
    if (event.source === "extension") return { action: "continue" };
    const text = (event.text || "").trim();
    if (!text || text.startsWith("/")) return { action: "continue" };

    const result = classify({ text }); // environment defaults to "external"

    if (result.verdict === "prohibited") {
      ctx.ui.notify(`⛔ blocked — ${result.rationale}`, "error");
      return { action: "handled" }; // GLM-5.2 is never called
    }

    if (result.verdict === "restricted") {
      // No interactive UI (tests, RPC, --print): fail safe — hold for review.
      if (!ctx.hasUI) {
        ctx.ui.notify(`held for review — ${result.rationale}`, "warning");
        return { action: "handled" };
      }
      const ok = await ctx.ui.confirm(
        "governance check — review required",
        `${result.rationale}\n\nsend to GLM-5.2 anyway?`,
      );
      if (!ok) {
        ctx.ui.notify("held back for review. nothing was sent to AI.", "warning");
        return { action: "handled" };
      }
      pendingReview = result;
      ctx.ui.notify("proceeding — verify the result with your team before you use it.", "warning");
      return { action: "continue" };
    }

    return { action: "continue" };
  });

  // For an approved restricted request, remind the model to keep the answer a
  // reviewable draft (belt-and-suspenders with SYSTEM.md's standing rule).
  pi.on("before_agent_start", async () => {
    if (!pendingReview) return;
    const c = pendingReview;
    pendingReview = null;
    return {
      message: {
        customType: "guardrail-review",
        content:
          `[governance] the staffer's request was classified ${c.tier}/${c.verdict}. ` +
          "Answer, but frame the result as a draft a human must review before use; " +
          "do not present it as final or authoritative.",
        display: true,
      },
    };
  });
}
