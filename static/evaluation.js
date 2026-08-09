(() => {
  "use strict";

  const byId = (id) => document.getElementById(id);
  const accessView = byId("access-view");
  const workspace = byId("workspace");
  const loginForm = byId("login-form");
  const accessStatus = byId("access-status");
  const accountButton = byId("account-button");
  const accountDialog = byId("account-dialog");
  const accountClose = byId("account-close");
  const accountName = byId("account-name");
  const accountEmail = byId("account-email");
  const logoutButton = byId("logout-button");
  const board = byId("conversation-board");
  const emptyState = byId("empty-state");
  const search = byId("conversation-search");
  const bucketVisibility = byId("bucket-visibility");
  const bucketSort = byId("bucket-sort");
  const bucketLayout = byId("bucket-layout");
  const newBucketButton = byId("new-bucket-button");
  const bucketDialog = byId("bucket-dialog");
  const bucketForm = byId("bucket-form");
  const bucketClose = byId("bucket-close");
  const bucketStatus = byId("bucket-status");
  const transcriptDialog = byId("transcript-dialog");
  const transcriptClose = byId("transcript-close");
  const transcriptTitle = byId("transcript-title");
  const transcriptMeta = byId("transcript-meta");
  const transcript = byId("transcript");
  const reviewNoteForm = byId("review-note-form");
  const reviewNote = byId("review-note");
  const reviewNoteStatus = byId("review-note-status");
  const moveStatus = byId("move-status");

  // Synthetic data is available only on an explicit localhost preview URL.
  // A production hostname can never enter this branch, even with ?preview=1.
  const localPreview = ["127.0.0.1", "localhost"].includes(window.location.hostname)
    && new URLSearchParams(window.location.search).get("preview") === "1";
  const previewStorageKey = "toolkit-evaluation-preview-v1";
  const viewKeyPrefix = "toolkit-evaluation-view-v1";
  const defaultView = { visibility: "all", sort: "default", layout: "compact" };

  const annotationLabels = {
    helpful: "Helpful",
    unclear: "Unclear",
    incorrect: "Incorrect",
    unsafe: "Safety concern",
    other: "Other",
  };

  const state = {
    csrfToken: "",
    session: null,
    status: null,
    buckets: [],
    conversations: [],
    selectedId: "",
    openConversation: null,
    view: { ...defaultView },
    pendingMutations: new Set(),
    operationIds: new Map(),
  };

  const previewBuckets = [
    {
      id: "10000000-0000-4000-8000-000000000001",
      standard_key: "success",
      label: "Success",
      color_key: "green",
      sort_position: 10,
    },
    {
      id: "10000000-0000-4000-8000-000000000002",
      standard_key: "needs-work",
      label: "Needs work",
      color_key: "red",
      sort_position: 20,
    },
    {
      id: "10000000-0000-4000-8000-000000000003",
      standard_key: "handoff",
      label: "Handoff",
      color_key: "blue",
      sort_position: 30,
    },
  ];

  const previewConversations = [
    {
      id: "7b8d3e10-0000-4000-8000-000000000001",
      record_title: "Volunteer scheduling assistant",
      organization_name: "Maple Community Network",
      stage: "redline",
      stage_label: "Red line test",
      cycle_number: 1,
      turn_count: 4,
      bucket_id: null,
      evaluation_version: 0,
      transcript_checksum: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      last_turn_at: "2026-08-08T18:32:00Z",
      note: null,
      annotations: [],
      turns: [
        {
          id: "30000000-0000-4000-8000-000000000001",
          role: "assistant",
          content: "Which information would the scheduling assistant need to use?",
        },
        {
          id: "30000000-0000-4000-8000-000000000002",
          role: "user",
          content: "Shift availability and contact preferences, but not case notes or participant records.",
        },
        {
          id: "30000000-0000-4000-8000-000000000003",
          role: "assistant",
          content: "Who can stop the pilot if those boundaries are not maintained?",
        },
        {
          id: "30000000-0000-4000-8000-000000000004",
          role: "user",
          content: "The program director and the staff data steward can pause it immediately.",
        },
      ],
    },
    {
      id: "4c6e8f20-0000-4000-8000-000000000002",
      record_title: "Public benefits information guide",
      organization_name: "Harbor Resource Center",
      stage: "stress",
      stage_label: "Stress test",
      cycle_number: 2,
      turn_count: 2,
      bucket_id: "10000000-0000-4000-8000-000000000001",
      evaluation_version: 2,
      transcript_checksum: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      last_turn_at: "2026-08-08T17:05:00Z",
      note: "The correction path and staff handoff are explicit.",
      annotations: [
        {
          message_id: "30000000-0000-4000-8000-000000000006",
          category: "helpful",
          note: "Names the human fallback.",
          version: 1,
        },
      ],
      turns: [
        {
          id: "30000000-0000-4000-8000-000000000005",
          role: "assistant",
          content: "What happens when the guide cannot verify a changing eligibility rule?",
        },
        {
          id: "30000000-0000-4000-8000-000000000006",
          role: "user",
          content: "It should say what is uncertain, link to the official source, and offer a staff handoff.",
        },
      ],
    },
    {
      id: "9f2a1c30-0000-4000-8000-000000000003",
      record_title: "Internal grant-drafting support",
      organization_name: "Northside Arts Collective",
      stage: "hidden_curriculum",
      stage_label: "Hidden curriculum",
      cycle_number: 1,
      turn_count: 2,
      bucket_id: "10000000-0000-4000-8000-000000000002",
      evaluation_version: 1,
      transcript_checksum: "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
      last_turn_at: "2026-08-07T16:28:00Z",
      note: null,
      annotations: [],
      turns: [
        {
          id: "30000000-0000-4000-8000-000000000007",
          role: "assistant",
          content: "Whose work could become less visible if drafting is automated?",
        },
        {
          id: "30000000-0000-4000-8000-000000000008",
          role: "user",
          content: "We have not yet spoken with the program staff who assemble the evidence.",
        },
      ],
    },
    {
      id: "6e7f9a40-0000-4000-8000-000000000004",
      record_title: "Participant intake triage",
      organization_name: "East River Services",
      stage: "internal_external_review",
      stage_label: "Internal and external review",
      cycle_number: 1,
      turn_count: 2,
      bucket_id: "10000000-0000-4000-8000-000000000003",
      evaluation_version: 3,
      transcript_checksum: "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
      last_turn_at: "2026-08-06T14:11:00Z",
      note: null,
      annotations: [],
      turns: [
        {
          id: "30000000-0000-4000-8000-000000000009",
          role: "assistant",
          content: "Which affected people have reviewed the proposed intake change?",
        },
        {
          id: "30000000-0000-4000-8000-000000000010",
          role: "user",
          content: "No participant advisory group has reviewed it, so the decision must return to them.",
        },
      ],
    },
  ];

  class ApiError extends Error {
    constructor(message, status, payload) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.payload = payload || {};
      this.current = this.payload.current
        || (typeof this.payload.detail === "object" ? this.payload.detail.current : null)
        || null;
    }
  }

  function element(tagName, className, text) {
    const node = document.createElement(tagName);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function option(value, label, selected = false) {
    const node = document.createElement("option");
    node.value = value;
    node.textContent = label;
    node.selected = selected;
    return node;
  }

  function firstValue(...values) {
    return values.find((value) => value !== undefined && value !== null);
  }

  function asNumber(value, fallback = 0) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function operationId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0"));
    return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
  }

  function operationForIntent(intentKey) {
    if (!state.operationIds.has(intentKey)) {
      state.operationIds.set(intentKey, operationId());
    }
    return state.operationIds.get(intentKey);
  }

  function finishIntent(intentKey, error = null) {
    state.pendingMutations.delete(intentKey);
    // Preserve the operation id only when a transport or 5xx failure leaves
    // the server result uncertain. A retry of that same intent is then exact.
    if (!error || (error.status && error.status < 500)) {
      state.operationIds.delete(intentKey);
    }
  }

  function apiMessage(payload, fallback) {
    const detail = payload && payload.detail;
    if (typeof detail === "string" && detail.trim()) return detail;
    if (detail && typeof detail.message === "string" && detail.message.trim()) return detail.message;
    for (const value of [payload && payload.message, payload && payload.error]) {
      if (typeof value === "string" && value.trim()) return value;
    }
    return fallback;
  }

  async function refreshUnauthenticatedCsrf() {
    try {
      const response = await window.fetch("/api/auth/session", {
        method: "GET",
        headers: new Headers({ Accept: "application/json" }),
        credentials: "same-origin",
        cache: "no-store",
      });
      const payload = response.ok ? await response.json() : {};
      state.csrfToken = payload.csrf_token || "";
    } catch (_error) {
      state.csrfToken = "";
    }
  }

  async function api(path, options = {}) {
    const method = options.method || "GET";
    const headers = new Headers({ Accept: "application/json" });
    if (options.body !== undefined) headers.set("Content-Type", "application/json");
    if (state.csrfToken && !["GET", "HEAD", "OPTIONS"].includes(method)) {
      headers.set("X-CSRF-Token", state.csrfToken);
    }
    const response = await window.fetch(path, {
      method,
      headers,
      credentials: "same-origin",
      cache: "no-store",
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json")
      ? await response.json().catch(() => ({}))
      : {};
    if (!response.ok) {
      const error = new ApiError(apiMessage(payload, "Request failed."), response.status, payload);
      if (response.status === 401) {
        const hadSession = Boolean(state.session);
        clearSensitiveEvaluationState(
          hadSession ? "Your session expired. Sign in again." : "",
          hadSession,
        );
        await refreshUnauthenticatedCsrf();
      }
      throw error;
    }
    return payload;
  }

  function setAccessStatus(message, isError = false) {
    accessStatus.textContent = message || "";
    accessStatus.classList.toggle("is-error", Boolean(isError));
  }

  function showAccess(message = "", isError = false) {
    accessView.hidden = false;
    workspace.hidden = true;
    accountButton.hidden = true;
    setAccessStatus(message, isError);
  }

  function showWorkspace() {
    accessView.hidden = true;
    workspace.hidden = false;
    accountButton.hidden = false;
    accountButton.textContent = firstValue(
      state.session && state.session.display_name,
      state.session && state.session.email,
      "Account",
    );
  }

  function clearSensitiveEvaluationState(message = "", isError = false) {
    for (const dialog of [transcriptDialog, bucketDialog, accountDialog]) {
      if (dialog.open) dialog.close();
    }
    state.session = null;
    state.csrfToken = "";
    state.buckets = [];
    state.conversations = [];
    state.selectedId = "";
    state.openConversation = null;
    state.pendingMutations.clear();
    state.operationIds.clear();
    board.replaceChildren();
    transcript.replaceChildren();
    search.value = "";
    bucketForm.reset();
    bucketStatus.textContent = "";
    bucketStatus.classList.remove("is-error");
    transcriptTitle.textContent = "Conversation";
    transcriptMeta.textContent = "";
    reviewNote.value = "";
    reviewNoteStatus.textContent = "";
    moveStatus.textContent = "";
    accountName.textContent = "";
    accountEmail.textContent = "";
    showAccess(message, isError);
  }

  function shortId(value) {
    return `CV-${String(value || "").replace(/[^a-zA-Z0-9]/g, "").slice(0, 6).toUpperCase()}`;
  }

  function humanizeStage(value) {
    return String(value || "Conversation")
      .replace(/[_-]+/g, " ")
      .replace(/\b\w/g, (character) => character.toUpperCase());
  }

  function normalizeAnnotation(item) {
    return {
      message_id: String(firstValue(item && item.message_id, item && item.turn_id, "")),
      category: String(firstValue(item && item.category, "")),
      note: firstValue(item && item.note, null),
      version: asNumber(firstValue(item && item.version, item && item.annotation_version), 0),
    };
  }

  function normalizeTurn(item) {
    return {
      id: String(firstValue(item && item.id, item && item.message_id, item && item.turn_id, "")),
      role: String(firstValue(item && item.role, "user")),
      content: String(firstValue(item && item.content, item && item.message, "")),
      ordinal: asNumber(firstValue(item && item.ordinal, 0), 0),
    };
  }

  function normalizeConversation(item) {
    const turns = firstValue(item && item.turns, item && item.messages, []);
    const annotations = firstValue(item && item.annotations, []);
    return {
      ...item,
      id: String(firstValue(item && item.id, item && item.conversation_id, item && item.stage_state_id, "")),
      record_title: String(firstValue(
        item && item.record_title,
        item && item.title,
        item && item.page_title,
        "Untitled review",
      )),
      organization_name: String(firstValue(item && item.organization_name, "")),
      stage: String(firstValue(item && item.stage, "conversation")),
      stage_label: String(firstValue(
        item && item.stage_label,
        humanizeStage(item && item.stage),
      )),
      cycle_number: asNumber(firstValue(item && item.cycle_number, item && item.cycle, 1), 1),
      turn_count: asNumber(firstValue(
        item && item.turn_count,
        Array.isArray(turns) ? turns.length : 0,
      ), 0),
      bucket_id: firstValue(item && item.bucket_id, null),
      evaluation_version: asNumber(firstValue(
        item && item.evaluation_version,
        item && item.version,
        0,
      ), 0),
      transcript_checksum: String(firstValue(
        item && item.transcript_checksum,
        item && item.checksum,
        "",
      )),
      last_turn_at: firstValue(item && item.last_turn_at, item && item.updated_at, null),
      note: firstValue(item && item.note, null),
      turns: Array.isArray(turns) ? turns.map(normalizeTurn) : [],
      annotations: Array.isArray(annotations) ? annotations.map(normalizeAnnotation) : [],
    };
  }

  function normalizeBucket(item) {
    return {
      ...item,
      id: String(firstValue(item && item.id, item && item.bucket_id, "")),
      label: String(firstValue(item && item.label, item && item.name, "Bucket")),
      color_key: String(firstValue(item && item.color_key, item && item.color, "blue")),
      standard_key: firstValue(item && item.standard_key, null),
      sort_position: asNumber(firstValue(item && item.sort_position, 0), 0),
      archived_at: firstValue(item && item.archived_at, null),
    };
  }

  function previewSnapshot() {
    return {
      buckets: previewBuckets.map((item) => ({ ...item })),
      conversations: previewConversations.map((item) => normalizeConversation(item)),
    };
  }

  function previewSave() {
    window.localStorage.setItem(previewStorageKey, JSON.stringify({
      buckets: state.buckets,
      conversations: state.conversations,
    }));
  }

  function previewLoad() {
    let saved = null;
    try {
      saved = JSON.parse(window.localStorage.getItem(previewStorageKey) || "null");
    } catch (_error) {
      saved = null;
    }
    const snapshot = saved && Array.isArray(saved.buckets) && Array.isArray(saved.conversations)
      ? saved
      : previewSnapshot();
    state.session = {
      id: "preview-reviewer",
      display_name: "Preview reviewer",
      email: "preview@localhost",
    };
    state.status = { enabled: true, ready: true, preview: true };
    state.buckets = snapshot.buckets.map(normalizeBucket);
    state.conversations = snapshot.conversations.map(normalizeConversation);
    loadViewPreferences();
    showWorkspace();
    renderBoard();
  }

  function viewStorageKey() {
    const identity = firstValue(
      state.session && state.session.id,
      state.session && state.session.email,
      "preview",
    );
    return `${viewKeyPrefix}:${identity}`;
  }

  function loadViewPreferences() {
    let saved = {};
    try {
      saved = JSON.parse(window.localStorage.getItem(viewStorageKey()) || "{}");
    } catch (_error) {
      saved = {};
    }
    state.view = {
      visibility: ["all", "with-conversations", "empty"].includes(saved.visibility)
        ? saved.visibility
        : defaultView.visibility,
      sort: ["default", "name", "count"].includes(saved.sort)
        ? saved.sort
        : defaultView.sort,
      layout: ["comfortable", "compact"].includes(saved.layout)
        ? saved.layout
        : defaultView.layout,
    };
    bucketVisibility.value = state.view.visibility;
    bucketSort.value = state.view.sort;
    bucketLayout.value = state.view.layout;
  }

  function saveViewPreferences() {
    window.localStorage.setItem(viewStorageKey(), JSON.stringify(state.view));
  }

  async function loadWorkspace() {
    if (localPreview) {
      previewLoad();
      return;
    }
    const status = await api("/api/evaluation/status");
    if (status.enabled === false || status.ready === false || status.available === false) {
      throw new ApiError("Evaluation access is not available.", 503, status);
    }
    state.status = status;
    const [bucketPayload, conversationPayload] = await Promise.all([
      api("/api/evaluation/buckets"),
      api("/api/evaluation/conversations?limit=100"),
    ]);
    state.buckets = (bucketPayload.buckets || []).map(normalizeBucket);
    state.conversations = (conversationPayload.conversations || []).map(normalizeConversation);
    loadViewPreferences();
    showWorkspace();
    renderBoard();
  }

  function bucketColumns() {
    return [
      {
        id: null,
        label: "Not yet reviewed",
        color_key: "blue",
        standard_key: "not-reviewed",
        sort_position: -1,
      },
      ...state.buckets
        .filter((item) => !item.archived_at)
        .sort((left, right) => left.sort_position - right.sort_position),
    ];
  }

  function bucketKey(value) {
    return value || "__not_reviewed__";
  }

  function filteredConversations() {
    const query = search.value.trim().toLowerCase();
    if (!query) return state.conversations;
    return state.conversations.filter((item) => [
      shortId(item.id),
      item.record_title,
      item.organization_name,
      item.stage_label,
      `cycle ${item.cycle_number}`,
    ].join(" ").toLowerCase().includes(query));
  }

  function moveSelect(conversation) {
    const select = element("select", "card-move");
    select.setAttribute("aria-label", `Move ${shortId(conversation.id)} to bucket`);
    for (const bucket of bucketColumns()) {
      const value = bucket.id || "";
      select.append(option(
        value,
        bucket.label,
        String(conversation.bucket_id || "") === value,
      ));
    }
    select.addEventListener("change", (event) => {
      moveConversation(conversation.id, event.currentTarget.value || null);
    });
    return select;
  }

  function focusSelectedCard() {
    const selected = Array.from(board.querySelectorAll(".conversation-card"))
      .find((card) => card.dataset.conversationId === state.selectedId);
    if (selected) selected.focus();
  }

  function conversationCard(conversation) {
    const selected = state.selectedId === conversation.id;
    const card = element("article", `conversation-card${selected ? " is-selected" : ""}`);
    card.draggable = true;
    card.tabIndex = 0;
    card.dataset.conversationId = conversation.id;
    card.setAttribute(
      "aria-label",
      `${shortId(conversation.id)}, ${conversation.record_title}, ${conversation.stage_label}, cycle ${conversation.cycle_number}`,
    );

    const handle = element("span", "drag-handle", "⠿");
    handle.setAttribute("aria-hidden", "true");
    card.append(handle);
    card.append(element("p", "conversation-code", shortId(conversation.id)));
    card.append(element("p", "conversation-title", conversation.record_title));
    const metaParts = [conversation.stage_label, `Cycle ${conversation.cycle_number}`];
    if (conversation.turn_count) metaParts.push(`${conversation.turn_count} turns`);
    card.append(element("p", "conversation-meta", metaParts.join(" · ")));

    if (selected) {
      const actions = element("div", "card-actions");
      const openButton = element("button", "open-transcript", "Open transcript");
      openButton.type = "button";
      openButton.addEventListener("click", () => openTranscript(conversation.id));
      actions.append(openButton, moveSelect(conversation));
      card.append(actions);
    } else {
      card.append(moveSelect(conversation));
    }

    card.addEventListener("click", (event) => {
      if (event.target.closest("button, select")) return;
      state.selectedId = conversation.id;
      renderBoard();
    });
    card.addEventListener("keydown", (event) => {
      if (!["Enter", " "].includes(event.key) || event.target.closest("button, select")) return;
      event.preventDefault();
      state.selectedId = conversation.id;
      renderBoard();
      focusSelectedCard();
    });
    card.addEventListener("dragstart", (event) => {
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", conversation.id);
    });
    return card;
  }

  function bucketColumn(bucket, conversations) {
    const section = element("section", "bucket");
    section.dataset.bucketId = bucket.id || "";
    section.dataset.color = ["blue", "green", "violet", "red"].includes(bucket.color_key)
      ? bucket.color_key
      : "blue";
    const headingId = `bucket-${String(bucket.id || "not-reviewed").replace(/[^a-zA-Z0-9_-]/g, "")}`;
    section.setAttribute("aria-labelledby", headingId);

    const header = element("header", "bucket-header");
    const title = element("h2", "", bucket.label);
    title.id = headingId;
    const count = element("span", "bucket-count", conversations.length);
    count.setAttribute("aria-label", `${conversations.length} conversations`);
    header.append(title, count);

    const cards = element("div", "bucket-cards");
    for (const conversation of conversations) cards.append(conversationCard(conversation));
    section.append(header, cards);

    section.addEventListener("dragover", (event) => {
      event.preventDefault();
      section.classList.add("is-drop-target");
    });
    section.addEventListener("dragleave", () => section.classList.remove("is-drop-target"));
    section.addEventListener("drop", (event) => {
      event.preventDefault();
      section.classList.remove("is-drop-target");
      const conversationId = event.dataTransfer.getData("text/plain");
      moveConversation(conversationId, bucket.id || null);
    });
    return section;
  }

  function renderBoard() {
    const conversations = filteredConversations();
    const columns = bucketColumns();
    const counts = new Map(columns.map((bucket) => [
      bucketKey(bucket.id),
      conversations.filter((item) => bucketKey(item.bucket_id) === bucketKey(bucket.id)).length,
    ]));
    let visibleColumns = columns.filter((bucket) => {
      const count = counts.get(bucketKey(bucket.id)) || 0;
      if (state.view.visibility === "with-conversations") return count > 0;
      if (state.view.visibility === "empty") return count === 0;
      return true;
    });
    if (state.view.sort === "name") {
      visibleColumns = [...visibleColumns].sort((left, right) => left.label.localeCompare(right.label));
    } else if (state.view.sort === "count") {
      visibleColumns = [...visibleColumns].sort(
        (left, right) => counts.get(bucketKey(right.id)) - counts.get(bucketKey(left.id)),
      );
    }

    board.dataset.layout = state.view.layout;
    board.replaceChildren();
    for (const bucket of visibleColumns) {
      const items = conversations.filter(
        (conversation) => bucketKey(conversation.bucket_id) === bucketKey(bucket.id),
      );
      board.append(bucketColumn(bucket, items));
    }
    emptyState.hidden = conversations.length > 0 && visibleColumns.length > 0;
  }

  function applyEvaluation(conversation, evaluation) {
    if (!conversation || !evaluation) return;
    if (Object.prototype.hasOwnProperty.call(evaluation, "bucket_id")) {
      conversation.bucket_id = evaluation.bucket_id || null;
    }
    if (Object.prototype.hasOwnProperty.call(evaluation, "note")) {
      conversation.note = evaluation.note || null;
    }
    conversation.evaluation_version = asNumber(firstValue(
      evaluation.evaluation_version,
      evaluation.version,
      conversation.evaluation_version,
    ), conversation.evaluation_version);
    conversation.transcript_checksum = String(firstValue(
      evaluation.transcript_checksum,
      evaluation.checksum,
      conversation.transcript_checksum,
    ));
    if (Object.prototype.hasOwnProperty.call(evaluation, "annotations")) {
      conversation.annotations = Array.isArray(evaluation.annotations)
        ? evaluation.annotations.map(normalizeAnnotation)
        : [];
    }
  }

  function optimisticBody(conversation, stableOperationId = operationId()) {
    return {
      expected_version: asNumber(conversation.evaluation_version, 0),
      expected_transcript_checksum: String(conversation.transcript_checksum || ""),
      operation_id: stableOperationId,
    };
  }

  async function moveConversation(conversationId, bucketId) {
    const conversation = state.conversations.find((item) => item.id === conversationId);
    if (!conversation || (conversation.bucket_id || null) === bucketId) return;
    const intentKey = `placement:${conversationId}:${bucketId || "not-reviewed"}`;
    if (state.pendingMutations.has(intentKey)) return;
    state.pendingMutations.add(intentKey);
    const stableOperationId = operationForIntent(intentKey);
    const previousBucket = conversation.bucket_id || null;
    conversation.bucket_id = bucketId;
    renderBoard();
    try {
      if (localPreview) {
        conversation.evaluation_version += 1;
        previewSave();
      } else {
        const payload = await api(
          `/api/evaluation/conversations/${encodeURIComponent(conversationId)}/placement`,
          {
            method: "PUT",
            body: {
              ...optimisticBody(conversation, stableOperationId),
              bucket_id: bucketId,
            },
          },
        );
        applyEvaluation(conversation, payload.evaluation || payload);
      }
      finishIntent(intentKey);
      renderBoard();
      const destination = bucketColumns().find((bucket) => bucket.id === bucketId);
      moveStatus.textContent = `${shortId(conversationId)} moved to ${destination ? destination.label : "Not yet reviewed"}.`;
    } catch (error) {
      finishIntent(intentKey, error);
      if (error.status === 409 && error.current) {
        applyEvaluation(conversation, error.current);
      } else {
        conversation.bucket_id = previousBucket;
      }
      renderBoard();
      moveStatus.textContent = `Move failed. ${error.message}`;
    }
  }

  function transcriptMetaText(conversation) {
    const parts = [];
    if (conversation.organization_name) parts.push(conversation.organization_name);
    parts.push(conversation.record_title, conversation.stage_label, `Cycle ${conversation.cycle_number}`);
    return parts.join(" · ");
  }

  async function openTranscript(conversationId) {
    const summary = state.conversations.find((item) => item.id === conversationId);
    try {
      let detail;
      if (localPreview) {
        detail = normalizeConversation(summary);
      } else {
        const payload = await api(
          `/api/evaluation/conversations/${encodeURIComponent(conversationId)}`,
        );
        detail = normalizeConversation({ ...summary, ...(payload.conversation || payload) });
      }
      state.openConversation = detail;
      if (summary) applyEvaluation(summary, detail);
      transcriptTitle.textContent = shortId(detail.id);
      transcriptMeta.textContent = transcriptMetaText(detail);
      reviewNote.value = detail.note || "";
      reviewNoteStatus.textContent = "";
      renderTranscript();
      transcriptDialog.showModal();
    } catch (error) {
      moveStatus.textContent = `Transcript could not be opened. ${error.message}`;
    }
  }

  function annotationFor(messageId) {
    return (state.openConversation && state.openConversation.annotations || [])
      .find((item) => item.message_id === messageId) || null;
  }

  function annotationSelect(selected) {
    const select = element("select");
    select.name = "category";
    select.append(option("", "Choose type", !selected));
    for (const [value, label] of Object.entries(annotationLabels)) {
      select.append(option(value, label, value === selected));
    }
    return select;
  }

  function annotationForm(message, annotation) {
    const form = element("form", "annotation-form");
    form.hidden = true;

    const selectLabel = element("label", "", "Annotation type");
    const select = annotationSelect(annotation && annotation.category);
    selectLabel.append(select);

    const noteId = `annotation-${message.id.replace(/[^a-zA-Z0-9_-]/g, "")}`;
    const noteLabel = element("label", "sr-only", "Annotation note");
    noteLabel.htmlFor = noteId;
    const note = element("textarea");
    note.id = noteId;
    note.name = "note";
    note.maxLength = 500;
    note.rows = 2;
    note.placeholder = "Short note (optional)";
    note.value = annotation && annotation.note || "";

    const actions = element("div", "annotation-actions");
    const save = element("button", "button button-secondary", "Save annotation");
    save.type = "submit";
    actions.append(save);
    if (annotation) {
      const remove = element("button", "text-button remove-annotation", "Remove");
      remove.type = "button";
      remove.addEventListener("click", () => saveAnnotation(message.id, form, true));
      actions.append(remove);
    }
    form.append(selectLabel, noteLabel, note, actions);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      saveAnnotation(message.id, form, false);
    });
    return form;
  }

  function transcriptMessage(message) {
    const annotation = annotationFor(message.id);
    const assistant = message.role === "assistant";
    const article = element("article", `message ${assistant ? "assistant" : "user"}`);
    article.dataset.messageId = message.id;
    article.append(element(
      "p",
      "message-role",
      assistant ? "Toolkit guide" : "Organization response",
    ));
    article.append(element("p", "message-content", message.content));

    const toggleLabel = annotation
      ? `Annotated: ${annotationLabels[annotation.category] || "Other"}`
      : "Annotate";
    const toggle = element("button", "annotation-toggle", toggleLabel);
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", "false");
    const form = annotationForm(message, annotation);
    toggle.addEventListener("click", () => {
      form.hidden = !form.hidden;
      toggle.setAttribute("aria-expanded", String(!form.hidden));
    });
    article.append(toggle, form);
    return article;
  }

  function renderTranscript() {
    transcript.replaceChildren();
    const turns = state.openConversation && state.openConversation.turns || [];
    for (const message of turns) transcript.append(transcriptMessage(message));
    if (!turns.length) {
      transcript.append(element("p", "empty-state", "No transcript turns are available."));
    }
  }

  function syncOpenConversation(evaluation) {
    const open = state.openConversation;
    if (!open) return;
    const includesAnnotations = Object.prototype.hasOwnProperty.call(
      evaluation || {},
      "annotations",
    );
    applyEvaluation(open, evaluation);
    const summary = state.conversations.find((item) => item.id === open.id);
    if (summary) applyEvaluation(summary, evaluation);
    if (includesAnnotations) renderTranscript();
  }

  async function saveReviewNote() {
    const conversation = state.openConversation;
    if (!conversation) return;
    const noteValue = reviewNote.value;
    const intentKey = `note:${conversation.id}:${noteValue}`;
    if (state.pendingMutations.has(intentKey)) return;
    state.pendingMutations.add(intentKey);
    const stableOperationId = operationForIntent(intentKey);
    const submit = reviewNoteForm.querySelector("button[type='submit']");
    submit.disabled = true;
    reviewNoteStatus.textContent = "Saving…";
    try {
      let evaluation;
      if (localPreview) {
        evaluation = {
          note: noteValue.trim() || null,
          version: conversation.evaluation_version + 1,
          transcript_checksum: conversation.transcript_checksum,
        };
      } else {
        const payload = await api(
          `/api/evaluation/conversations/${encodeURIComponent(conversation.id)}/note`,
          {
            method: "PUT",
            body: {
              ...optimisticBody(conversation, stableOperationId),
              note: noteValue,
            },
          },
        );
        evaluation = payload.evaluation || payload;
      }
      syncOpenConversation(evaluation);
      if (localPreview) previewSave();
      finishIntent(intentKey);
      reviewNoteStatus.textContent = "Saved";
    } catch (error) {
      finishIntent(intentKey, error);
      if (error.status === 409 && error.current) {
        syncOpenConversation(error.current);
        reviewNote.value = state.openConversation.note || "";
      }
      reviewNoteStatus.textContent = `Not saved. ${error.message}`;
    } finally {
      submit.disabled = false;
    }
  }

  async function saveAnnotation(messageId, form, remove) {
    const conversation = state.openConversation;
    if (!conversation) return;
    const category = remove ? "" : form.elements.category.value;
    const note = remove ? "" : form.elements.note.value;
    const intentKey = `annotation:${conversation.id}:${messageId}:${category}:${note}`;
    if (state.pendingMutations.has(intentKey)) return;
    state.pendingMutations.add(intentKey);
    const stableOperationId = operationForIntent(intentKey);
    for (const control of form.elements) control.disabled = true;
    reviewNoteStatus.textContent = "Saving annotation…";
    try {
      let annotation;
      let evaluation;
      if (localPreview) {
        const nextVersion = conversation.evaluation_version + 1;
        annotation = remove ? null : {
          message_id: messageId,
          category,
          note: note.trim() || null,
          version: nextVersion,
        };
        evaluation = {
          evaluation_version: nextVersion,
          transcript_checksum: conversation.transcript_checksum,
        };
      } else {
        const payload = await api(
          `/api/evaluation/conversations/${encodeURIComponent(conversation.id)}/annotations/${encodeURIComponent(messageId)}`,
          {
            method: "PUT",
            body: {
              category,
              note,
              expected_version: asNumber(conversation.evaluation_version, 0),
              expected_transcript_checksum: conversation.transcript_checksum,
              operation_id: stableOperationId,
            },
          },
        );
        annotation = Object.prototype.hasOwnProperty.call(payload, "annotation")
          ? payload.annotation
          : payload;
        evaluation = payload.evaluation || payload;
        if (
          !Object.prototype.hasOwnProperty.call(evaluation, "evaluation_version")
          && !Object.prototype.hasOwnProperty.call(evaluation, "version")
          && annotation
        ) {
          evaluation = { ...evaluation, evaluation_version: annotation.version };
        }
      }
      applyEvaluation(conversation, evaluation);
      conversation.annotations = conversation.annotations.filter(
        (item) => item.message_id !== messageId,
      );
      if (annotation) conversation.annotations.push(normalizeAnnotation(annotation));
      const summary = state.conversations.find((item) => item.id === conversation.id);
      if (summary) summary.annotations = conversation.annotations.map((item) => ({ ...item }));
      if (localPreview) previewSave();
      finishIntent(intentKey);
      renderTranscript();
      reviewNoteStatus.textContent = annotation ? "Annotation saved" : "Annotation removed";
    } catch (error) {
      finishIntent(intentKey, error);
      if (error.status === 409 && error.current) {
        syncOpenConversation(error.current);
      }
      reviewNoteStatus.textContent = `Annotation not saved. ${error.message}`;
      for (const control of form.elements) control.disabled = false;
    }
  }

  loginForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(loginForm);
    const submit = loginForm.querySelector("button[type='submit']");
    submit.disabled = true;
    setAccessStatus("Signing in…");
    try {
      const payload = await api("/api/auth/login", {
        method: "POST",
        body: { email: form.get("email"), password: form.get("password") },
      });
      state.session = payload.user || payload.account || null;
      state.csrfToken = payload.csrf_token || state.csrfToken;
      loginForm.reset();
      await loadWorkspace();
    } catch (error) {
      setAccessStatus(error.message, true);
    } finally {
      submit.disabled = false;
    }
  });

  search.addEventListener("input", renderBoard);
  bucketVisibility.addEventListener("change", () => {
    state.view.visibility = bucketVisibility.value;
    saveViewPreferences();
    renderBoard();
  });
  bucketSort.addEventListener("change", () => {
    state.view.sort = bucketSort.value;
    saveViewPreferences();
    renderBoard();
  });
  bucketLayout.addEventListener("change", () => {
    state.view.layout = bucketLayout.value;
    saveViewPreferences();
    renderBoard();
  });

  newBucketButton.addEventListener("click", () => {
    bucketStatus.textContent = "";
    bucketStatus.classList.remove("is-error");
    bucketDialog.showModal();
  });
  bucketClose.addEventListener("click", () => bucketDialog.close());
  transcriptClose.addEventListener("click", () => {
    transcriptDialog.close();
    state.openConversation = null;
  });
  reviewNoteForm.addEventListener("submit", (event) => {
    event.preventDefault();
    saveReviewNote();
  });

  accountButton.addEventListener("click", () => {
    accountName.textContent = firstValue(
      state.session && state.session.display_name,
      "Toolkit account",
    );
    accountEmail.textContent = firstValue(state.session && state.session.email, "");
    accountDialog.showModal();
  });
  accountClose.addEventListener("click", () => accountDialog.close());

  bucketForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(bucketForm);
    const label = String(form.get("label") || "").trim();
    const colorKey = String(form.get("color_key") || "blue");
    const intentKey = `bucket:${label}:${colorKey}`;
    if (state.pendingMutations.has(intentKey)) return;
    state.pendingMutations.add(intentKey);
    const stableOperationId = operationForIntent(intentKey);
    const submit = bucketForm.querySelector("button[type='submit']");
    submit.disabled = true;
    bucketStatus.textContent = "Creating…";
    bucketStatus.classList.remove("is-error");
    try {
      let bucket;
      if (localPreview) {
        bucket = normalizeBucket({
          id: operationId(),
          label,
          color_key: colorKey,
          standard_key: null,
          sort_position: Math.max(30, ...state.buckets.map((item) => item.sort_position)) + 10,
        });
        previewSave();
      } else {
        const payload = await api("/api/evaluation/buckets", {
          method: "POST",
          body: { label, color_key: colorKey, operation_id: stableOperationId },
        });
        bucket = normalizeBucket(payload.bucket || payload);
      }
      state.buckets.push(bucket);
      if (localPreview) previewSave();
      finishIntent(intentKey);
      bucketForm.reset();
      bucketDialog.close();
      renderBoard();
    } catch (error) {
      finishIntent(intentKey, error);
      bucketStatus.textContent = error.message;
      bucketStatus.classList.add("is-error");
    } finally {
      submit.disabled = false;
    }
  });

  logoutButton.addEventListener("click", async () => {
    logoutButton.disabled = true;
    try {
      if (!localPreview) {
        await api("/api/auth/logout", { method: "POST", body: {} });
      }
    } catch (_error) {
      // Clear the local view even if the server has already invalidated the session.
    }
    if (localPreview) {
      accountDialog.close();
      previewLoad();
    } else {
      clearSensitiveEvaluationState("Signed out.");
      await refreshUnauthenticatedCsrf();
    }
    logoutButton.disabled = false;
  });

  async function start() {
    if (localPreview) {
      previewLoad();
      return;
    }
    try {
      const payload = await api("/api/auth/session");
      state.csrfToken = payload.csrf_token || "";
      if (!payload.authenticated || !payload.user) {
        showAccess();
        return;
      }
      state.session = payload.user;
      await loadWorkspace();
    } catch (error) {
      if (error.status === 401) {
        showAccess();
      } else if (state.session) {
        showAccess(error.message, true);
      } else {
        showAccess("Evaluation access is temporarily unavailable.", true);
      }
    }
  }

  start();
})();
