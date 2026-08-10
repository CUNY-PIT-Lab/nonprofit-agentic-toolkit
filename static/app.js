"use strict";

if (window.location.hostname === "zmuhls.github.io") {
  const canonical = "https://toolkit-api-production-535d.up.railway.app/";
  const safeFragment = /^(#(?:verify|reset)\?token=[A-Za-z0-9._~-]+)$/.test(window.location.hash)
    ? window.location.hash
    : "";
  window.location.replace(canonical + safeFragment);
}

const FALLBACK_STAGES = [
  {
    key: "entry",
    label: "Describe the proposal",
    shortLabel: "Describe the proposal",
    intro: "The guide uses your initial description, then asks one question at a time about purpose, current practice, affected people, ownership, capacity, and reasons to stop."
  },
  {
    key: "redline",
    label: "Red line test",
    shortLabel: "Red line test",
    intro: "Set conditions for privacy, consent, human authority, equity, audit, ownership, and organizational capacity."
  },
  {
    key: "stress",
    label: "Stress test",
    shortLabel: "Stress test",
    intro: "Examine failure, unsupported output, security, reliability, accessibility, correction, and recourse."
  },
  {
    key: "cost_benefit",
    label: "Costs and benefits",
    shortLabel: "Costs and benefits",
    intro: "Compare who benefits, labor, risk, resources, maintenance, and a credible non-AI option."
  },
  {
    key: "hidden_curriculum",
    label: "Hidden curriculum",
    shortLabel: "Hidden curriculum",
    intro: "Review what the proposal changes about values, behavior, authority, knowledge, invisible work, and dependence."
  },
  {
    key: "accountability",
    label: "Accountability",
    shortLabel: "Accountability",
    intro: "Identify who explains, audits, hears appeals, handles incidents, suspends the system, reviews it, and retires it."
  },
  {
    key: "internal_external_review",
    label: "Internal and external review",
    shortLabel: "Internal and external review",
    intro: "Bring affected staff, participants, existing advisory groups, governance groups, and approvers into the decision."
  },
  {
    key: "synthesis",
    label: "Synthesis",
    shortLabel: "Synthesis",
    intro: ""
  }
];

let STAGES = FALLBACK_STAGES.map((stage) => ({ ...stage }));

const ANALYSIS_LABELS = {
  context: "Context",
  constraints: "Constraints",
  affordances: "Affordances",
  infrastructure: "Existing AI infrastructure",
  use_patterns: "Targeted use patterns"
};

const INTERFACE_STATES = new Set([
  "ask",
  "choose",
  "confirm",
  "classify",
  "resolve_conflict",
  "delegate",
  "record_unknown",
  "review_stage",
  "complete_stage",
  "stop_route"
]);

const QUICK_ACTION_LABELS = {
  unknown: "I don’t know yet",
  delegate: "Ask someone else",
  not_applicable: "Not applicable"
};

const PATHWAY_OUTCOMES = {
  proceed: {
    label: "Proceed",
    detail: "Confirm readiness and move to the next pathway node."
  },
  negotiate_return: {
    label: "Negotiate and return",
    detail: "Keep this node open while conditions or evidence change."
  },
  pause: {
    label: "Pause",
    detail: "Stop active progression without closing the record."
  },
  resume: {
    label: "Resume",
    detail: "Resume active review at this pathway node."
  },
  non_ai: {
    label: "Take a non-AI route",
    detail: "Continue the organizational work without deploying AI."
  },
  walk_away: {
    label: "Walk away",
    detail: "Close this proposal as a legitimate governance outcome."
  },
  reassess: {
    label: "Return for reassessment",
    detail: "Bring monitoring evidence back into internal and external review."
  },
  retire: {
    label: "Retire",
    detail: "Retire the system and preserve the decision record."
  },
  review: {
    label: "Return to review",
    detail: "Route the record back through organizational review."
  }
};

const TERMINAL_PATHWAY_NODES = new Set(["non_ai", "walked_away", "retired"]);
const UNGUIDED_PATHWAY_NODES = new Set(["synthesis", "pilot", "monitoring"]);

const state = {
  csrfToken: "",
  session: null,
  records: [],
  record: null,
  currentStage: 0,
  synthesis: null,
  cy: null,
  selectedNodeId: "",
  annotations: {},
  pathway: null,
  pendingPathwayDecision: null,
  currentView: "record",
  fieldworkCycles: [],
  fieldworkReplay: null,
  fieldworkEventOptions: [],
  sidecarHistory: [],
  sidecarRecordId: "",
  sidecarSelectionKey: "",
  evolutionConsent: "not_set",
  evolutionCollectionEnabled: false,
  namePreference: "",
  authEmail: "",
  resetToken: "",
  currentInterface: null,
  inflight: false,
  toastTimer: null
};

const byId = (id) => document.getElementById(id);
const firstValue = (...values) => values.find((value) => value !== undefined && value !== null);
const asArray = (value) => Array.isArray(value) ? value : value ? [value] : [];

async function loadProductIdentity() {
  try {
    const payload = await api.request("/api/product-evolution/identity");
    const displayName = String(firstValue(payload.display_name, ""))
      .replace(/[\u0000-\u001f\u007f]/g, " ")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, 120);
    if (!displayName) return;
    document.title = displayName;
    document.querySelectorAll("[data-product-name]").forEach((element) => {
      element.textContent = displayName;
    });
  } catch (_error) {
    // Naming is governed server-side; older deployments keep the bundled name.
  }
}

