// Adapter smoke tests for the Pi guardrail extension. A mocked ExtensionAPI /
// context drives the real handlers, so we verify the wiring — block, escalate,
// pass-through, fail-safe — without launching Pi or calling the model.
// Run: npm test  (tsx --test)
import test from "node:test";
import assert from "node:assert/strict";
import guardrail from "../.pi/extensions/guardrail.ts";

type Note = { msg: string; level: string };

function makePi() {
  const handlers: Record<string, (...a: any[]) => any> = {};
  const pi: any = {
    on: (name: string, fn: (...a: any[]) => any) => { handlers[name] = fn; },
    registerProvider() {},
    registerTool() {},
    registerCommand() {},
  };
  guardrail(pi);
  return handlers;
}

function makeCtx(opts: { hasUI?: boolean; confirm?: boolean } = {}) {
  const notes: Note[] = [];
  const ctx = {
    hasUI: opts.hasUI ?? true,
    ui: {
      notify: (msg: string, level: string) => notes.push({ msg, level }),
      confirm: async () => opts.confirm ?? false,
    },
  };
  return { ctx, notes };
}

test("prohibited input is blocked (handled); the model is never reached", async () => {
  const h = makePi();
  const { ctx, notes } = makeCtx();
  const res = await h.input({ text: "email maria@example.org, case #A12345", source: "interactive" }, ctx);
  assert.deepEqual(res, { action: "handled" });
  assert.ok(notes.some((n) => n.level === "error"));
});

test("allowed input passes through", async () => {
  const h = makePi();
  const { ctx } = makeCtx();
  const res = await h.input({ text: "draft a public thank-you note for volunteers", source: "interactive" }, ctx);
  assert.deepEqual(res, { action: "continue" });
});

test("restricted input, declined at confirm → held back (handled)", async () => {
  const h = makePi();
  const { ctx } = makeCtx({ confirm: false });
  const res = await h.input({ text: "summarize our internal HR staff policy", source: "interactive" }, ctx);
  assert.deepEqual(res, { action: "handled" });
});

test("restricted input, approved → continue, then a review notice is injected", async () => {
  const h = makePi();
  const { ctx } = makeCtx({ confirm: true });
  const res = await h.input({ text: "summarize our internal HR staff policy", source: "interactive" }, ctx);
  assert.deepEqual(res, { action: "continue" });
  const injected = await h.before_agent_start({}, ctx);
  assert.ok(String(injected?.message?.content).includes("governance"));
  // The notice is consumed once — a second turn injects nothing.
  assert.equal(await h.before_agent_start({}, ctx), undefined);
});

test("no interactive UI (tests/RPC/--print): restricted fails safe → handled", async () => {
  const h = makePi();
  const { ctx } = makeCtx({ hasUI: false });
  const res = await h.input({ text: "summarize our internal HR staff policy", source: "interactive" }, ctx);
  assert.deepEqual(res, { action: "handled" });
});

test("extension-injected input is not re-screened", async () => {
  const h = makePi();
  const { ctx } = makeCtx();
  const res = await h.input({ text: "email a@b.com", source: "extension" }, ctx);
  assert.deepEqual(res, { action: "continue" });
});

test("slash-commands and blank input pass through", async () => {
  const h = makePi();
  const { ctx } = makeCtx();
  assert.deepEqual(await h.input({ text: "/model", source: "interactive" }, ctx), { action: "continue" });
  assert.deepEqual(await h.input({ text: "   ", source: "interactive" }, ctx), { action: "continue" });
});
