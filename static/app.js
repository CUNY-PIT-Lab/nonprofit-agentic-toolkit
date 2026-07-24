"use strict";

if (window.location.hostname === "zmuhls.github.io") {
  const canonical = "https://toolkit-api-production-535d.up.railway.app/";
  const safeFragment = /^(#(?:verify|reset)\?token=[A-Za-z0-9._~-]+)$/.test(window.location.hash)
    ? window.location.hash
    : "";
  window.location.replace(canonical + safeFragment);
}

const STAGES = [
  {
    key: "entry",
    answers: 4,
    label: "Describe the proposal",
    shortLabel: "Describe the proposal",
    intro: "The guide uses your initial description, then asks one question at a time about purpose, current practice, affected people, ownership, capacity, and reasons to stop."
  },
  {
    key: "redline",
    answers: 5,
    label: "Red line test",
    shortLabel: "Red line test",
    intro: "Set conditions for privacy, consent, human authority, equity, audit, ownership, and organizational capacity."
  },
  {
    key: "stress",
    answers: 4,
    label: "Stress test",
    shortLabel: "Stress test",
    intro: "Examine failure, unsupported output, security, reliability, accessibility, correction, and recourse."
  },
  {
    key: "cost_benefit",
    answers: 4,
    label: "Costs and benefits",
    shortLabel: "Costs and benefits",
    intro: "Compare who benefits, labor, risk, resources, maintenance, and a credible non-AI option."
  },
  {
    key: "hidden_curriculum",
    answers: 4,
    label: "Hidden curriculum",
    shortLabel: "Hidden curriculum",
    intro: "Review what the proposal changes about values, behavior, authority, knowledge, invisible work, and dependence."
  },
  {
    key: "accountability",
    answers: 4,
    label: "Accountability",
    shortLabel: "Accountability",
    intro: "Identify who explains, audits, hears appeals, handles incidents, suspends the system, reviews it, and retires it."
  },
  {
    key: "internal_external_review",
    answers: 4,
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

const ANALYSIS_LABELS = {
  context: "Context",
  constraints: "Constraints",
  affordances: "Affordances",
  infrastructure: "Existing AI infrastructure",
  use_patterns: "Targeted use patterns"
};

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
  authEmail: "",
  resetToken: "",
  inflight: false,
  toastTimer: null
};

const byId = (id) => document.getElementById(id);
const firstValue = (...values) => values.find((value) => value !== undefined && value !== null);
const asArray = (value) => Array.isArray(value) ? value : value ? [value] : [];

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
      renderNavigation();
      await selectStage(nextRecordStage(state.record), false);
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
      const step = Math.min(7, Math.max(0, nextRecordStage(record)));
      detail.textContent = step === 7 ? "Synthesis ready" : `Continue at ${STAGES[step].label}`;
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
    byId("recordsDialog").close();
    renderNavigation();
    await selectStage(nextRecordStage(state.record), false);
  } catch (error) {
    showToast(error.message);
  }
}

function showRecordHome() {
  state.record = null;
  state.synthesis = null;
  byId("recordHome").hidden = false;
  byId("conversationStage").hidden = true;
  byId("synthesisStage").hidden = true;
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
  const stages = firstValue(record.stages, record.stage_records, {});
  if (Array.isArray(stages)) {
    return Object.fromEntries(stages.map((stage, index) => [String(firstValue(stage.key, stage.stage, index)), stage]));
  }
  return stages || {};
}

function stageRecord(index, record = state.record) {
  const stages = stageCollection(record);
  return firstValue(stages[STAGES[index].key], stages[String(index)], stages[index], null);
}

function stageIsComplete(index, record = state.record) {
  const completedSteps = asArray(record && record.completed_steps);
  if (completedSteps.some((step) => String(firstValue(step.stage, step.key, step)) === STAGES[index].key)) return true;
  const stage = stageRecord(index, record);
  if (stage) return ["complete", "completed"].includes(String(stage.status).toLowerCase()) || stage.completed === true || Boolean(stage.completed_at);
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
  for (let index = 0; index < 7; index += 1) {
    if (!stageIsComplete(index, record)) return index;
  }
  return 7;
}