class ApiError extends Error {
  constructor(message, status, code, details) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

const api = {
  async request(path, options = {}) {
    const method = options.method || "GET";
    const headers = new Headers({ Accept: "application/json" });
    if (options.body !== undefined) headers.set("Content-Type", "application/json");
    if (state.csrfToken && !["GET", "HEAD", "OPTIONS"].includes(method)) {
      headers.set("X-CSRF-Token", state.csrfToken);
    }
    if (options.idempotent) {
      headers.set("Idempotency-Key", options.idempotencyKey || makeRequestId());
    }

    const request = () => fetch(path, {
      method,
      headers,
      credentials: "include",
      body: options.body === undefined ? undefined : JSON.stringify(options.body)
    });

    let response = await request();
    if (options.idempotent && [502, 503, 504].includes(response.status)) {
      await new Promise((resolve) => window.setTimeout(resolve, 350));
      response = await request();
    }

    let payload = {};
    if (response.status !== 204) {
      const contentType = response.headers.get("content-type") || "";
      payload = contentType.includes("application/json")
        ? await response.json().catch(() => ({}))
        : { message: await response.text().catch(() => "") };
    }

    if (!response.ok) {
      const message = firstValue(payload.message, payload.error, payload.detail, readableStatus(response.status));
      throw new ApiError(String(message), response.status, payload.code || "", payload);
    }
    return payload;
  }
};

function reviewStageCount() {
  const index = STAGES.findIndex((stage) => stage.key === "synthesis");
  return index >= 0 ? index : STAGES.length;
}

function synthesisStageIndex() {
  const index = STAGES.findIndex((stage) => stage.key === "synthesis");
  return index >= 0 ? index : STAGES.length - 1;
}

function stageDefinition(index = state.currentStage) {
  return STAGES[index] || STAGES[0];
}

async function loadStageDefinitions() {
  try {
    const payload = await api.request("/api/stages");
    const seen = new Set();
    const definitions = asArray(payload.stages).flatMap((item) => {
      if (!item || typeof item !== "object") return [];
      const key = String(firstValue(item.id, item.key, "")).trim();
      const label = String(firstValue(item.label, "")).trim();
      if (!/^[a-z][a-z0-9_]{0,59}$/.test(key) || !label || key === "synthesis" || seen.has(key)) return [];
      seen.add(key);
      return [{
        key,
        label,
        shortLabel: String(firstValue(item.short_label, label)),
        intro: String(firstValue(item.purpose, item.intro, "")),
        dimensions: asArray(item.dimensions)
      }];
    });
    if (definitions.length) {
      const synthesis = FALLBACK_STAGES.find((stage) => stage.key === "synthesis");
      STAGES = [...definitions, { ...synthesis }];
    }
  } catch (_error) {
    // Older deployments do not expose the dynamic stage contract yet.
    STAGES = FALLBACK_STAGES.map((stage) => ({ ...stage }));
  }
}

function makeRequestId() {
  if (window.crypto && typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
  const bytes = new Uint8Array(16);
  window.crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function readableStatus(status) {
  if (status === 400) return "Please check the information and try again.";
  if (status === 401) return "Sign in to continue.";
  if (status === 403) return "This action is not available for this account.";
  if (status === 404) return "That saved item could not be found.";
  if (status === 409) return "This request conflicts with an existing record.";
  if (status === 422) return "Please check each field and try again.";
  if (status === 429) return "Please wait a moment before trying again.";
  return "The service could not complete that request.";
}

function setStatus(message, success = false) {
  const status = byId("authStatus");
  status.textContent = message || "";
  status.classList.toggle("is-success", Boolean(success));
}

function showToast(message) {
  const toast = byId("toast");
  window.clearTimeout(state.toastTimer);
  toast.textContent = message;
  toast.hidden = false;
  state.toastTimer = window.setTimeout(() => {
    toast.hidden = true;
  }, 4200);
}

function setFormBusy(form, busy) {
  Array.from(form.elements).forEach((element) => {
    element.disabled = busy;
  });
  form.setAttribute("aria-busy", busy ? "true" : "false");
}

function normalizeSession(payload) {
  const user = firstValue(payload.user, payload.session && payload.session.user, null);
  const authenticated = Boolean(firstValue(payload.authenticated, payload.signed_in, user));
  const verified = Boolean(firstValue(
    payload.verified,
    payload.email_verified,
    user && firstValue(user.email_verified, user.verified, user.email_verified_at)
  ));
  state.csrfToken = String(firstValue(payload.csrf_token, payload.csrfToken, payload.session && payload.session.csrf_token, ""));
  return { authenticated, verified, user };
}

async function refreshSession() {
  try {
    const payload = await api.request("/api/auth/session");
    state.session = normalizeSession(payload);
  } catch (error) {
    if (error.status === 401) {
      state.session = { authenticated: false, verified: false, user: null };
      state.csrfToken = "";
      return state.session;
    }
    throw error;
  }
  updateAccountUi();
  return state.session;
}

function updateAccountUi() {
  const user = state.session && state.session.user;
  const name = user && firstValue(user.name, user.display_name, user.email);
  byId("accountName").textContent = name ? String(name) : "Account";
  const startButton = document.querySelector('[data-action="start-review"]');
  if (startButton) startButton.textContent = state.session && state.session.verified ? "Open the toolkit" : "Sign in";
}

function showAuth(view) {
  const dialog = byId("authDialog");
  const views = {
    login: "authLogin",
    register: "authRegister",
    forgot: "authForgot",
    reset: "authReset",
    verify: "authVerify"
  };
  Object.values(views).forEach((id) => {
    byId(id).hidden = id !== views[view];
  });
  setStatus("");
  if (!dialog.open) dialog.showModal();
  const firstInput = byId(views[view]).querySelector("input, button");
  window.setTimeout(() => firstInput && firstInput.focus(), 0);
}

function closeAuth() {
  const dialog = byId("authDialog");
  if (dialog.open) dialog.close();
}

function showVerification(message, title = "Check your email") {
  byId("verifyTitle").textContent = title;
  byId("verifyMessage").textContent = message;
  showAuth("verify");
}

async function handleLogin(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const values = new FormData(form);
  setFormBusy(form, true);
  setStatus("");
  try {
    state.authEmail = String(values.get("email") || "").trim();
    const payload = await api.request("/api/auth/login", {
      method: "POST",
      body: { email: state.authEmail, password: String(values.get("password") || "") },
      idempotent: true
    });
    state.session = normalizeSession(payload);
    if (!state.session.authenticated || !state.session.verified) {
      showVerification("Verify your email address before opening the toolkit.");
      return;
    }
    updateAccountUi();
    closeAuth();
    form.reset();
    await openToolkit();
  } catch (error) {
    if (error.code === "email_unverified" || error.status === 403) {
      showVerification("Verify your email address before opening the toolkit.");
    } else {
      setStatus(error.message);
    }
  } finally {
    setFormBusy(form, false);
  }
}

async function handleRegister(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const values = new FormData(form);
  setFormBusy(form, true);
  setStatus("");
  try {
    state.authEmail = String(values.get("email") || "").trim();
    await api.request("/api/auth/register", {
      method: "POST",
      body: {
        display_name: String(values.get("name") || "").trim(),
        email: state.authEmail,
        password: String(values.get("password") || "")
      },
      idempotent: true
    });
    form.reset();
    showVerification("Open the verification link we sent to your email address. The link must be used before you can sign in.");
  } catch (error) {
    setStatus(error.message);
  } finally {
    setFormBusy(form, false);
  }
}

async function handleForgot(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const values = new FormData(form);
  setFormBusy(form, true);
  setStatus("");
  try {
    state.authEmail = String(values.get("email") || "").trim();
    await api.request("/api/auth/forgot-password", {
      method: "POST",
      body: { email: state.authEmail },
      idempotent: true
    });
    setStatus("If an account uses that address, a reset link is on its way.", true);
    form.reset();
  } catch (error) {
    setStatus(error.message);
  } finally {
    setFormBusy(form, false);
  }
}

async function handleReset(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const values = new FormData(form);
  const password = String(values.get("password") || "");
  const confirmation = String(values.get("password_confirm") || "");
  if (password !== confirmation) {
    setStatus("The passwords do not match.");
    return;
  }
  if (!state.resetToken) {
    setStatus("This reset link is missing or has expired.");
    return;
  }
  setFormBusy(form, true);
  try {
    await api.request("/api/auth/reset-password", {
      method: "POST",
      body: { token: state.resetToken, password },
      idempotent: true
    });
    state.resetToken = "";
    form.reset();
    showAuth("login");
    setStatus("Your password has been updated. Sign in with the new password.", true);
  } catch (error) {
    setStatus(error.message);
  } finally {
    setFormBusy(form, false);
  }
}

async function resendVerification() {
  const button = byId("resendVerificationButton");
  button.disabled = true;
  setStatus("");
  try {
    await api.request("/api/auth/resend-verification", {
      method: "POST",
      body: state.authEmail ? { email: state.authEmail } : {},
      idempotent: true
    });
    setStatus("A new verification email is on its way.", true);
  } catch (error) {
    setStatus(error.message);
  } finally {
    button.disabled = false;
  }
}

function readLinkFragment() {
  const raw = window.location.hash.slice(1);
  const match = raw.match(/^(verify|reset)\?(.+)$/);
  if (!match) return null;
  const params = new URLSearchParams(match[2]);
  const token = params.get("token") || "";
  window.history.replaceState(null, "", window.location.pathname + window.location.search);
  if (!/^[A-Za-z0-9._~-]{20,}$/.test(token)) return { type: match[1], token: "" };
  return { type: match[1], token };
}

async function handleLinkFragment(fragment) {
  if (!fragment) return;
  if (fragment.type === "reset") {
    state.resetToken = fragment.token;
    showAuth("reset");
    if (!fragment.token) setStatus("This reset link is incomplete.");
    return;
  }
  showVerification("Checking the verification link…", "Verify your email");
  if (!fragment.token) {
    setStatus("This verification link is incomplete.");
    return;
  }
  try {
    await api.request("/api/auth/verify", {
      method: "POST",
      body: { token: fragment.token },
      idempotent: true
    });
    showAuth("login");
    setStatus("Your email is verified. Sign in to begin.", true);
  } catch (error) {
    setStatus(error.message);
  }
}

async function requireVerifiedSession() {
  const session = state.session || await refreshSession();
  if (!session.authenticated) {
    showAuth("login");
    return false;
  }
  if (!session.verified) {
    showVerification("Verify your email address before opening the toolkit.");
    return false;
  }
  return true;
}

async function openToolkit() {
  if (!await requireVerifiedSession()) return;
  byId("siteShell").hidden = true;
  byId("toolkitShell").hidden = false;
  window.scrollTo({ top: 0 });
  await loadRecords(false);
  if (state.record) {
    try {
      const payload = await api.request(`/api/records/${encodeURIComponent(recordId())}`);
      state.record = normalizeRecord(payload);
      state.synthesis = state.record.synthesis
        ? { ...state.record.synthesis, concept_map: state.record.concept_map }
        : null;
      loadRecordAnnotations();
      await loadPathway();
      renderNavigation();
      if (state.pathway) await navigateToPathwayNode(currentPathwayNode());
      else await selectStage(nextRecordStage(state.record), false);
    } catch (error) {
      showToast(error.message);
      showRecordHome();
    }
  } else {
    showRecordHome();
  }
}

function showLanding() {
  byId("toolkitShell").hidden = true;
  byId("siteShell").hidden = false;
  closeMobileSidebar();
  window.scrollTo({ top: 0 });
}

async function logout() {
  try {
    await api.request("/api/auth/logout", { method: "POST", body: {}, idempotent: true });
  } catch (error) {
    if (error.status !== 401) showToast(error.message);
  }
  state.session = { authenticated: false, verified: false, user: null };
  state.csrfToken = "";
  state.record = null;
  state.records = [];
  state.synthesis = null;
  state.pathway = null;
  state.fieldworkCycles = [];
  state.fieldworkReplay = null;
  state.sidecarHistory = [];
  state.sidecarRecordId = "";
  state.sidecarSelectionKey = "";
  state.evolutionConsent = "not_set";
  state.evolutionCollectionEnabled = false;
  if (state.cy) state.cy.destroy();
  state.cy = null;
  showLanding();
}

function recordId(record = state.record) {
  return String(firstValue(record && record.id, record && record.public_id, ""));
}

function conceptMapId() {
  return String(firstValue(
    state.synthesis && state.synthesis.concept_map && state.synthesis.concept_map.id,
    state.record && state.record.concept_map && state.record.concept_map.id,
    ""
  ));
}

function pathwayState(payload) {
  const value = firstValue(payload && payload.pathway, payload);
  return value && typeof value === "object" && value.run ? value : null;
}

function currentPathwayNode() {
  return String(firstValue(state.pathway && state.pathway.run && state.pathway.run.current_node, ""));
}

function pathwayNodeLabel(nodeId = currentPathwayNode()) {
  const nodes = firstValue(state.pathway && state.pathway.definition && state.pathway.definition.nodes, {});
  return String(firstValue(nodes && nodes[nodeId] && nodes[nodeId].label, nodeId.replaceAll("_", " "), "Pathway"));
}

async function loadPathway(initializeIfMissing = true) {
  if (!recordId()) {
    state.pathway = null;
    renderPathwayPanel();
    return null;
  }
  const path = `/api/records/${encodeURIComponent(recordId())}/pathway`;
  try {
    const payload = await api.request(path);
    state.pathway = pathwayState(payload);
  } catch (error) {
    if (error.status !== 404 || !initializeIfMissing) {
      state.pathway = null;
      renderPathwayPanel(error.message);
      return null;
    }
    try {
      const payload = await api.request(path, {
        method: "POST",
        body: { entry_role: "author" }
      });
      state.pathway = pathwayState(payload);
    } catch (initializeError) {
      state.pathway = null;
      renderPathwayPanel(initializeError.message);
      return null;
    }
  }
  renderPathwayPanel();
  return state.pathway;
}

function pathwayEdgesForCurrentNode() {
  const node = currentPathwayNode();
  return asArray(state.pathway && state.pathway.definition && state.pathway.definition.edges)
    .filter((edge) => String(edge && edge.from) === node);
}

function renderPathwayDecisionHistory() {
  const decisions = asArray(state.pathway && state.pathway.decisions);
  const list = byId("pathwayDecisionHistory");
  list.replaceChildren();
  byId("pathwayDecisionCount").textContent = String(decisions.length);
  if (!decisions.length) {
    const empty = document.createElement("li");
    empty.className = "pathway-history-empty";
    empty.textContent = "No pathway decisions have been committed yet.";
    list.appendChild(empty);
    return;
  }
  decisions.slice().reverse().forEach((decision) => {
    const item = document.createElement("li");
    const heading = document.createElement("strong");
    const outcome = PATHWAY_OUTCOMES[String(decision.outcome)] || {};
    heading.textContent = `${firstValue(outcome.label, String(decision.outcome).replaceAll("_", " "))}: ${pathwayNodeLabel(String(decision.from_node))} → ${pathwayNodeLabel(String(decision.to_node))}`;
    const rationale = document.createElement("p");
    rationale.textContent = String(firstValue(decision.rationale, "No rationale recorded."));
    const meta = document.createElement("small");
    const timestamp = decision.decided_at ? new Date(decision.decided_at) : null;
    meta.textContent = [
      `Decision ${firstValue(decision.sequence, "")}`,
      timestamp && !Number.isNaN(timestamp.getTime()) ? timestamp.toLocaleString() : "",
      decision.decision_hash ? `hash ${String(decision.decision_hash).slice(0, 12)}` : ""
    ].filter(Boolean).join(" · ");
    item.append(heading, rationale, meta);
    list.appendChild(item);
  });
}

function appendDynamicPathwayChoice(outcome) {
  const spec = PATHWAY_OUTCOMES[outcome];
  if (!spec || byId("pathwayChoices").querySelector(`[data-pathway-outcome="${outcome}"]`)) return;
  const button = document.createElement("button");
  button.type = "submit";
  button.className = `pathway-choice pathway-choice-dynamic${outcome === "retire" ? " pathway-stop" : ""}`;
  button.dataset.pathwayOutcome = outcome;
  const label = document.createElement("strong");
  label.textContent = spec.label;
  const detail = document.createElement("small");
  detail.textContent = spec.detail;
  button.append(label, detail);
  byId("pathwayChoices").appendChild(button);
}

function renderPathwayPanel(errorMessage = "") {
  const panel = byId("pathwayPanel");
  if (!state.record) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  const pathway = state.pathway;
  if (!pathway) {
    byId("pathwayCurrentNode").textContent = "Unavailable";
    byId("pathwayVersion").textContent = "—";
    byId("pathwayCycle").textContent = "—";
    byId("pathwayChecksum").textContent = "—";
    byId("pathwayDecisionForm").hidden = true;
    byId("pathwayStatus").textContent = errorMessage || "The pinned pathway could not be loaded.";
    renderPathwayDecisionHistory();
    return;
  }

  const run = pathway.run || {};
  const definition = pathway.definition || {};
  const node = currentPathwayNode();
  byId("pathwayCurrentNode").textContent = pathwayNodeLabel(node);
  byId("pathwayVersion").textContent = `${firstValue(run.family_key, definition.family_key, "pathway")} v${firstValue(run.version, definition.version, "—")}`;
  byId("pathwayCycle").textContent = String(firstValue(run.cycle_number, "—"));
  byId("pathwayChecksum").textContent = String(firstValue(run.definition_checksum, definition.checksum, "—"));
  byId("pathwayStatus").textContent = errorMessage;
  byId("pathwayTitle").textContent = TERMINAL_PATHWAY_NODES.has(node)
    ? pathwayNodeLabel(node)
    : "Choose what happens next";
  byId("pathwayPrompt").textContent = TERMINAL_PATHWAY_NODES.has(node)
    ? "This is a legitimate organizational outcome. The append-only decision history remains available for review and replay."
    : `The record is currently at ${pathwayNodeLabel(node)}. The model may draft a route; the organization records the decision and rationale.`;

  document.querySelectorAll(".pathway-choice-dynamic").forEach((button) => button.remove());
  const edges = pathwayEdgesForCurrentNode();
  const definedOutcomes = new Set(edges.map((edge) => String(edge.outcome)));
  const availableOutcomes = new Set(asArray(pathway.available_transitions).map((edge) => String(edge.outcome)));
  definedOutcomes.forEach((outcome) => appendDynamicPathwayChoice(outcome));
  const currentStageIndex = STAGES.findIndex((stage) => stage.key === node);
  const guidedProceedReady = currentStageIndex < 0 || node === "synthesis" || stageIsComplete(currentStageIndex);
  byId("pathwayDecisionForm").hidden = TERMINAL_PATHWAY_NODES.has(node) || !edges.length;
  byId("pathwayChoices").querySelectorAll("[data-pathway-outcome]").forEach((button) => {
    const outcome = String(button.dataset.pathwayOutcome);
    const isGatedRoute = outcome === "proceed" || outcome === "retire";
    const isDefined = definedOutcomes.has(outcome);
    const isAvailable = availableOutcomes.has(outcome);
    const hiddenForRunState = run.status === "paused"
      ? outcome !== "resume"
      : outcome === "resume";
    button.hidden = !isDefined || hiddenForRunState;
    button.disabled = !isDefined || (!isGatedRoute && !isAvailable) || (outcome === "proceed" && !guidedProceedReady);
    if (outcome === "proceed" && !guidedProceedReady) {
      button.title = "Complete this guided review node before proceeding.";
    } else {
      button.removeAttribute("title");
    }
  });
  renderPathwayDecisionHistory();
}

function loadRecordAnnotations() {
  state.annotations = {};
  asArray(state.record && state.record.annotations).forEach((annotation) => {
    const targetId = String(firstValue(annotation.target_id, ""));
    if (targetId) {
      state.annotations[targetId] = {
        id: String(firstValue(annotation.id, "")),
        body: String(firstValue(annotation.body, ""))
      };
    }
  });
}

function normalizeRecord(payload) {
  return firstValue(payload.record, payload.adoption_record, payload);
}

function normalizeRecords(payload) {
  return asArray(firstValue(payload.records, payload.items, payload.data, payload)).map(normalizeRecord);
}

async function loadRecords(showDialog = true) {
  try {
    const payload = await api.request("/api/records");
    state.records = normalizeRecords(payload);
    if (!state.record && state.records.length) state.record = state.records[0];
    if (showDialog) renderRecordsDialog();
  } catch (error) {
    if (error.status === 401) {
      state.session = { authenticated: false, verified: false, user: null };
      showAuth("login");
    } else {
      showToast(error.message);
    }
  }
}

function renderRecordsDialog() {
  const list = byId("recordsList");
  list.replaceChildren();
  if (!state.records.length) {
    const empty = document.createElement("p");
    empty.textContent = "No saved reviews yet.";
    list.appendChild(empty);
  } else {
    state.records.forEach((record) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "record-list-item";
      button.dataset.recordId = recordId(record);
      const label = document.createElement("span");
      const title = document.createElement("strong");
      title.textContent = String(firstValue(record.title, record.organization_name, record.organization, "Untitled review"));
      const detail = document.createElement("small");
      const synthesisIndex = synthesisStageIndex();
      const step = Math.min(synthesisIndex, Math.max(0, nextRecordStage(record)));
      detail.textContent = step === synthesisIndex ? "Synthesis ready" : `Continue at ${stageDefinition(step).label}`;
      label.append(title, detail);
      const arrow = document.createElement("span");
      arrow.setAttribute("aria-hidden", "true");
      arrow.textContent = "›";
      button.append(label, arrow);
      list.appendChild(button);
    });
  }
  const dialog = byId("recordsDialog");
  if (!dialog.open) dialog.showModal();
}

async function resumeRecord(id) {
  try {
    const payload = await api.request(`/api/records/${encodeURIComponent(id)}`);
    state.record = normalizeRecord(payload);
    state.synthesis = state.record.synthesis
      ? { ...state.record.synthesis, concept_map: state.record.concept_map }
      : null;
    loadRecordAnnotations();
    await loadPathway();
    byId("recordsDialog").close();
    renderNavigation();
    if (state.pathway) await navigateToPathwayNode(currentPathwayNode());
    else await selectStage(nextRecordStage(state.record), false);
  } catch (error) {
    showToast(error.message);
  }
}

function showRecordHome() {
  state.record = null;
  state.synthesis = null;
  state.pathway = null;
  state.sidecarHistory = [];
  state.sidecarRecordId = "";
  state.sidecarSelectionKey = "";
  state.currentView = "record";
  byId("recordHome").hidden = false;
  byId("conversationStage").hidden = true;
  byId("synthesisStage").hidden = true;
  byId("fieldworkStage").hidden = true;
  byId("pathwayDestination").hidden = true;
  byId("pathwayPanel").hidden = true;
  byId("recordName").textContent = "Guided review";
  byId("mobileStageTitle").textContent = "Describe the proposal";
  renderNavigation();
  window.scrollTo({ top: 0 });
}

async function createRecord(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (!await requireVerifiedSession()) return;
  const values = new FormData(form);
  setFormBusy(form, true);
  try {
    const organizationName = String(values.get("organization_name") || "").trim();
    const proposal = String(values.get("proposal") || "").trim();
    const payload = await api.request("/api/records", {
      method: "POST",
      body: {
        organization_name: organizationName,
        title: organizationName ? `${organizationName} review` : "AI adoption review",
        proposed_use: proposal
      },
      idempotent: true
    });
    state.record = normalizeRecord(payload);
    state.records.unshift(state.record);
    form.reset();
    await loadPathway();
    renderNavigation();
    await selectStage(0, false);
  } catch (error) {
    showToast(error.message);
  } finally {
    setFormBusy(form, false);
  }
}

function stageCollection(record = state.record) {
  if (!record) return {};
  const collection = {};
  [record.stage_records, record.stages, record.stage_states].forEach((stages) => {
    if (Array.isArray(stages)) {
      stages.forEach((stage, index) => {
        collection[String(firstValue(stage && stage.key, stage && stage.stage, index))] = stage;
      });
    } else if (stages && typeof stages === "object") {
      Object.assign(collection, stages);
    }
  });
  return collection;
}

function pinnedCycleForStage(index, record = state.record) {
  const definition = STAGES[index];
  if (!definition || record !== state.record || currentPathwayNode() !== definition.key) return null;
  const cycle = Number(state.pathway && state.pathway.run && state.pathway.run.cycle_number);
  return Number.isInteger(cycle) && cycle >= 1 ? cycle : null;
}

function stageRecord(index, record = state.record) {
  const stages = stageCollection(record);
  const definition = STAGES[index];
  if (!definition) return null;
  const pinnedCycle = pinnedCycleForStage(index, record);
  if (pinnedCycle !== null) {
    const pass = asArray(record && record.stage_passes)
      .filter((item) => String(firstValue(item && item.stage, item && item.key, "")) === definition.key)
      .findLast((item) => Number(firstValue(item && item.cycle_number, 1)) === pinnedCycle);
    if (pass) return pass;
  }
  const candidate = firstValue(stages[definition.key], stages[String(index)], stages[index], null);
  if (
    pinnedCycle !== null &&
    candidate &&
    Number(firstValue(candidate.cycle_number, 1)) !== pinnedCycle
  ) return null;
  return candidate;
}

function stageIsComplete(index, record = state.record) {
  const definition = STAGES[index];
  if (!definition || definition.key === "synthesis") return false;
  const pinnedCycle = pinnedCycleForStage(index, record);
  const completedSteps = asArray(record && record.completed_steps);
  if (completedSteps.some((step) => (
    String(firstValue(step.stage, step.key, step)) === definition.key &&
    (pinnedCycle === null || Number(firstValue(step.cycle_number, 1)) === pinnedCycle)
  ))) return true;
  const stage = stageRecord(index, record);
  if (stage) return ["complete", "completed"].includes(String(stage.status).toLowerCase()) || stage.completed === true || Boolean(stage.completed_at);
  if (pinnedCycle !== null) return false;
  const completedThrough = Number(firstValue(record && record.completed_through, record && record.completed_step, -1));
  return completedThrough >= index;
}

function nextRecordStage(record) {
  if (!record) return 0;
  const hasDetailedProgress = asArray(record.completed_steps).length > 0 || Object.keys(stageCollection(record)).length > 0;
  if (!hasDetailedProgress && record.current_stage) {
    const current = STAGES.findIndex((stage) => stage.key === record.current_stage);
    if (current >= 0) return current;
  }
  for (let index = 0; index < reviewStageCount(); index += 1) {
    if (!stageIsComplete(index, record)) return index;
  }
  return synthesisStageIndex();
}

function canOpenStage(index) {
  if (!state.record) return index === 0;
  if (state.pathway && currentPathwayNode() === String(STAGES[index] && STAGES[index].key)) return true;
  if (index === 0) return true;
  if (index === synthesisStageIndex()) {
    return Array.from({ length: reviewStageCount() }, (_, stage) => stageIsComplete(stage)).every(Boolean);
  }
  return stageIsComplete(index - 1) || Boolean(stageRecord(index));
}

function renderNavigation() {
  const navigation = byId("stageNavigation");
  navigation.replaceChildren();
  STAGES.forEach((stage, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "stage-nav-button";
    button.dataset.stageIndex = String(index);
    button.disabled = !canOpenStage(index);
    button.classList.toggle("is-current", Boolean(state.record) && state.currentView === "stage" && state.currentStage === index);
    button.classList.toggle("is-complete", index < reviewStageCount() && stageIsComplete(index));
    const number = document.createElement("span");
    number.className = "nav-number";
    number.textContent = index === 0 ? "0" : String(index);
    const label = document.createElement("span");
    label.textContent = stage.label;
    button.append(number, label);
    navigation.appendChild(button);
  });
  if (state.record) {
    const fieldworkButton = document.createElement("button");
    fieldworkButton.type = "button";
    fieldworkButton.className = "stage-nav-button stage-nav-utility";
    fieldworkButton.dataset.action = "open-fieldwork";
    fieldworkButton.classList.toggle("is-current", state.currentView === "fieldwork");
    const fieldworkMark = document.createElement("span");
    fieldworkMark.className = "nav-number";
    fieldworkMark.textContent = "F";
    const fieldworkLabel = document.createElement("span");
    fieldworkLabel.textContent = "Fieldwork cycles";
    fieldworkButton.append(fieldworkMark, fieldworkLabel);
    const pathwayButton = document.createElement("button");
    pathwayButton.type = "button";
    pathwayButton.className = "stage-nav-button stage-nav-utility";
    pathwayButton.dataset.action = "focus-pathway";
    const pathwayMark = document.createElement("span");
    pathwayMark.className = "nav-number";
    pathwayMark.textContent = "P";
    const pathwayLabel = document.createElement("span");
    pathwayLabel.textContent = "Pathway decisions";
    pathwayButton.append(pathwayMark, pathwayLabel);
    navigation.append(fieldworkButton, pathwayButton);
  }
  if (state.record) {
    byId("recordName").textContent = String(firstValue(
      state.record.title,
      state.record.organization_name,
      state.record.organization,
      "Guided review"
    ));
  }
}

async function selectStage(index, startIfEmpty = false) {
  if (!state.record || !canOpenStage(index)) return;
  state.currentView = "stage";
  state.currentStage = index;
  renderNavigation();
  byId("recordHome").hidden = true;
  byId("fieldworkStage").hidden = true;
  byId("pathwayDestination").hidden = true;
  const synthesisIndex = synthesisStageIndex();
  byId("conversationStage").hidden = index === synthesisIndex;
  byId("synthesisStage").hidden = index !== synthesisIndex;
  byId("mobileStageTitle").textContent = stageDefinition(index).shortLabel;
  closeMobileSidebar();
  window.scrollTo({ top: 0 });
  if (index === synthesisIndex) {
    await openSynthesis();
  } else {
    await openConversationStage(index, startIfEmpty);
  }
  renderPathwayPanel();
}

function recordTurns(index) {
  const stage = stageRecord(index);
  const definition = stageDefinition(index);
  const pinnedCycle = pinnedCycleForStage(index);
  const inPinnedCycle = (turn) => (
    pinnedCycle === null || Number(firstValue(turn && turn.cycle_number, 1)) === pinnedCycle
  );
  const direct = firstValue(stage && stage.turns, stage && stage.messages, null);
  if (direct) return asArray(direct).filter(inPinnedCycle);
  const conversations = firstValue(state.record && state.record.conversations, {});
  const grouped = firstValue(conversations && conversations[definition.key], conversations && conversations[String(index)], null);
  if (grouped) return asArray(grouped).filter(inPinnedCycle);
  const looseTurns = asArray(state.record && state.record.turns).filter((turn) => {
    const stageValue = firstValue(turn.stage, turn.stage_key, turn.step);
    const matchesStage = String(stageValue) === definition.key || Number(stageValue) === index;
    return matchesStage && (
      pinnedCycle === null || Number(firstValue(turn.cycle_number, 1)) === pinnedCycle
    );
  });
  const initialProposal = index === 0 && firstValue(state.record && state.record.proposal, state.record && state.record.proposed_use);
  if (looseTurns.length) {
    const hasInitialResponse = looseTurns.some((turn) => String(firstValue(turn.content, turn.text, "")) === String(initialProposal));
    return initialProposal && !hasInitialResponse
      ? [{ role: "user", content: String(initialProposal), synthetic: true }, ...looseTurns]
      : looseTurns;
  }
  return initialProposal ? [{ role: "user", content: String(initialProposal) }] : [];
}

function payloadTurns(payload) {
  const messages = asArray(payload.messages);
  if (payload.user_message) messages.push(payload.user_message);
  if (payload.message) messages.push(payload.message);
  return messages.filter((turn) => turn && typeof turn === "object" && firstValue(turn.content, turn.text, turn.message));
}

function mergePayloadTurns(payload) {
  if (!state.record) return;
  const existing = asArray(state.record.turns);
  const seen = new Set(existing.map((turn) => String(firstValue(turn.id, `${turn.stage}:${firstValue(turn.cycle_number, 1)}:${turn.ordinal}:${turn.role}`))));
  payloadTurns(payload).forEach((turn) => {
    const identity = String(firstValue(turn.id, `${turn.stage}:${firstValue(turn.cycle_number, 1)}:${turn.ordinal}:${turn.role}`));
    if (!seen.has(identity)) {
      existing.push(turn);
      seen.add(identity);
    }
  });
  state.record.turns = existing;
}

function normalizeTurn(turn) {
  if (typeof turn === "string") return { role: "assistant", content: turn };
  return {
    role: ["user", "human"].includes(String(firstValue(turn.role, turn.author, "")).toLowerCase()) ? "user" : "assistant",
    content: String(firstValue(turn.content, turn.text, turn.message, ""))
  };
}

function renderTurns(turns) {
  const conversation = byId("conversation");
  conversation.replaceChildren();
  turns.map(normalizeTurn).filter((turn) => turn.content).forEach((turn) => appendMessage(turn.role, turn.content));
}

function appendMessage(role, content, thinking = false) {
  const conversation = byId("conversation");
  const article = document.createElement("article");
  article.className = `message message-${role}${thinking ? " message-thinking" : ""}`;
  const roleLabel = document.createElement("span");
  roleLabel.className = "message-role";
  roleLabel.textContent = role === "user" ? "You" : "Guide";
  const body = document.createElement("p");
  body.className = "message-body";
  body.textContent = thinking ? "Reading your response " : String(content || "");
  article.append(roleLabel, body);
  conversation.appendChild(article);
  article.scrollIntoView({ behavior: "smooth", block: "nearest" });
  return article;
}

function interfaceFromStage(index = state.currentStage) {
  const stage = stageRecord(index) || {};
  const action = stage.next_action && typeof stage.next_action === "object" ? stage.next_action : {};
  return {
    ...action,
    stage_status: firstValue(stage.stage_status, stage.status, ""),
    coverage: firstValue(stage.coverage, {}),
    coverage_summary: firstValue(stage.coverage_summary, {}),
    blockers: asArray(stage.blockers),
    delegations: asArray(stage.delegations),
    open_questions: asArray(stage.open_questions),
    contradictions: asArray(stage.contradictions),
    review_routing: asArray(state.record && state.record.review_routing)
  };
}

function setTextAndVisibility(id, value) {
  const element = byId(id);
  const text = String(value || "").trim();
  element.textContent = text;
  element.hidden = !text;
}

function routeButton(label, action, options = {}) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = options.className || "button button-secondary";
  button.dataset.routeAction = action;
  button.dataset.routeContent = String(firstValue(options.content, label));
  if (options.optionId) button.dataset.optionId = options.optionId;
  if (options.targetRole) button.dataset.targetRole = options.targetRole;
  if (options.assigneeId) button.dataset.assigneeId = options.assigneeId;
  button.textContent = label;
  return button;
}

function appendFollowUpGroup(container, title, items, renderItem) {
  if (!items.length) return;
  const section = document.createElement("section");
  section.className = "follow-up-group";
  const heading = document.createElement("h3");
  heading.textContent = title;
  const list = document.createElement("ul");
  list.className = "follow-up-list";
  items.forEach((item) => {
    const row = document.createElement("li");
    const content = renderItem(item);
    const main = document.createElement("strong");
    main.textContent = content.main;
    row.appendChild(main);
    if (content.detail) {
      const detail = document.createElement("small");
      detail.textContent = content.detail;
      row.appendChild(detail);
    }
    list.appendChild(row);
  });
  section.append(heading, list);
  container.appendChild(section);
}

function renderFollowUp(payload) {
  const stage = stageRecord() || {};
  const delegations = asArray(firstValue(payload.delegations, stage.delegations, []));
  const reviewRouting = asArray(firstValue(payload.review_routing, state.record && state.record.review_routing, []));
  const delegationContainer = byId("delegationsList");
  const routingContainer = byId("reviewRoutingList");
  delegationContainer.replaceChildren();
  routingContainer.replaceChildren();

  appendFollowUpGroup(
    delegationContainer,
    "Delegated questions",
    delegations,
    (item) => ({
      main: String(firstValue(item.question, item.text, "Question awaiting a response")),
      detail: [
        firstValue(item.target_role_label, item.target_role, "Another reviewer"),
        firstValue(item.status, "open")
      ].filter(Boolean).join(" · ")
    })
  );

  reviewRouting.forEach((group) => {
    appendFollowUpGroup(
      routingContainer,
      String(firstValue(group.label, group.role, "Reviewer")),
      asArray(group.items),
      (item) => ({
        main: String(firstValue(item.text, item.question, "Follow-up needed")),
        detail: [firstValue(item.kind, "Review item"), item.stage_label].filter(Boolean).join(" · ")
      })
    );
  });

  byId("followUpPanel").hidden = !delegationContainer.children.length && !routingContainer.children.length;
}

function renderInterface(payload = {}) {
  const stage = stageRecord() || {};
  const merged = { ...interfaceFromStage(), ...payload };
  const interfaceState = INTERFACE_STATES.has(String(merged.interface_state))
    ? String(merged.interface_state)
    : "";
  const completed = stageIsComplete(state.currentStage);
  state.currentInterface = interfaceState ? { ...merged, interface_state: interfaceState } : null;

  const panel = byId("routePanel");
  panel.hidden = !interfaceState;
  const dimension = String(firstValue(merged.dimension_label, merged.dimension, "")).trim();
  byId("routeDimension").textContent = dimension || "Current question";
  const coverage = merged.coverage_summary && typeof merged.coverage_summary === "object"
    ? merged.coverage_summary
    : {};
  byId("coverageLabel").textContent = String(firstValue(
    coverage.label,
    typeof merged.coverage_summary === "string" ? merged.coverage_summary : ""
  ));
  setTextAndVisibility("routeContext", merged.context_sentence);
  byId("routePrompt").textContent = String(firstValue(merged.prompt, "Continue the review"));
  setTextAndVisibility("routeStatement", interfaceState === "confirm" ? merged.statement : "");

  const conflict = merged.conflict && typeof merged.conflict === "object" ? merged.conflict : {};
  const showConflict = interfaceState === "resolve_conflict" && Boolean(conflict.earlier && conflict.now);
  byId("routeConflict").hidden = !showConflict;
  byId("conflictEarlier").textContent = showConflict ? String(conflict.earlier) : "";
  byId("conflictNow").textContent = showConflict ? String(conflict.now) : "";

  const choices = byId("routeChoices");
  const choiceList = byId("routeChoiceList");
  choiceList.replaceChildren();
  const options = asArray(merged.options).filter((option) => option && firstValue(option.label, option.text));
  if (["choose", "classify"].includes(interfaceState) && options.length) {
    options.forEach((option) => {
      const button = routeButton(
        String(firstValue(option.label, option.text)),
        interfaceState === "classify" ? "classification" : "choice",
        {
          className: "route-choice",
          content: String(firstValue(option.label, option.text)),
          optionId: String(firstValue(option.id, option.value, ""))
        }
      );
      const label = document.createElement("strong");
      label.textContent = String(firstValue(option.label, option.text));
      button.textContent = "";
      button.appendChild(label);
      if (option.detail) {
        const detail = document.createElement("small");
        detail.textContent = String(option.detail);
        button.appendChild(detail);
      }
      choiceList.appendChild(button);
    });
    choices.hidden = false;
  } else {
    choices.hidden = true;
  }

  const primary = byId("routePrimaryActions");
  primary.replaceChildren();
  if (interfaceState === "confirm") {
    primary.appendChild(routeButton("Confirm this statement", "reply", { content: "Yes, this is accurate." }));
  } else if (interfaceState === "delegate") {
    const role = String(firstValue(merged.target_role, "program_staff"));
    const roleLabel = String(merged.target_role_label || role.replaceAll("_", " "));
    primary.appendChild(routeButton(`Delegate to ${roleLabel}`, "delegate", {
      content: String(firstValue(merged.prompt, "Please answer this question.")),
      targetRole: role
    }));
  } else if (interfaceState === "record_unknown") {
    primary.appendChild(routeButton("Record this as unknown", "unknown", {
      content: `We do not know this yet: ${String(firstValue(merged.prompt, "This question remains open."))}`
    }));
  }

  const quickActions = byId("routeQuickActions");
  quickActions.replaceChildren();
  asArray(merged.quick_actions).forEach((action) => {
    const key = String(action);
    if (!QUICK_ACTION_LABELS[key]) return;
    const options = {};
    if (key === "unknown") options.content = `We do not know this yet: ${String(firstValue(merged.prompt, "This question remains open."))}`;
    if (key === "not_applicable") options.content = "This does not apply to the proposal.";
    if (key === "delegate") {
      options.content = String(firstValue(merged.prompt, "Please answer this question."));
      options.targetRole = String(firstValue(merged.target_role, "program_staff"));
    }
    quickActions.appendChild(routeButton(QUICK_ACTION_LABELS[key], key, options));
  });
  setTextAndVisibility("routeConsequence", merged.consequence);

  const form = byId("messageForm");
  form.hidden = completed;
  const input = byId("messageInput");
  const sendButton = byId("sendMessageButton");
  input.placeholder = ["review_stage", "complete_stage"].includes(interfaceState)
    ? "Correct or add to the draft…"
    : interfaceState === "resolve_conflict"
      ? "Explain what changed or how both accounts fit…"
      : "Your response…";
  sendButton.textContent = ["review_stage", "complete_stage"].includes(interfaceState)
    ? "Send correction"
    : "Send response";

  renderFollowUp(merged);
  const ready = responseIsReady(merged) || ["review_stage", "complete_stage"].includes(interfaceState);
  if (completed) {
    showCompletion("This step is complete. Its saved record remains available for review.", firstValue(merged.draft_record, ""), true);
  } else if (ready) {
    showCompletion(
      firstValue(coverage.label, merged.summary, "Review the drafted record, correct anything that is wrong, and complete this step when the organization is ready."),
      firstValue(merged.draft_record, ""),
      false
    );
  } else {
    byId("completionPanel").hidden = true;
  }
}

async function openConversationStage(index, startIfEmpty) {
  const definition = stageDefinition(index);
  byId("stageKicker").textContent = index === 0 ? "Entry screen" : `Step ${index}`;
  byId("stageTitle").textContent = definition.label;
  byId("stageIntro").textContent = definition.intro;
  byId("completionPanel").hidden = true;
  byId("messageForm").hidden = false;
  byId("messageInput").disabled = false;
  byId("sendMessageButton").disabled = false;

  const turns = recordTurns(index);
  renderTurns(turns);
  renderInterface(interfaceFromStage(index));
  const hasGuideTurn = turns.map(normalizeTurn).some((turn) => turn.role === "assistant");
  if (!hasGuideTurn) await startStage(index);
}

function updateRecordFromPayload(payload) {
  if (payload.record || payload.adoption_record) state.record = normalizeRecord(payload);
  if (payload.pathway) state.pathway = pathwayState(payload);
  if (payload.stage && typeof payload.stage === "object") {
    const stages = stageCollection();
    if (Array.isArray(state.record.stages)) {
      const match = state.record.stages.findIndex((stage) => String(firstValue(stage.key, stage.stage)) === stageDefinition().key);
      if (match >= 0) state.record.stages[match] = payload.stage;
      else state.record.stages.push(payload.stage);
    } else {
      state.record.stages = { ...stages, [stageDefinition().key]: payload.stage };
    }
  }
  const currentDefinition = stageDefinition();
  const stageKey = typeof payload.stage === "string" ? payload.stage : currentDefinition.key;
  const hasStructuredStage = Boolean(
    payload.interface_state ||
    payload.stage_status ||
    payload.coverage ||
    payload.coverage_summary ||
    payload.delegations ||
    payload.review_routing
  );
  if (hasStructuredStage && stageKey && stageKey !== "synthesis") {
    const stageStates = state.record.stage_states && typeof state.record.stage_states === "object"
      ? state.record.stage_states
      : {};
    const existing = stageStates[stageKey] || {};
    const action = {};
    [
      "interface_state",
      "dimension",
      "dimension_label",
      "context_sentence",
      "prompt",
      "options",
      "statement",
      "conflict",
      "target_role",
      "target_role_label",
      "consequence",
      "quick_actions"
    ].forEach((key) => {
      if (payload[key] !== undefined) action[key] = payload[key];
    });
    state.record.stage_states = {
      ...stageStates,
      [stageKey]: {
        ...existing,
        cycle_number: Number(firstValue(payload.cycle_number, existing.cycle_number, 1)),
        status: firstValue(payload.stage_status, existing.status, "in_progress"),
        coverage: firstValue(payload.coverage, existing.coverage, {}),
        coverage_summary: firstValue(payload.coverage_summary, existing.coverage_summary, {}),
        blockers: firstValue(payload.blockers, existing.blockers, []),
        delegations: firstValue(payload.delegations, existing.delegations, []),
        open_questions: firstValue(payload.open_questions, existing.open_questions, []),
        contradictions: firstValue(payload.contradictions, existing.contradictions, []),
        next_action: Object.keys(action).length ? action : existing.next_action
      }
    };
  }
  mergePayloadTurns(payload);
  if (payload.record_text && payload.stage && typeof payload.stage === "string") {
    const completed = asArray(state.record.completed_steps);
    const cycleNumber = Number(firstValue(payload.cycle_number, 1));
    if (!completed.some((step) => (
      String(firstValue(step.stage, step)) === payload.stage &&
      Number(firstValue(step.cycle_number, 1)) === cycleNumber
    ))) {
      completed.push({
        stage: payload.stage,
        cycle_number: cycleNumber,
        record_text: payload.record_text
      });
    }
    state.record.completed_steps = completed;
  }
  if (payload.next_stage) state.record.current_stage = payload.next_stage;
}

function responseMessage(payload) {
  const message = firstValue(payload.message, payload.assistant_message, payload.turn, payload.content, "");
  if (typeof message === "string") return message;
  return String(firstValue(message && message.content, message && message.text, ""));
}

function responseIsReady(payload) {
  return Boolean(
    payload.ready_to_complete ||
    payload.can_complete ||
    payload.stage_complete ||
    (payload.stage && (payload.stage.ready_to_complete || payload.stage.can_complete)) ||
    String(firstValue(payload.stage_status, payload.status, "")).toLowerCase() === "ready" ||
    ["review_stage", "complete_stage"].includes(String(payload.interface_state || ""))
  );
}

async function startStage(index) {
  if (state.inflight) return;
  state.inflight = true;
  setConversationBusy(true);
  const thinking = appendMessage("assistant", "", true);
  try {
    const payload = await api.request(
      `/api/records/${encodeURIComponent(recordId())}/stages/${encodeURIComponent(stageDefinition(index).key)}/start`,
      { method: "POST", body: {}, idempotent: true }
    );
    thinking.remove();
    updateRecordFromPayload(payload);
    const messages = asArray(payload.messages).map(normalizeTurn).filter((turn) => turn.content);
    if (messages.length) {
      renderTurns(recordTurns(index));
    } else {
      const content = responseMessage(payload);
      if (content) appendMessage("assistant", content);
    }
    renderInterface(payload);
    renderNavigation();
    setSaveStatus("Saved");
  } catch (error) {
    thinking.remove();
    appendMessage("assistant", error.message);
    setSaveStatus("Save failed");
  } finally {
    state.inflight = false;
    setConversationBusy(false);
  }
}

async function sendStageMessage(event) {
  event.preventDefault();
  const input = byId("messageInput");
  const content = input.value.trim();
  if (!content) return;
  input.value = "";
  const interfaceState = state.currentInterface && state.currentInterface.interface_state;
  const action = ["confirm", "resolve_conflict", "review_stage", "complete_stage"].includes(interfaceState)
    ? "correction"
    : "reply";
  await submitStageResponse({ content, action });
}

async function submitStageResponse({
  content,
  action = "reply",
  optionId = "",
  targetRole = "",
  assigneeId = "",
  displayContent = ""
}) {
  if (state.inflight || !String(content || "").trim()) return;
  const normalizedContent = String(content).trim();
  appendMessage("user", displayContent || normalizedContent);
  setSaveStatus("Saving…");
  state.inflight = true;
  setConversationBusy(true);
  const thinking = appendMessage("assistant", "", true);
  const idempotencyKey = makeRequestId();
  try {
    const payload = await api.request(
      `/api/records/${encodeURIComponent(recordId())}/stages/${encodeURIComponent(stageDefinition().key)}/messages`,
      {
        method: "POST",
        body: {
          content: normalizedContent,
          idempotency_key: idempotencyKey,
          action,
          dimension: String(firstValue(state.currentInterface && state.currentInterface.dimension, "")) || null,
          option_id: optionId || null,
          target_role: targetRole || null,
          assignee_id: assigneeId || null
        },
        idempotent: true,
        idempotencyKey
      }
    );
    thinking.remove();
    updateRecordFromPayload(payload);
    const response = responseMessage(payload);
    if (response) appendMessage("assistant", response);
    renderInterface(payload);
    setSaveStatus("Saved");
  } catch (error) {
    thinking.remove();
    appendMessage("assistant", error.message);
    setSaveStatus("Save failed");
  } finally {
    state.inflight = false;
    setConversationBusy(false);
  }
}

async function handleRouteAction(button) {
  const action = String(button.dataset.routeAction || "");
  if (!["reply", "choice", "classification", "unknown", "not_applicable", "delegate"].includes(action)) return;
  const delegateRole = String(firstValue(
    button.dataset.targetRole,
    state.currentInterface && state.currentInterface.target_role,
    ""
  ));
  const delegateLabel = String(
    state.currentInterface && (
      state.currentInterface.target_role_label ||
      delegateRole.replaceAll("_", " ")
    ) ||
    "another reviewer"
  );
  const displayContent = action === "unknown"
    ? QUICK_ACTION_LABELS.unknown
    : action === "not_applicable"
      ? QUICK_ACTION_LABELS.not_applicable
      : action === "delegate"
        ? `Delegate to ${delegateLabel}`
        : "";
  await submitStageResponse({
    content: button.dataset.routeContent || button.textContent || "Response",
    action,
    optionId: button.dataset.optionId || "",
    targetRole: button.dataset.targetRole || "",
    assigneeId: button.dataset.assigneeId || "",
    displayContent
  });
}

function setConversationBusy(busy) {
  byId("messageInput").disabled = busy;
  byId("sendMessageButton").disabled = busy;
  document.querySelectorAll("[data-route-action]").forEach((button) => {
    button.disabled = busy;
  });
  byId("conversationStage").setAttribute("aria-busy", busy ? "true" : "false");
  if (!busy && !byId("messageForm").hidden) byId("messageInput").focus();
}

function setSaveStatus(message) {
  byId("saveStatus").textContent = message;
}

function showCompletion(summary, draftRecord = "", completed = false) {
  byId("completionSummary").textContent = summary || "The guide has enough information to draft this part of the adoption record.";
  const review = byId("stageRecordReview");
  const draft = String(draftRecord || "").trim();
  review.hidden = !draft;
  if (draft) byId("stageRecordText").value = draft;
  byId("completeStageButton").hidden = completed;
  byId("completionPanel").hidden = false;
}

async function completeCurrentStage() {
  if (state.inflight) return;
  const button = byId("completeStageButton");
  button.disabled = true;
  setSaveStatus("Saving…");
  try {
    const payload = await api.request(
      `/api/records/${encodeURIComponent(recordId())}/stages/${encodeURIComponent(stageDefinition().key)}/complete`,
      {
        method: "POST",
        body: { record_text: byId("stageRecordReview").hidden ? null : byId("stageRecordText").value.trim() || null },
        idempotent: true
      }
    );
    updateRecordFromPayload(payload);
    const completedIndex = state.currentStage;
    if (!stageIsComplete(completedIndex)) {
      const stage = stageRecord(completedIndex) || {};
      stage.status = "completed";
      if (!state.record.stages || Array.isArray(state.record.stages)) {
        state.record.stage_states = { ...stageCollection(), [stageDefinition(completedIndex).key]: stage };
      } else {
        const collection = stageCollection();
        collection[stageDefinition(completedIndex).key] = stage;
      }
    }
    state.pathway = pathwayState(payload) || state.pathway;
    if (!state.pathway) await loadPathway();
    renderNavigation();
    renderInterface(interfaceFromStage(completedIndex));
    renderPathwayPanel();
    setSaveStatus("Saved");
    showToast(`${stageDefinition(completedIndex).label} saved. Choose the organization’s route below.`);
    byId("pathwayRationale").focus();
  } catch (error) {
    showToast(error.message);
    setSaveStatus("Save failed");
  } finally {
    button.disabled = false;
  }
}

function pathwayDecisionNeedsConfirmation(outcome) {
  return ["non_ai", "walk_away", "retire"].includes(outcome) ||
    (outcome === "proceed" && UNGUIDED_PATHWAY_NODES.has(currentPathwayNode()));
}

function openPathwayConfirmation(outcome, rationale) {
  const label = firstValue(PATHWAY_OUTCOMES[outcome] && PATHWAY_OUTCOMES[outcome].label, outcome.replaceAll("_", " "));
  const node = pathwayNodeLabel();
  const copy = outcome === "non_ai"
    ? `Confirm a non-AI route from ${node}. The fieldwork and decision record will remain available, and this will be recorded as a valid outcome rather than a failed review.`
    : outcome === "walk_away"
      ? `Confirm that the organization is walking away from this proposal at ${node}. This closes progression while preserving the reasons and evidence.`
      : outcome === "retire"
        ? `Confirm retirement from ${node}. This commits a retirement approval and preserves the monitoring record.`
        : `Confirm that ${node} is ready to proceed. This records a bounded, server-owned checkpoint before the evidence-bound approval and transition.`;
  state.pendingPathwayDecision = { outcome, rationale };
  byId("pathwayConfirmTitle").textContent = label;
  byId("pathwayConfirmCopy").textContent = copy;
  byId("confirmPathwayDecisionButton").textContent = `Confirm ${String(label).toLowerCase()}`;
  byId("confirmPathwayDecisionButton").classList.toggle("button-danger", ["walk_away", "retire"].includes(outcome));
  byId("pathwayConfirmDialog").showModal();
  byId("confirmPathwayDecisionButton").focus();
}

async function handlePathwayDecision(event) {
  event.preventDefault();
  const submitter = event.submitter;
  const outcome = String(submitter && submitter.dataset.pathwayOutcome || "");
  const rationale = byId("pathwayRationale").value.trim();
  if (!outcome || !event.currentTarget.reportValidity()) return;
  if (pathwayDecisionNeedsConfirmation(outcome)) {
    openPathwayConfirmation(outcome, rationale);
    return;
  }
  await executePathwayDecision(outcome, rationale);
}

function setPathwayBusy(busy) {
  byId("pathwayDecisionForm").setAttribute("aria-busy", busy ? "true" : "false");
  byId("pathwayRationale").disabled = busy;
  byId("pathwayChoices").querySelectorAll("button").forEach((button) => {
    button.disabled = busy || button.disabled;
  });
  byId("confirmPathwayDecisionButton").disabled = busy;
  byId("cancelPathwayDecisionButton").disabled = busy;
}

async function executePathwayDecision(outcome, rationale) {
  if (state.inflight || !state.pathway) return;
  const node = currentPathwayNode();
  state.inflight = true;
  setPathwayBusy(true);
  byId("pathwayStatus").textContent = "Committing the organization’s decision…";
  try {
    const idempotencyKey = makeRequestId();
    if (outcome === "proceed" && UNGUIDED_PATHWAY_NODES.has(node)) {
      const checkpointPayload = await api.request(
        `/api/records/${encodeURIComponent(recordId())}/pathway/checkpoints`,
        {
          method: "POST",
          body: {
            node,
            cycle_number: Number(state.pathway.run.cycle_number),
            confirmed: true,
            rationale,
            idempotency_key: `checkpoint-${idempotencyKey}`
          },
          idempotent: true,
          idempotencyKey: `checkpoint-${idempotencyKey}`
        }
      );
      state.pathway = pathwayState(checkpointPayload) || state.pathway;
    }

    const approvalGate = outcome === "proceed"
      ? `${node}_owner`
      : outcome === "retire"
        ? "retirement_owner"
        : "";
    if (approvalGate && !asArray(state.pathway.approved_gates).includes(approvalGate)) {
      const approvalPayload = await api.request(
        `/api/records/${encodeURIComponent(recordId())}/pathway/approvals`,
        {
          method: "POST",
          body: {
            gate_key: approvalGate,
            status: "approved",
            rationale
          }
        }
      );
      state.pathway = pathwayState(approvalPayload) || state.pathway;
    }

    const payload = await api.request(
      `/api/records/${encodeURIComponent(recordId())}/pathway/transitions`,
      {
        method: "POST",
        body: { outcome, rationale, idempotency_key: idempotencyKey },
        idempotent: true,
        idempotencyKey
      }
    );
    state.pathway = pathwayState(payload);
    const nextNode = currentPathwayNode();
    state.record.current_stage = nextNode;
    byId("pathwayRationale").value = "";
    renderPathwayPanel();
    renderNavigation();
    showToast(`${firstValue(PATHWAY_OUTCOMES[outcome] && PATHWAY_OUTCOMES[outcome].label, "Pathway decision")} recorded.`);
    const boundedRouteSignals = {
      negotiate_return: "pathway.negotiate_selected",
      non_ai: "pathway.non_ai_selected",
      walk_away: "pathway.walk_away_selected"
    };
    if (boundedRouteSignals[outcome]) {
      void sendProductSignal(boundedRouteSignals[outcome], {
        pathway_stage: node,
        route: outcome
      });
    }
    state.inflight = false;
    setPathwayBusy(false);
    await navigateToPathwayNode(nextNode);
  } catch (error) {
    byId("pathwayStatus").textContent = error.message;
    showToast(error.message);
  } finally {
    state.inflight = false;
    setPathwayBusy(false);
    renderPathwayPanel(byId("pathwayStatus").textContent);
  }
}

async function navigateToPathwayNode(node) {
  const stageIndex = STAGES.findIndex((stage) => stage.key === node);
  if (stageIndex >= 0 && canOpenStage(stageIndex)) {
    await selectStage(stageIndex, false);
    return;
  }
  if (TERMINAL_PATHWAY_NODES.has(node)) {
    showPathwayDestination(node);
    return;
  }
  await openFieldwork();
}

function showPathwayDestination(node) {
  state.currentView = "pathway";
  byId("recordHome").hidden = true;
  byId("conversationStage").hidden = true;
  byId("synthesisStage").hidden = true;
  byId("fieldworkStage").hidden = true;
  byId("pathwayDestination").hidden = false;
  byId("mobileStageTitle").textContent = pathwayNodeLabel(node);
  byId("pathwayDestinationTitle").textContent = pathwayNodeLabel(node);
  byId("pathwayDestinationCopy").textContent = node === "non_ai"
    ? "The organization chose a non-AI route. The decision, rationale, fieldwork cycles, and prior evidence remain replayable if conditions change."
    : node === "walked_away"
      ? "The organization chose to walk away from this proposal. The record remains available as evidence of governance in practice."
      : "The organization retired this pathway. Monitoring evidence and the retirement rationale remain in the append-only history.";
  closeMobileSidebar();
  renderNavigation();
  renderPathwayPanel();
  window.scrollTo({ top: 0 });
}

function localDateTimeValue(date = new Date()) {
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

function observedAtIso() {
  const input = byId("fieldworkObservedAt");
  const parsed = new Date(input.value);
  if (Number.isNaN(parsed.getTime())) throw new Error("Choose a valid observed time.");
  return parsed.toISOString();
}

function selectedFieldworkCycle() {
  return String(byId("fieldworkCycleSelect").value || "");
}

function canonicalFieldworkBranchId() {
  return recordId() ? `${recordId()}:canonical` : "";
}

function currentSidecarSelectionKey() {
  return [recordId(), selectedFieldworkCycle(), byId("fieldworkScale").value, canonicalFieldworkBranchId()].join("|");
}

function renderSidecarSelection() {
  const cycleId = selectedFieldworkCycle();
  const cycle = state.fieldworkCycles.find((item) => String(item.cycle_id) === cycleId);
  const enabled = Boolean(cycleId && canonicalFieldworkBranchId());
  byId("sidecarSelection").textContent = enabled
    ? `Canonical branch · ${String(firstValue(cycle && cycle.label, cycleId))} · ${byId("fieldworkScale").value.replaceAll("_", " ")} scale`
    : "Select a fieldwork cycle to begin.";
  byId("sidecarInput").disabled = !enabled;
  byId("sendSidecarButton").disabled = !enabled;
  byId("clearSidecarButton").disabled = !state.sidecarHistory.length;
}

function resetSidecarChat(message = "") {
  state.sidecarHistory = [];
  state.sidecarSelectionKey = currentSidecarSelectionKey();
  const log = byId("sidecarLog");
  log.replaceChildren();
  const empty = document.createElement("p");
  empty.className = "sidecar-empty";
  empty.id = "sidecarEmpty";
  empty.textContent = "No ephemeral messages yet.";
  log.appendChild(empty);
  byId("sidecarProvenance").hidden = true;
  byId("sidecarContextHash").textContent = "—";
  byId("sidecarModelVersion").textContent = "—";
  byId("sidecarEventCitations").replaceChildren();
  byId("sidecarSourceCitations").replaceChildren();
  byId("sidecarStatus").textContent = message;
  renderSidecarSelection();
}

function renderSidecarHistory() {
  const log = byId("sidecarLog");
  log.replaceChildren();
  if (!state.sidecarHistory.length) {
    const empty = document.createElement("p");
    empty.className = "sidecar-empty";
    empty.id = "sidecarEmpty";
    empty.textContent = "No ephemeral messages yet.";
    log.appendChild(empty);
  } else {
    state.sidecarHistory.forEach((message) => {
      const article = document.createElement("article");
      article.className = `sidecar-message is-${message.role}`;
      const role = document.createElement("span");
      role.textContent = message.role === "user" ? "You" : "Sidecar";
      const content = document.createElement("p");
      content.textContent = message.content;
      article.append(role, content);
      log.appendChild(article);
    });
  }
  byId("clearSidecarButton").disabled = !state.sidecarHistory.length;
  log.scrollTop = log.scrollHeight;
}

function boundedSidecarHistory() {
  const bounded = [];
  let total = 0;
  for (const message of state.sidecarHistory.slice().reverse()) {
    if (bounded.length >= 12) break;
    const content = String(message.content || "").slice(0, 4000);
    if (!content || total + content.length > 24000) continue;
    bounded.unshift({ role: message.role, content });
    total += content.length;
  }
  return bounded;
}

function renderCitationList(id, values, emptyLabel) {
  const list = byId(id);
  list.replaceChildren();
  if (!values.length) {
    const item = document.createElement("li");
    item.textContent = emptyLabel;
    list.appendChild(item);
    return;
  }
  values.forEach((value) => {
    const item = document.createElement("li");
    const code = document.createElement("code");
    code.textContent = String(value);
    item.appendChild(code);
    list.appendChild(item);
  });
}

function renderSidecarProvenance(payload) {
  byId("sidecarProvenance").hidden = false;
  byId("sidecarContextHash").textContent = String(firstValue(payload.context_hash, "Unavailable"));
  byId("sidecarModelVersion").textContent = String(firstValue(payload.model_version, "Unavailable"));
  const citations = payload.citations || {};
  renderCitationList("sidecarEventCitations", asArray(citations.event_ids), "No authorized event citations returned.");
  renderCitationList("sidecarSourceCitations", asArray(citations.source_ids), "No authorized source citations returned.");
}

async function sendSidecarMessage(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const message = byId("sidecarInput").value.trim();
  const cycleId = selectedFieldworkCycle();
  const branchId = canonicalFieldworkBranchId();
  if (!message || !cycleId || !branchId || !form.reportValidity()) return;
  if (state.sidecarSelectionKey !== currentSidecarSelectionKey()) resetSidecarChat();
  const requestHistory = boundedSidecarHistory();
  state.sidecarHistory.push({ role: "user", content: message });
  state.sidecarHistory = state.sidecarHistory.slice(-12);
  renderSidecarHistory();
  byId("sidecarInput").value = "";
  byId("sidecarStatus").textContent = "Reading the authorized projection…";
  byId("sidecarInput").disabled = true;
  byId("sendSidecarButton").disabled = true;
  byId("clearSidecarButton").disabled = true;
  form.setAttribute("aria-busy", "true");
  try {
    const payload = await api.request(
      `/api/records/${encodeURIComponent(recordId())}/sidecar/chat`,
      {
        method: "POST",
        body: {
          message,
          history: requestHistory,
          scale: byId("fieldworkScale").value,
          cycle_id: cycleId,
          branch_id: branchId
        }
      }
    );
    if (payload.persisted !== false || payload.canonical_effect !== false || payload.record_write_authority !== false) {
      throw new Error("The informational sidecar boundary could not be verified.");
    }
    state.sidecarHistory.push({ role: "assistant", content: String(payload.answer) });
    state.sidecarHistory = state.sidecarHistory.slice(-12);
    renderSidecarHistory();
    renderSidecarProvenance(payload);
    byId("sidecarStatus").textContent = "Ephemeral answer received. Nothing was added to the record.";
  } catch (error) {
    byId("sidecarStatus").textContent = error.message;
  } finally {
    form.setAttribute("aria-busy", "false");
    renderSidecarSelection();
    byId("sidecarInput").focus();
  }
}

function renderEvolutionConsent() {
  const enabled = state.evolutionCollectionEnabled;
  const granted = state.evolutionConsent === "granted";
  const checkbox = byId("evolutionConsent");
  checkbox.disabled = !enabled;
  checkbox.checked = granted;
  byId("namePreference").hidden = !enabled || !granted;
  byId("evolutionConsentHelp").textContent = !enabled
    ? "Bounded product signals are disabled for this deployment."
    : granted
      ? "Opted in. You can withdraw at any time; content collection remains off."
      : state.evolutionConsent === "withdrawn"
        ? "Opted out. No product-improvement signals will be accepted."
        : "Not opted in. No product-improvement signals will be accepted.";
  document.querySelectorAll("[data-evolution-signal]").forEach((button) => {
    button.disabled = !enabled || !granted;
    button.setAttribute("aria-pressed", button.dataset.evolutionSignal === state.namePreference ? "true" : "false");
  });
}

async function loadProductEvolutionConsent() {
  try {
    const payload = await api.request("/api/product-evolution/consent");
    state.evolutionCollectionEnabled = Boolean(payload.collection_enabled);
    state.evolutionConsent = String(firstValue(payload.consent, "not_set"));
    byId("evolutionStatus").textContent = "";
  } catch (error) {
    state.evolutionCollectionEnabled = false;
    state.evolutionConsent = "not_set";
    byId("evolutionStatus").textContent = error.message;
  }
  renderEvolutionConsent();
}

async function updateProductEvolutionConsent() {
  const checkbox = byId("evolutionConsent");
  const enabled = checkbox.checked;
  checkbox.disabled = true;
  byId("evolutionStatus").textContent = enabled ? "Recording opt-in…" : "Recording opt-out…";
  try {
    const payload = await api.request("/api/product-evolution/consent", {
      method: "POST",
      body: { enabled }
    });
    state.evolutionCollectionEnabled = Boolean(payload.collection_enabled);
    state.evolutionConsent = String(firstValue(payload.consent, enabled ? "granted" : "withdrawn"));
    byId("evolutionStatus").textContent = enabled
      ? "Opted in to bounded categorical signals. Content collection remains off."
      : "Opted out. No further product-improvement signals will be sent.";
  } catch (error) {
    byId("evolutionStatus").textContent = error.message;
    await loadProductEvolutionConsent();
  }
  renderEvolutionConsent();
}

async function sendProductSignal(signal, dimensions = {}) {
  if (!state.evolutionCollectionEnabled || state.evolutionConsent !== "granted") return false;
  const allowedSignals = new Set([
    "pathway.negotiate_selected",
    "pathway.non_ai_selected",
    "pathway.walk_away_selected",
    "fieldwork.replay_used",
    "name.preference.fieldwork_loop",
    "name.preference.current_toolkit"
  ]);
  if (!allowedSignals.has(signal)) return false;
  const idempotencyKey = makeRequestId();
  const body = { signal, idempotency_key: idempotencyKey };
  ["pathway_stage", "route", "scale"].forEach((key) => {
    const value = dimensions[key];
    if (typeof value === "string" && /^[a-z0-9][a-z0-9._:-]{0,79}$/.test(value)) body[key] = value;
  });
  try {
    const payload = await api.request("/api/product-evolution/signals", {
      method: "POST",
      body,
      idempotent: true,
      idempotencyKey
    });
    return payload.accepted === true;
  } catch (error) {
    if (![403, 404].includes(error.status)) return false;
    return false;
  }
}

async function recordNamePreference(button) {
  const signal = String(button.dataset.evolutionSignal || "");
  document.querySelectorAll("[data-evolution-signal]").forEach((item) => { item.disabled = true; });
  byId("evolutionStatus").textContent = "Recording the categorical name preference…";
  const accepted = await sendProductSignal(signal);
  if (accepted) {
    state.namePreference = signal;
    byId("evolutionStatus").textContent = "Name preference recorded without content or identifying details.";
  } else {
    byId("evolutionStatus").textContent = "The preference was not recorded; the toolkit remains fully usable.";
  }
  renderEvolutionConsent();
}

function setFieldworkStatus(message, success = false) {
  const element = byId("fieldworkStatus");
  element.textContent = message || "";
  element.classList.toggle("is-success", Boolean(success));
}

function setFieldworkEnabled(enabled) {
  [
    "fieldworkCycleSelect",
    "fieldworkEntryType",
    "fieldworkObservedAt",
    "fieldworkEntryContent",
    "appendFieldworkButton",
    "fieldworkScale",
    "fieldworkAsOf",
    "replayFieldworkButton"
  ].forEach((id) => {
    byId(id).disabled = !enabled;
  });
}

function renderFieldworkCycles(preferredCycleId = "") {
  const select = byId("fieldworkCycleSelect");
  const previous = preferredCycleId || select.value;
  select.replaceChildren();
  if (!state.fieldworkCycles.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "Create a cycle to begin";
    select.appendChild(option);
    setFieldworkEnabled(false);
    resetSidecarChat();
    return;
  }
  state.fieldworkCycles.forEach((cycle) => {
    const option = document.createElement("option");
    option.value = String(cycle.cycle_id);
    option.textContent = String(firstValue(cycle.label, cycle.cycle_id));
    select.appendChild(option);
  });
  const chosen = state.fieldworkCycles.some((cycle) => String(cycle.cycle_id) === previous)
    ? previous
    : String(state.fieldworkCycles[state.fieldworkCycles.length - 1].cycle_id);
  select.value = chosen;
  setFieldworkEnabled(true);
  if (state.sidecarSelectionKey && state.sidecarSelectionKey !== currentSidecarSelectionKey()) {
    resetSidecarChat("The ephemeral chat was cleared because the selected cycle changed.");
  } else {
    state.sidecarSelectionKey = currentSidecarSelectionKey();
    renderSidecarSelection();
  }
}

async function loadFieldworkCycles(preferredCycleId = "") {
  if (!recordId()) return;
  setFieldworkStatus("Loading fieldwork cycles…");
  try {
    const payload = await api.request(`/api/records/${encodeURIComponent(recordId())}/fieldwork/cycles`);
    state.fieldworkCycles = asArray(payload.cycles);
    renderFieldworkCycles(preferredCycleId);
    setFieldworkStatus("");
    if (selectedFieldworkCycle()) await loadFieldworkReplay();
    else renderFieldworkReplay(null);
  } catch (error) {
    setFieldworkStatus(error.message);
    state.fieldworkCycles = [];
    renderFieldworkCycles();
  }
}

async function openFieldwork() {
  if (!state.record) return;
  state.currentView = "fieldwork";
  byId("recordHome").hidden = true;
  byId("conversationStage").hidden = true;
  byId("synthesisStage").hidden = true;
  byId("pathwayDestination").hidden = true;
  byId("fieldworkStage").hidden = false;
  byId("mobileStageTitle").textContent = "Fieldwork cycles";
  byId("fieldworkObservedAt").value = localDateTimeValue();
  byId("fieldworkObservedAt").max = localDateTimeValue();
  if (state.sidecarRecordId !== recordId()) {
    state.sidecarRecordId = recordId();
    resetSidecarChat();
  }
  closeMobileSidebar();
  renderNavigation();
  renderPathwayPanel();
  window.scrollTo({ top: 0 });
  await loadFieldworkCycles(selectedFieldworkCycle());
  await loadProductEvolutionConsent();
}

async function createFieldworkCycle(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const label = byId("fieldworkCycleLabel").value.trim();
  if (!label || !form.reportValidity()) return;
  const now = new Date().toISOString();
  setFormBusy(form, true);
  setFieldworkStatus("Opening the cycle…");
  try {
    const payload = await api.request(
      `/api/records/${encodeURIComponent(recordId())}/fieldwork/cycles`,
      {
        method: "POST",
        body: { label, observed_at: now, recorded_at: now }
      }
    );
    const cycle = payload.cycle;
    byId("fieldworkCycleLabel").value = "";
    await loadFieldworkCycles(String(cycle.cycle_id));
    setFieldworkStatus("Cycle opened. New entries will be appended to its canonical branch.", true);
    byId("fieldworkEntryContent").focus();
  } catch (error) {
    setFieldworkStatus(error.message);
  } finally {
    setFormBusy(form, false);
  }
}

async function appendFieldworkEntry(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const cycleId = selectedFieldworkCycle();
  const entryType = String(byId("fieldworkEntryType").value);
  const content = byId("fieldworkEntryContent").value.trim();
  if (!cycleId || !content || !form.reportValidity()) return;
  const idempotencyKey = makeRequestId();
  setFormBusy(form, true);
  setFieldworkStatus("Appending the fieldnote…");
  try {
    const observedAt = observedAtIso();
    const recordedAt = new Date().toISOString();
    await api.request(
      `/api/records/${encodeURIComponent(recordId())}/fieldwork/cycles/${encodeURIComponent(cycleId)}/${encodeURIComponent(entryType)}`,
      {
        method: "POST",
        body: {
          content,
          observed_at: observedAt,
          recorded_at: recordedAt,
          idempotency_key: idempotencyKey,
          branch_id: null,
          causal_event_ids: [],
          source_refs: [],
          sensitivity: "internal",
          allowed_scales: [byId("fieldworkScale").value],
          consent_basis: "not_required",
          consent_subjects: [],
          authorization_tags: [],
          scope_node_ids: []
        },
        idempotent: true,
        idempotencyKey
      }
    );
    byId("fieldworkEntryContent").value = "";
    byId("fieldworkObservedAt").value = localDateTimeValue();
    await loadFieldworkReplay({ asOf: "" });
    setFieldworkStatus("Fieldnote appended. Earlier entries were not changed.", true);
  } catch (error) {
    setFieldworkStatus(error.message);
  } finally {
    setFormBusy(form, false);
    setFieldworkEnabled(Boolean(selectedFieldworkCycle()));
  }
}

function fieldworkEventTitle(event) {
  const entryType = event && event.payload && event.payload.entry_type;
  if (entryType) return String(entryType).replaceAll("_", " ");
  return String(firstValue(event && event.kind, "event")).replaceAll("_", " ");
}

function fieldworkEventCopy(event) {
  if (event.redacted) return "Content is redacted under the current consent and authorization policy.";
  const payload = event.payload || {};
  return String(firstValue(payload.content, payload.label, payload.reason, payload.title, "Recorded in the append-only ledger."));
}

function updateFieldworkAsOfOptions(events, selected = "") {
  const select = byId("fieldworkAsOf");
  select.replaceChildren();
  const latest = document.createElement("option");
  latest.value = "";
  latest.textContent = "Latest canonical state";
  select.appendChild(latest);
  events.forEach((event, index) => {
    const option = document.createElement("option");
    option.value = String(event.event_id);
    option.textContent = `${index + 1}. ${fieldworkEventTitle(event)}`;
    select.appendChild(option);
  });
  if (selected && events.some((event) => String(event.event_id) === selected)) select.value = selected;
}

function renderFieldworkReplay(payload) {
  state.fieldworkReplay = payload;
  const timeline = byId("fieldworkTimeline");
  const outputList = byId("fieldworkOutputList");
  timeline.replaceChildren();
  outputList.replaceChildren();
  if (!payload || !payload.projection) {
    byId("fieldworkEmptyState").hidden = false;
    byId("fieldworkOutputs").hidden = true;
    byId("fieldworkStateHash").textContent = "No replay yet";
    byId("fieldworkReplayBadge").textContent = "Stored exact projection";
    renderSidecarSelection();
    return;
  }
  const projection = payload.projection;
  const events = asArray(projection.events);
  const branchMode = String(firstValue(projection.branch && projection.branch.mode, "canonical"));
  byId("fieldworkEmptyState").hidden = Boolean(events.length);
  byId("fieldworkReplayBadge").textContent = branchMode === "counterfactual"
    ? "Counterfactual · simulation only"
    : "Stored exact projection";
  byId("fieldworkReplayExplainer").textContent = branchMode === "counterfactual"
    ? "This is a counterfactual simulation. Its events have no canonical effect and cannot overwrite the stored fieldwork record."
    : "This replay is a deterministic projection of stored, authorized ledger events. It does not regenerate model language.";
  const stateHash = String(firstValue(payload.state_hash, ""));
  byId("fieldworkStateHash").textContent = stateHash ? `state ${stateHash.slice(0, 12)}` : "State hash unavailable";
  byId("fieldworkStateHash").title = stateHash;
  events.forEach((event) => {
    const item = document.createElement("li");
    item.className = `fieldwork-event${event.redacted ? " is-redacted" : ""}`;
    const meta = document.createElement("div");
    meta.className = "fieldwork-event-meta";
    const layer = document.createElement("span");
    layer.textContent = String(firstValue(event.epistemic_layer, "record")).replaceAll("_", " ");
    const chronology = event.chronology || {};
    const time = document.createElement("time");
    const observed = new Date(firstValue(chronology.observed_at, chronology.committed_at, ""));
    time.textContent = Number.isNaN(observed.getTime()) ? "Time unavailable" : observed.toLocaleString();
    meta.append(layer, time);
    const heading = document.createElement("h3");
    heading.textContent = fieldworkEventTitle(event);
    const copy = document.createElement("p");
    copy.textContent = fieldworkEventCopy(event);
    const provenance = document.createElement("small");
    provenance.textContent = `${event.canonical_effect ? "canonical" : "simulation only"} · event ${String(event.event_id).slice(0, 16)} · ${firstValue(event.actor && event.actor.actor_role, "actor")}`;
    item.append(meta, heading, copy, provenance);
    timeline.appendChild(item);
  });
  const outputs = asArray(projection.outputs);
  byId("fieldworkOutputs").hidden = !outputs.length;
  outputs.forEach((output) => {
    const item = document.createElement("li");
    const name = document.createElement("strong");
    name.textContent = String(firstValue(output.output_id, "Stored output"));
    const detail = document.createElement("small");
    detail.textContent = `Exact stored replay · ${firstValue(output.generator, "generator version unknown")} · hash ${String(firstValue(output.stored_output_hash, "")).slice(0, 12)}`;
    item.append(name, detail);
    outputList.appendChild(item);
  });
  renderSidecarSelection();
}

async function loadFieldworkReplay(options = {}) {
  const cycleId = selectedFieldworkCycle();
  if (!cycleId) {
    renderFieldworkReplay(null);
    return false;
  }
  const asOf = options.asOf !== undefined ? String(options.asOf) : String(byId("fieldworkAsOf").value || "");
  const params = new URLSearchParams({ scale: byId("fieldworkScale").value });
  if (asOf) params.set("as_of_event_id", asOf);
  setFieldworkStatus("Replaying the authorized ledger projection…");
  byId("fieldworkReplayForm").setAttribute("aria-busy", "true");
  try {
    const payload = await api.request(
      `/api/records/${encodeURIComponent(recordId())}/fieldwork/cycles/${encodeURIComponent(cycleId)}/replay?${params}`
    );
    renderFieldworkReplay(payload);
    const returnedEvents = asArray(payload.projection && payload.projection.events);
    if (!asOf || !state.fieldworkEventOptions.length) state.fieldworkEventOptions = returnedEvents;
    updateFieldworkAsOfOptions(state.fieldworkEventOptions, asOf);
    setFieldworkStatus(
      asOf ? "Historical projection replayed under current consent policy." : "Latest canonical projection replayed.",
      true
    );
    return true;
  } catch (error) {
    setFieldworkStatus(error.message);
    return false;
  } finally {
    byId("fieldworkReplayForm").setAttribute("aria-busy", "false");
  }
}

async function handleFieldworkReplay(event) {
  event.preventDefault();
  const replayed = await loadFieldworkReplay();
  if (replayed) {
    void sendProductSignal("fieldwork.replay_used", { scale: byId("fieldworkScale").value });
  }
}

async function openSynthesis(force = false) {
  if (state.cy) {
    state.cy.resize();
    state.cy.fit(undefined, 36);
  }
  if (state.synthesis && !force) {
    renderSynthesis();
    return;
  }
  await generateSynthesis(false);
}

async function generateSynthesis(regenerate) {
  if (state.inflight) return;
  state.inflight = true;
  const buttons = document.querySelectorAll('[data-action="regenerate-map"]');
  buttons.forEach((button) => { button.disabled = true; });
  byId("mapEmpty").hidden = false;
  byId("mapEmpty").querySelector("p").textContent = "Reviewing saved responses and building the map…";
  try {
    const suffix = regenerate ? "/synthesis/regenerate" : "/synthesis";
    const payload = await api.request(`/api/records/${encodeURIComponent(recordId())}${suffix}`, {
      method: "POST",
      body: {},
      idempotent: true
    });
    state.synthesis = payload.synthesis
      ? { ...payload.synthesis, concept_map: payload.concept_map }
      : payload;
    state.annotations = {};
    renderSynthesis();
    await loadPathway(false);
    showToast(regenerate ? "The synthesis map has been regenerated." : "The synthesis is ready.");
  } catch (error) {
    byId("mapEmpty").hidden = false;
    byId("mapEmpty").querySelector("p").textContent = error.message;
  } finally {
    state.inflight = false;
    buttons.forEach((button) => { button.disabled = false; });
  }
}

function synthesisAnalysis() {
  return firstValue(state.synthesis && state.synthesis.analysis, state.synthesis && state.synthesis.key_points, {});
}

function analysisItems(key) {
  const analysis = synthesisAnalysis();
  const aliases = {
    infrastructure: ["infrastructure", "existing_ai_infrastructure", "ai_infrastructure"],
    use_patterns: ["use_patterns", "targeted_use_patterns", "target_use_patterns"],
    context: ["context", "key_context"],
    constraints: ["constraints", "constraint"],
    affordances: ["affordances", "opportunities"]
  };
  const value = aliases[key].map((alias) => analysis && analysis[alias]).find((item) => item !== undefined);
  if (Array.isArray(value)) {
    return value.map((item) => {
      if (typeof item === "string") return item;
      if (item && item.fit) {
        const pattern = String(firstValue(item.pattern, "Use pattern")).replaceAll("_", " ");
        return `${pattern}: ${item.fit}`;
      }
      return String(firstValue(item.text, item.label, item.summary, ""));
    }).filter(Boolean);
  }
  if (typeof value === "string") return value.split(/\n+/).map((item) => item.replace(/^[-•]\s*/, "").trim()).filter(Boolean);
  return [];
}

function renderSynthesis() {
  Object.keys(ANALYSIS_LABELS).forEach((key) => {
    const button = document.querySelector(`[data-analysis-key="${key}"]`);
    button.disabled = analysisItems(key).length === 0;
  });
  selectAnalysis("context");
  renderMap();
  byId("mapEmpty").hidden = normalizeGraph().nodes.length > 0;
}

function selectAnalysis(key) {
  document.querySelectorAll("[data-analysis-key]").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.analysisKey === key);
  });
  byId("analysisDetailTitle").textContent = ANALYSIS_LABELS[key];
  const list = byId("analysisDetailList");
  list.replaceChildren();
  const items = analysisItems(key);
  if (!items.length) {
    const item = document.createElement("li");
    item.textContent = "No saved point appears in this category yet.";
    list.appendChild(item);
    return;
  }
  items.forEach((text) => {
    const item = document.createElement("li");
    item.textContent = text;
    list.appendChild(item);
  });
}

function normalizeGraph() {
  const mapContainer = firstValue(
    state.synthesis && state.synthesis.map,
    state.synthesis && state.synthesis.graph,
    state.synthesis && state.synthesis.concept_map,
    {}
  );
  const graph = firstValue(mapContainer && mapContainer.graph, mapContainer, {});
  const sourceNodes = asArray(firstValue(graph.nodes, state.synthesis && state.synthesis.nodes, []));
  const sourceEdges = asArray(firstValue(graph.edges, state.synthesis && state.synthesis.edges, []));
  const turnById = new Map(asArray(state.record && state.record.turns).map((turn) => [
    String(firstValue(turn.id, "")),
    String(firstValue(turn.content, turn.text, ""))
  ]));
  const nodes = sourceNodes.map((node, index) => {
    const data = node.data || node;
    const evidenceSource = asArray(firstValue(data.evidence, data.supporting_responses, data.sources, data.evidence_ids, []));
    return {
      id: String(firstValue(data.id, data.node_id, `node-${index + 1}`)),
      label: String(firstValue(data.label, data.title, data.name, `Point ${index + 1}`)),
      kind: normalizeNodeKind(firstValue(data.kind, data.type, data.category, "condition")),
      description: String(firstValue(data.description, data.summary, data.detail, "")),
      evidence: evidenceSource
        .map((item) => {
          if (typeof item === "string") return turnById.get(item) || item;
          const evidenceId = String(firstValue(item.id, item.turn_id, ""));
          return String(firstValue(item.text, item.quote, item.summary, turnById.get(evidenceId), ""));
        })
        .filter(Boolean),
      annotation: String(firstValue(
        state.annotations[String(firstValue(data.id, data.node_id, `node-${index + 1}`))] &&
          state.annotations[String(firstValue(data.id, data.node_id, `node-${index + 1}`))].body,
        data.annotation,
        data.annotation_text,
        ""
      ))
    };
  });
  const nodeIds = new Set(nodes.map((node) => node.id));
  const edges = sourceEdges.map((edge, index) => {
    const data = edge.data || edge;
    return {
      id: String(firstValue(data.id, data.edge_id, `edge-${index + 1}`)),
      source: String(firstValue(data.source, data.from, "")),
      target: String(firstValue(data.target, data.to, "")),
      label: String(firstValue(data.label, data.relationship, data.relation, data.type, ""))
    };
  }).filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target));
  return { nodes, edges };
}