function canOpenStage(index) {
  if (!state.record) return index === 0;
  if (index === 0) return true;
  if (index === 7) return Array.from({ length: 7 }, (_, stage) => stageIsComplete(stage)).every(Boolean);
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
    button.classList.toggle("is-current", Boolean(state.record) && state.currentStage === index);
    button.classList.toggle("is-complete", index < 7 && stageIsComplete(index));
    const number = document.createElement("span");
    number.className = "nav-number";
    number.textContent = index === 0 ? "0" : String(index);
    const label = document.createElement("span");
    label.textContent = stage.label;
    button.append(number, label);
    navigation.appendChild(button);
  });
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
  state.currentStage = index;
  renderNavigation();
  byId("recordHome").hidden = true;
  byId("conversationStage").hidden = index === 7;
  byId("synthesisStage").hidden = index !== 7;
  byId("mobileStageTitle").textContent = STAGES[index].shortLabel;
  closeMobileSidebar();
  window.scrollTo({ top: 0 });
  if (index === 7) {
    await openSynthesis();
  } else {
    await openConversationStage(index, startIfEmpty);
  }
}

function recordTurns(index) {
  const stage = stageRecord(index);
  const direct = firstValue(stage && stage.turns, stage && stage.messages, null);
  if (direct) return asArray(direct);
  const conversations = firstValue(state.record && state.record.conversations, {});
  const grouped = firstValue(conversations && conversations[STAGES[index].key], conversations && conversations[String(index)], null);
  if (grouped) return asArray(grouped);
  const looseTurns = asArray(state.record && state.record.turns).filter((turn) => {
    const stageValue = firstValue(turn.stage, turn.stage_key, turn.step);
    return String(stageValue) === STAGES[index].key || Number(stageValue) === index;
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

function savedUserTurnCount(index = state.currentStage) {
  return asArray(state.record && state.record.turns).filter((turn) => {
    const stageValue = firstValue(turn.stage, turn.stage_key, turn.step);
    const matchesStage = String(stageValue) === STAGES[index].key || Number(stageValue) === index;
    return matchesStage && ["user", "human"].includes(String(firstValue(turn.role, turn.author, "")).toLowerCase());
  }).length;
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
  const seen = new Set(existing.map((turn) => String(firstValue(turn.id, `${turn.stage}:${turn.ordinal}:${turn.role}`))));
  payloadTurns(payload).forEach((turn) => {
    const identity = String(firstValue(turn.id, `${turn.stage}:${turn.ordinal}:${turn.role}`));
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

async function openConversationStage(index, startIfEmpty) {
  const definition = STAGES[index];
  byId("stageKicker").textContent = index === 0 ? "Entry screen" : `Step ${index}`;
  byId("stageTitle").textContent = definition.label;
  byId("stageIntro").textContent = definition.intro;
  byId("completionPanel").hidden = true;
  byId("messageInput").disabled = false;
  byId("sendMessageButton").disabled = false;

  const turns = recordTurns(index);
  renderTurns(turns);
  const stage = stageRecord(index);
  const ready = stage && (stage.ready_to_complete || stage.can_complete || String(stage.status).toLowerCase() === "ready");
  if (ready || savedUserTurnCount(index) >= STAGES[index].answers) {
    showCompletion(firstValue(stage && stage.summary, stage && stage.completion_summary, ""));
  }
  const hasGuideTurn = turns.map(normalizeTurn).some((turn) => turn.role === "assistant");
  if (!hasGuideTurn) await startStage(index);
}

function updateRecordFromPayload(payload) {
  if (payload.record || payload.adoption_record) state.record = normalizeRecord(payload);
  if (payload.stage && typeof payload.stage === "object") {
    const stages = stageCollection();
    if (Array.isArray(state.record.stages)) {
      const match = state.record.stages.findIndex((stage) => String(firstValue(stage.key, stage.stage)) === STAGES[state.currentStage].key);
      if (match >= 0) state.record.stages[match] = payload.stage;
      else state.record.stages.push(payload.stage);
    } else {
      state.record.stages = { ...stages, [STAGES[state.currentStage].key]: payload.stage };
    }
  }
  mergePayloadTurns(payload);
  if (payload.record_text && payload.stage && typeof payload.stage === "string") {
    const completed = asArray(state.record.completed_steps);
    if (!completed.some((step) => String(firstValue(step.stage, step)) === payload.stage)) {
      completed.push({ stage: payload.stage, record_text: payload.record_text });
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
    String(payload.status || "").toLowerCase() === "ready"
  );
}

async function startStage(index) {
  if (state.inflight) return;
  state.inflight = true;
  setConversationBusy(true);
  const thinking = appendMessage("assistant", "", true);
  try {
    const payload = await api.request(
      `/api/records/${encodeURIComponent(recordId())}/stages/${encodeURIComponent(STAGES[index].key)}/start`,
      { method: "POST", body: {}, idempotent: true }
    );
    thinking.remove();
    updateRecordFromPayload(payload);
    const messages = asArray(payload.messages).map(normalizeTurn).filter((turn) => turn.content);
    if (messages.length) {
      messages.forEach((message) => appendMessage(message.role, message.content));
    } else {
      const content = responseMessage(payload);
      if (content) appendMessage("assistant", content);
    }
    if (responseIsReady(payload)) showCompletion(firstValue(payload.summary, payload.completion_summary, ""));
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
  if (state.inflight) return;
  const input = byId("messageInput");
  const content = input.value.trim();
  if (!content) return;
  input.value = "";
  appendMessage("user", content);
  setSaveStatus("Saving…");
  state.inflight = true;
  setConversationBusy(true);
  const thinking = appendMessage("assistant", "", true);
  const idempotencyKey = makeRequestId();
  try {
    const payload = await api.request(
      `/api/records/${encodeURIComponent(recordId())}/stages/${encodeURIComponent(STAGES[state.currentStage].key)}/messages`,
      {
        method: "POST",
        body: { content, idempotency_key: idempotencyKey },
        idempotent: true,
        idempotencyKey
      }
    );
    thinking.remove();
    updateRecordFromPayload(payload);
    const response = responseMessage(payload);
    if (response) appendMessage("assistant", response);
    if (responseIsReady(payload) || savedUserTurnCount() >= STAGES[state.currentStage].answers) {
      showCompletion(firstValue(payload.summary, payload.completion_summary, ""));
    }
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

function setConversationBusy(busy) {
  byId("messageInput").disabled = busy;
  byId("sendMessageButton").disabled = busy;
  byId("conversationStage").setAttribute("aria-busy", busy ? "true" : "false");
  if (!busy) byId("messageInput").focus();
}

function setSaveStatus(message) {
  byId("saveStatus").textContent = message;
}

function showCompletion(summary) {
  byId("completionSummary").textContent = summary || "The guide has enough information to draft this part of the adoption record.";
  byId("completionPanel").hidden = false;
}

async function completeCurrentStage() {
  if (state.inflight) return;
  const button = byId("completeStageButton");
  button.disabled = true;
  setSaveStatus("Saving…");
  try {
    const payload = await api.request(
      `/api/records/${encodeURIComponent(recordId())}/stages/${encodeURIComponent(STAGES[state.currentStage].key)}/complete`,
      { method: "POST", body: {}, idempotent: true }
    );
    updateRecordFromPayload(payload);
    const completedIndex = state.currentStage;
    if (!stageIsComplete(completedIndex)) {
      const stage = stageRecord(completedIndex) || {};
      stage.status = "completed";
      if (!state.record.stages || Array.isArray(state.record.stages)) {
        state.record.stages = { ...stageCollection(), [STAGES[completedIndex].key]: stage };
      } else {
        state.record.stages[STAGES[completedIndex].key] = stage;
      }
    }
    renderNavigation();
    setSaveStatus("Saved");
    showToast(`${STAGES[completedIndex].label} saved.`);
    await selectStage(Math.min(7, completedIndex + 1), false);
  } catch (error) {
    showToast(error.message);
    setSaveStatus("Save failed");
  } finally {
    button.disabled = false;
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
  STAGES.slice(0, 7).forEach((definition, index) => {
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
  const updateDirection = () => {
    const reachedReview = window.scrollY + 80 >= review.offsetTop;
    button.classList.toggle("is-at-review", reachedReview);
    button.setAttribute(
      "aria-label",
      reachedReview ? "Return to the introduction" : "Go to review the steps"
    );
  };
  window.addEventListener("scroll", updateDirection, { passive: true });
  updateDirection();
  button.addEventListener("click", () => {
    const destination = button.classList.contains("is-at-review") ? byId("intro") : review;
    const behavior = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
    window.scrollTo({ top: destination.offsetTop, behavior });
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
  byId("annotationForm").addEventListener("submit", saveAnnotation);
  byId("annotationText").addEventListener("input", updateAnnotationCount);
  byId("mapNodeSelect").addEventListener("change", (event) => selectNode(event.target.value));
  byId("authDialog").addEventListener("click", (event) => {
    if (event.target === byId("authDialog")) closeAuth();
  });
  byId("recordsDialog").addEventListener("click", (event) => {
    if (event.target === byId("recordsDialog")) byId("recordsDialog").close();
  });
}

async function init() {
  initEventHandlers();
  initReviewJump();
  const fragment = readLinkFragment();
  try {
    await refreshSession();
  } catch (error) {
    state.session = { authenticated: false, verified: false, user: null };
  }
  await handleLinkFragment(fragment);
}

document.addEventListener("DOMContentLoaded", init);