function normalizeNodeKind(kind) {
  const value = String(kind || "").toLowerCase().replace(/[\s_-]+/g, " ");
  if (value.includes("decision")) return "decision";
  if (value.includes("path")) return "pathway";
  if (value.includes("potential") || value.includes("next")) return "potential";
  return "condition";
}

function renderMap() {
  const graph = normalizeGraph();
  const select = byId("mapNodeSelect");
  select.replaceChildren();
  graph.nodes.forEach((node) => {
    const option = document.createElement("option");
    option.value = node.id;
    option.textContent = node.label;
    select.appendChild(option);
  });
  if (!graph.nodes.length || typeof window.cytoscape !== "function") {
    if (graph.nodes.length && typeof window.cytoscape !== "function") {
      byId("mapEmpty").hidden = false;
      byId("mapEmpty").querySelector("p").textContent = "The map library could not be loaded. Export JSON to keep the synthesis data.";
    }
    return;
  }
  if (state.cy) state.cy.destroy();
  state.cy = window.cytoscape({
    container: byId("conceptMap"),
    elements: [
      ...graph.nodes.map((node) => ({ data: node })),
      ...graph.edges.map((edge) => ({ data: edge }))
    ],
    layout: {
      name: "cose",
      animate: false,
      padding: 36,
      nodeRepulsion: 9000,
      idealEdgeLength: 120,
      edgeElasticity: 90,
      gravity: .22,
      randomize: true
    },
    minZoom: .25,
    maxZoom: 2.5,
    style: [
      {
        selector: "node",
        style: {
          "background-color": "#ffffff",
          "border-color": "#064bc2",
          "border-width": 1.5,
          "color": "#0b1b42",
          "font-family": "Inter, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif",
          "font-size": 10,
          "height": 58,
          "label": "data(label)",
          "shape": "ellipse",
          "text-halign": "center",
          "text-valign": "center",
          "text-wrap": "wrap",
          "text-max-width": 92,
          "width": 108
        }
      },
      {
        selector: 'node[kind = "decision"]',
        style: { "background-color": "#edf3ff", "border-width": 2, "height": 66, "width": 118 }
      },
      {
        selector: 'node[kind = "pathway"]',
        style: { "background-color": "#eff8f2", "border-color": "#287e4b" }
      },
      {
        selector: 'node[kind = "potential"]',
        style: { "background-color": "#f5f0fb", "border-color": "#7952b3", "shape": "round-rectangle" }
      },
      {
        selector: "node:selected",
        style: { "border-width": 4, "overlay-opacity": 0 }
      },
      {
        selector: "edge",
        style: {
          "curve-style": "bezier",
          "line-color": "#a8b4ca",
          "target-arrow-color": "#a8b4ca",
          "target-arrow-shape": "triangle",
          "arrow-scale": .7,
          "width": 1,
          "label": "data(label)",
          "font-size": 7,
          "color": "#647089",
          "text-background-color": "#ffffff",
          "text-background-opacity": .9,
          "text-background-padding": 2,
          "text-rotation": "autorotate"
        }
      }
    ]
  });
  state.cy.on("tap", "node", (event) => selectNode(event.target.id()));
  const initialId = graph.nodes.some((node) => node.id === state.selectedNodeId)
    ? state.selectedNodeId
    : graph.nodes[0].id;
  selectNode(initialId);
}

function selectNode(id) {
  const graph = normalizeGraph();
  const node = graph.nodes.find((candidate) => candidate.id === id);
  if (!node) return;
  state.selectedNodeId = id;
  if (state.cy) {
    state.cy.$(":selected").unselect();
    state.cy.getElementById(id).select();
  }
  byId("mapNodeSelect").value = id;
  byId("nodeKind").textContent = {
    condition: "Current condition",
    decision: "Decision point",
    pathway: "Possible pathway",
    potential: "Potential"
  }[node.kind];
  byId("nodeTitle").textContent = node.label;
  byId("nodeDescription").textContent = node.description || "This point connects evidence from the saved review.";
  const evidence = byId("nodeEvidence");
  evidence.replaceChildren();
  if (!node.evidence.length) {
    const item = document.createElement("li");
    item.textContent = "No direct response excerpt was attached to this node.";
    evidence.appendChild(item);
  } else {
    node.evidence.slice(0, 6).forEach((text) => {
      const item = document.createElement("li");
      item.textContent = text;
      evidence.appendChild(item);
    });
    if (node.evidence.length > 6) {
      const item = document.createElement("li");
      item.textContent = `${node.evidence.length - 6} more saved responses support this point.`;
      evidence.appendChild(item);
    }
  }
  byId("annotationText").value = node.annotation;
  updateAnnotationCount();
}

function updateAnnotationCount() {
  const text = byId("annotationText").value;
  byId("annotationCount").textContent = `${text.length} / 1000`;
  byId("saveAnnotationButton").disabled = !state.selectedNodeId || !text.trim();
}

async function saveAnnotation(event) {
  event.preventDefault();
  if (!state.selectedNodeId) return;
  const text = byId("annotationText").value.trim();
  if (!text) {
    showToast("Add an annotation before saving.");
    return;
  }
  const button = byId("saveAnnotationButton");
  button.disabled = true;
  try {
    const existing = state.annotations[state.selectedNodeId];
    const path = existing && existing.id
      ? `/api/annotations/${encodeURIComponent(existing.id)}`
      : `/api/records/${encodeURIComponent(recordId())}/annotations`;
    const payload = await api.request(path, {
      method: existing && existing.id ? "PATCH" : "POST",
      body: existing && existing.id
        ? { body: text }
        : {
            concept_map_id: conceptMapId() || undefined,
            target_type: "node",
            target_id: state.selectedNodeId,
            body: text
          },
      idempotent: true
    });
    const annotation = payload.annotation || payload;
    state.annotations[state.selectedNodeId] = {
      id: String(firstValue(annotation.id, existing && existing.id, "")),
      body: String(firstValue(annotation.body, text))
    };
    showToast("Annotation saved.");
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
  }
}

function renderResponseDrawer() {
  const container = byId("responseList");
  container.replaceChildren();
  STAGES.slice(0, reviewStageCount()).forEach((definition, index) => {
    const turns = recordTurns(index).map(normalizeTurn).filter((turn) => turn.content);
    const section = document.createElement("section");
    section.className = "response-stage";
    const heading = document.createElement("h3");
    heading.textContent = index === 0 ? definition.label : `Step ${index}: ${definition.label}`;
    section.appendChild(heading);
    if (!turns.length) {
      const empty = document.createElement("p");
      empty.textContent = "No saved responses.";
      section.appendChild(empty);
    } else {
      const list = document.createElement("dl");
      turns.forEach((turn) => {
        const term = document.createElement("dt");
        term.textContent = turn.role === "user" ? "Your response" : "Guide";
        const detail = document.createElement("dd");
        detail.textContent = turn.content;
        list.append(term, detail);
      });
      section.appendChild(list);
    }
    container.appendChild(section);
  });
  byId("responseDrawer").hidden = false;
  byId("responseDrawer").querySelector("button").focus();
}

function exportSynthesis() {
  if (!state.synthesis) {
    showToast("Generate the synthesis before exporting it.");
    return;
  }
  const output = {
    exported_at: new Date().toISOString(),
    record_id: recordId(),
    title: firstValue(state.record && state.record.title, state.record && state.record.organization_name, "Guided review"),
    synthesis: state.synthesis
  };
  const blob = new Blob([JSON.stringify(output, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `toolkit-synthesis-${recordId() || "record"}.json`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function closeMobileSidebar() {
  byId("toolkitSidebar").classList.remove("is-open");
  byId("sidebarToggle").setAttribute("aria-expanded", "false");
}

function toggleMobileSidebar() {
  const open = byId("toolkitSidebar").classList.toggle("is-open");
  byId("sidebarToggle").setAttribute("aria-expanded", open ? "true" : "false");
}

function initReviewJump() {
  const review = byId("review");
  const button = byId("reviewJump");
  const lastStep = review.querySelector(".review-list > li:last-child");
  const updateDirection = () => {
    const reachedReview = window.scrollY + 80 >= review.offsetTop;
    const lastStepRect = lastStep.getBoundingClientRect();
    const buttonRect = button.getBoundingClientRect();
    const reviewIsClear =
      reachedReview && lastStepRect.bottom <= buttonRect.top - 8;
    const phase = reviewIsClear ? "return" : reachedReview ? "continue" : "review";

    button.dataset.phase = phase;
    button.classList.toggle("is-returning", reviewIsClear);
    button.setAttribute(
      "aria-label",
      phase === "return"
        ? "Return to the introduction"
        : phase === "continue"
          ? "Continue through the review steps"
          : "Go to review the steps"
    );
  };
  window.addEventListener("scroll", updateDirection, { passive: true });
  window.addEventListener("resize", updateDirection, { passive: true });
  updateDirection();
  button.addEventListener("click", () => {
    const phase = button.dataset.phase;
    const behavior = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
    let top;

    if (phase === "return") {
      top = byId("intro").offsetTop;
    } else if (phase === "continue") {
      const lastStepBottom = window.scrollY + lastStep.getBoundingClientRect().bottom;
      const buttonClearance = window.innerHeight - button.getBoundingClientRect().top + 12;
      const maximumScroll = document.documentElement.scrollHeight - window.innerHeight;
      top = Math.min(
        maximumScroll,
        Math.max(review.offsetTop, lastStepBottom - window.innerHeight + buttonClearance)
      );
    } else {
      top = review.offsetTop;
    }

    window.scrollTo({ top, behavior });
  });
}

function initEventHandlers() {
  document.querySelectorAll("[data-auth-open]").forEach((button) => {
    button.addEventListener("click", () => showAuth(button.dataset.authOpen));
  });
  document.addEventListener("click", async (event) => {
    const actionTarget = event.target.closest("[data-action]");
    if (actionTarget) {
      const action = actionTarget.dataset.action;
      if (action === "start-review") await openToolkit();
      if (action === "show-landing") showLanding();
      if (action === "close-auth") closeAuth();
      if (action === "records") await loadRecords(true);
      if (action === "close-records") byId("recordsDialog").close();
      if (action === "new-record") {
        byId("recordsDialog").close();
        showRecordHome();
      }
      if (action === "logout") await logout();
      if (action === "open-fieldwork") await openFieldwork();
      if (action === "focus-pathway") {
        closeMobileSidebar();
        byId("pathwayPanel").scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
        byId("pathwayPanel").focus({ preventScroll: true });
      }
      if (action === "review-responses") renderResponseDrawer();
      if (action === "close-responses") byId("responseDrawer").hidden = true;
      if (action === "regenerate-map") await generateSynthesis(Boolean(state.synthesis));
      if (action === "export-json") exportSynthesis();
      if (action === "account-menu") {
        const menu = byId("accountMenu");
        menu.hidden = !menu.hidden;
      }
    }

    const recordButton = event.target.closest("[data-record-id]");
    if (recordButton) await resumeRecord(recordButton.dataset.recordId);
    const evolutionButton = event.target.closest("[data-evolution-signal]");
    if (evolutionButton && !evolutionButton.disabled) await recordNamePreference(evolutionButton);
    const routeActionButton = event.target.closest("[data-route-action]");
    if (routeActionButton && !routeActionButton.disabled) await handleRouteAction(routeActionButton);
    const stageButton = event.target.closest("[data-stage-index]");
    if (stageButton && !stageButton.disabled) await selectStage(Number(stageButton.dataset.stageIndex), false);
    const analysisButton = event.target.closest("[data-analysis-key]");
    if (analysisButton && !analysisButton.disabled) selectAnalysis(analysisButton.dataset.analysisKey);
    const mapButton = event.target.closest("[data-map-action]");
    if (mapButton && state.cy) {
      if (mapButton.dataset.mapAction === "zoom-in") state.cy.zoom({ level: Math.min(state.cy.zoom() * 1.2, 2.5), renderedPosition: { x: byId("conceptMap").clientWidth / 2, y: byId("conceptMap").clientHeight / 2 } });
      if (mapButton.dataset.mapAction === "zoom-out") state.cy.zoom({ level: Math.max(state.cy.zoom() / 1.2, .25), renderedPosition: { x: byId("conceptMap").clientWidth / 2, y: byId("conceptMap").clientHeight / 2 } });
      if (mapButton.dataset.mapAction === "fit") state.cy.fit(undefined, 36);
    }
  });

  byId("menuButton").addEventListener("click", () => {
    const menu = byId("mobileMenu");
    menu.hidden = !menu.hidden;
    byId("menuButton").setAttribute("aria-expanded", menu.hidden ? "false" : "true");
  });
  byId("sidebarToggle").addEventListener("click", toggleMobileSidebar);
  byId("accountButton").addEventListener("click", () => {
    const menu = byId("accountMenu");
    menu.hidden = !menu.hidden;
    byId("accountButton").setAttribute("aria-expanded", menu.hidden ? "false" : "true");
  });
  byId("loginForm").addEventListener("submit", handleLogin);
  byId("registerForm").addEventListener("submit", handleRegister);
  byId("forgotForm").addEventListener("submit", handleForgot);
  byId("resetForm").addEventListener("submit", handleReset);
  byId("resendVerificationButton").addEventListener("click", resendVerification);
  byId("recordForm").addEventListener("submit", createRecord);
  byId("messageForm").addEventListener("submit", sendStageMessage);
  byId("completeStageButton").addEventListener("click", completeCurrentStage);
  byId("pathwayDecisionForm").addEventListener("submit", handlePathwayDecision);
  byId("fieldworkCycleForm").addEventListener("submit", createFieldworkCycle);
  byId("fieldworkEntryForm").addEventListener("submit", appendFieldworkEntry);
  byId("fieldworkReplayForm").addEventListener("submit", handleFieldworkReplay);
  byId("sidecarForm").addEventListener("submit", sendSidecarMessage);
  byId("clearSidecarButton").addEventListener("click", () => resetSidecarChat("Ephemeral chat cleared."));
  byId("evolutionConsent").addEventListener("change", updateProductEvolutionConsent);
  byId("refreshFieldworkButton").addEventListener("click", () => loadFieldworkCycles(selectedFieldworkCycle()));
  byId("fieldworkCycleSelect").addEventListener("change", async () => {
    state.fieldworkEventOptions = [];
    byId("fieldworkAsOf").value = "";
    resetSidecarChat("The ephemeral chat was cleared because the selected cycle changed.");
    await loadFieldworkReplay({ asOf: "" });
  });
  byId("fieldworkAsOf").addEventListener("change", () => loadFieldworkReplay());
  byId("fieldworkScale").addEventListener("change", async () => {
    resetSidecarChat("The ephemeral chat was cleared because the selected scale changed.");
    await loadFieldworkReplay();
  });
  byId("cancelPathwayDecisionButton").addEventListener("click", () => {
    state.pendingPathwayDecision = null;
    byId("pathwayConfirmDialog").close();
    byId("pathwayRationale").focus();
  });
  byId("confirmPathwayDecisionButton").addEventListener("click", async () => {
    const pending = state.pendingPathwayDecision;
    if (!pending) return;
    byId("pathwayConfirmDialog").close();
    state.pendingPathwayDecision = null;
    await executePathwayDecision(pending.outcome, pending.rationale);
  });
  byId("annotationForm").addEventListener("submit", saveAnnotation);
  byId("annotationText").addEventListener("input", updateAnnotationCount);
  byId("mapNodeSelect").addEventListener("change", (event) => selectNode(event.target.value));
  byId("authDialog").addEventListener("click", (event) => {
    if (event.target === byId("authDialog")) closeAuth();
  });
  byId("recordsDialog").addEventListener("click", (event) => {
    if (event.target === byId("recordsDialog")) byId("recordsDialog").close();
  });
  byId("pathwayConfirmDialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    state.pendingPathwayDecision = null;
    byId("pathwayConfirmDialog").close();
    byId("pathwayRationale").focus();
  });
}

async function init() {
  initEventHandlers();
  initReviewJump();
  const fragment = readLinkFragment();
  await loadProductIdentity();
  await loadStageDefinitions();
  try {
    await refreshSession();
  } catch (error) {
    state.session = { authenticated: false, verified: false, user: null };
  }
  await handleLinkFragment(fragment);
}

document.addEventListener("DOMContentLoaded", init);
