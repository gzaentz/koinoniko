const DEFAULT_SYNC_USERNAME = "gavinzaentz";
const SIDEBAR_COLLAPSED_KEY = "koinonikoSidebarCollapsed";
const ASSISTANT_FALLBACK_STARTERS = [
  "What are the best posts to use as Voice Examples?",
  "What patterns are working best?",
  "What should I post about today?",
  "Which scheduled posts feel weakest?",
  "What topics are getting the best engagement?",
  "Which recent post should we make a follow-up to?",
];

const state = {
  view: "dashboard",
  dashboard: null,
  analytics: null,
  status: null,
  account: null,
  voice: null,
  voiceAvoidDraft: [],
  reviewItems: [],
  scheduledItems: [],
  savedTrends: [],
  selectedTrendWindow: "daily",
  selectedPatternWindow: "weekly",
  selectedTrendForCreate: null,
  selectedPatternForCreate: null,
  patternDetail: null,
  draggedQueueId: "",
  queueDetail: null,
  postDetail: null,
  editingPostField: null,
  taxonomy: { topics: [], postTypes: [] },
  recentPostsSort: { key: "date", direction: "desc" },
  assistant: {
    open: false,
    expanded: false,
    prompts: [],
    messages: [],
    loading: false,
    conversationId: "",
  },
  accountDetail: {
    open: false,
    panel: "connections",
  },
};

const DEFAULT_TOPIC_OPTIONS = [
  "Leadpoet",
  "sales LLMs",
  "Bittensor",
  "product",
  "AI SDRs",
  "lead generation",
  "GTM",
  "AI",
  "founder/company",
  "uncategorized",
];
const DEFAULT_POST_TYPE_OPTIONS = [
  "founder_insight",
  "product_update",
  "bittensor",
  "bittensor_update",
  "investor_facing",
  "quote_tweet",
  "launch",
  "technical_explanation",
  "light_funny",
];

const RECENT_POST_SORT_COLUMNS = {
  date: {
    type: "date",
    defaultDirection: "desc",
    get: (post) => post.published_at,
  },
  post: {
    type: "text",
    defaultDirection: "asc",
    get: (post) => post.summary || summarize(post.text),
  },
  topic: {
    type: "text",
    defaultDirection: "asc",
    get: (post) => post.topic,
  },
  type: {
    type: "text",
    defaultDirection: "asc",
    get: (post) => label(post.post_type),
  },
  likes: {
    type: "number",
    defaultDirection: "desc",
    get: (post) => post.metrics?.likes,
  },
  replies: {
    type: "number",
    defaultDirection: "desc",
    get: (post) => post.metrics?.replies,
  },
  reposts: {
    type: "number",
    defaultDirection: "desc",
    get: (post) => post.metrics?.reposts,
  },
  quotes: {
    type: "number",
    defaultDirection: "desc",
    get: (post) => post.metrics?.quotes,
  },
  bookmarks: {
    type: "number",
    defaultDirection: "desc",
    get: (post) => post.metrics?.bookmarks,
  },
  impressions: {
    type: "number",
    defaultDirection: "desc",
    get: (post) => post.metrics?.impressions,
  },
  er: {
    type: "number",
    defaultDirection: "desc",
    get: (post) => post.metrics?.engagement_rate,
  },
  link: {
    type: "text",
    defaultDirection: "asc",
    get: (post) => post.url,
  },
};

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Accept": "application/json",
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  const text = await response.text();
  const contentType = response.headers.get("Content-Type") || "";
  const isJson = contentType.includes("application/json");
  const redirectedToLogin = response.redirected && new URL(response.url, window.location.href).pathname === "/login";
  let payload = {};
  if (text && isJson) {
    try {
      payload = JSON.parse(text);
    } catch (_) {
      throw new Error(`Server returned invalid JSON for ${path}.`);
    }
  } else if (text) {
    if (response.status === 401 || redirectedToLogin) {
      window.location.href = "/login";
      throw new Error("Authentication required.");
    }
    const trimmed = text.trimStart().toLowerCase();
    if (contentType.includes("text/html") || trimmed.startsWith("<!doctype") || trimmed.startsWith("<html")) {
      throw new Error(`Server returned HTML instead of JSON for ${path}. Refresh the page and try again.`);
    }
    throw new Error(`Server returned a non-JSON response for ${path}.`);
  }
  if (response.status === 401 || (response.status === 503 && payload.error && payload.error.includes("login"))) {
    window.location.href = "/login";
    throw new Error(payload.error || "Authentication required.");
  }
  if (!response.ok) {
    throw new Error(payload.error || `Request failed: ${response.status}`);
  }
  return payload;
}

function showMessage(text, isError = false) {
  if (!isError && state.view === "reviewer") {
    clearMessage();
    return;
  }
  const box = $("message");
  box.textContent = text;
  box.classList.toggle("error", isError);
  box.classList.remove("hidden");
  window.setTimeout(() => box.classList.add("hidden"), isError ? 9000 : 5000);
}

function clearMessage() {
  const box = $("message");
  box.textContent = "";
  box.classList.remove("error");
  box.classList.add("hidden");
}

async function runButton(button, loadingText, work) {
  const original = button?.textContent || "";
  const originallyDisabled = Boolean(button?.disabled);
  if (button) {
    button.disabled = true;
    button.textContent = loadingText;
  }
  try {
    await work();
  } catch (error) {
    showMessage(error.message, true);
  } finally {
    if (button) {
      button.disabled = originallyDisabled;
      button.textContent = original;
    }
    if (state.status) updateGlobalControls();
  }
}

async function loadTaxonomy() {
  const [topics, postTypes] = await Promise.all([
    api("/api/taxonomy/topics"),
    api("/api/taxonomy/post-types"),
  ]);
  state.taxonomy = {
    topics: (topics.items || []).map((item) => item.value).filter(Boolean),
    postTypes: (postTypes.items || []).map((item) => item.value).filter(Boolean),
  };
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function escapeAttr(value) {
  return escapeHtml(value).replaceAll("'", "&#39;");
}

function cssAttr(value) {
  return String(value ?? "").replaceAll("\\", "\\\\").replaceAll('"', '\\"');
}

function jsArg(value) {
  return JSON.stringify(String(value ?? ""));
}

function setView(view) {
  state.view = view;
  if (view !== "account") closeAccountDetailDrawer();
  document.querySelectorAll(".view").forEach((node) => node.classList.remove("active"));
  document.querySelectorAll(".nav-button[data-view]").forEach((node) => {
    node.classList.toggle("active", node.dataset.view === view);
  });
  $(`${view}View`).classList.add("active");
  renderCurrentView();
}

function isSidebarCollapsed() {
  return document.documentElement.classList.contains("sidebar-collapsed");
}

function setSidebarCollapsed(collapsed) {
  document.documentElement.classList.toggle("sidebar-collapsed", collapsed);
  const button = $("sidebarToggle");
  if (button) {
    button.setAttribute("aria-expanded", String(!collapsed));
    button.setAttribute("aria-label", collapsed ? "Expand sidebar" : "Collapse sidebar");
    button.title = collapsed ? "Expand sidebar" : "Collapse sidebar";
  }
  try {
    window.localStorage.setItem(SIDEBAR_COLLAPSED_KEY, String(collapsed));
  } catch (_) {}
}

function setupSidebar() {
  setSidebarCollapsed(isSidebarCollapsed());
  $("sidebarToggle")?.addEventListener("click", () => {
    setSidebarCollapsed(!isSidebarCollapsed());
  });
}

async function refresh() {
  state.status = await api("/api/status");
  const [dashboard, analytics, review, scheduled] = await Promise.all([
    api("/api/dashboard"),
    api("/api/analytics"),
    loadQueueStatuses(["draft", "needs_review", "failed"]),
    api("/api/queue?limit=100&status=approved"),
    loadTaxonomy(),
  ]);
  state.dashboard = dashboard;
  state.analytics = analytics;
  state.reviewItems = review;
  state.scheduledItems = scheduled.items || [];
  updateGlobalControls();
  renderCurrentView();
}

async function loadQueueStatuses(statuses) {
  const payloads = await Promise.all(
    statuses.map((status) => api(`/api/queue?limit=100&status=${encodeURIComponent(status)}`))
  );
  return payloads.flatMap((payload) => payload.items || []);
}

function renderCurrentView() {
  if (state.view === "dashboard") renderDashboard();
  if (state.view === "reviewer") renderReviewer();
  if (state.view === "voice") loadVoice().catch((error) => showMessage(error.message, true));
  if (state.view === "account") loadAccount().catch((error) => showMessage(error.message, true));
}

function updateGlobalControls() {
  const reviewCount = state.reviewItems.length;
  $("reviewNavBadge").textContent = String(reviewCount);
  $("reviewNavBadge").classList.toggle("muted-badge", reviewCount === 0);

  [$("reviewAddButton"), $("generatePostButton")].forEach((button) => {
    button.disabled = !state.status?.provider_configured;
    button.title = state.status?.provider_configured ? "" : "Provider-backed generation is not connected.";
  });

  ["accountSyncPostsButton", "accountSyncMetricsButton", "accountSyncAllButton"].forEach((id) => {
    const button = $(id);
    if (!button) return;
    button.disabled = !state.status?.x_read_configured;
    button.title = state.status?.x_read_configured ? "" : "X read auth is not connected.";
  });
}

function renderDashboard() {
  renderRecentPosts();
  renderDashboardTrends();
  renderDashboardPatterns();
}

function renderRecentPosts() {
  const tbody = $("recentPostsTable");
  renderRecentPostSortHeaders();
  const posts = filteredRecentPosts();
  if (!posts.length) {
    tbody.innerHTML = `
      <tr>
        <td colspan="12" class="empty-cell">No posts yet. Sync X posts to import your history.</td>
      </tr>
    `;
    return;
  }
  tbody.innerHTML = posts.map(recentPostRow).join("");
}

function filteredRecentPosts() {
  const search = $("postSearch").value.trim().toLowerCase();
  const voiceFilter = $("postVoiceFilter").value;
  const from = $("postDateFrom").value;
  const to = $("postDateTo").value;
  const filtered = (state.dashboard?.recent_posts || []).filter((post) => {
    if (voiceFilter === "voice" && !post.voice?.is_voice_example) return false;
    const haystack = [post.text, post.summary, post.topic, post.post_type].join(" ").toLowerCase();
    if (search && !haystack.includes(search)) return false;
    const dateText = String(post.published_at || "").slice(0, 10);
    if (from && dateText && dateText < from) return false;
    if (to && dateText && dateText > to) return false;
    return true;
  });
  return sortRecentPosts(filtered);
}

function setRecentPostsSort(key) {
  const column = RECENT_POST_SORT_COLUMNS[key];
  if (!column) return;
  const isCurrentColumn = state.recentPostsSort.key === key;
  state.recentPostsSort = {
    key,
    direction: isCurrentColumn
      ? (state.recentPostsSort.direction === "asc" ? "desc" : "asc")
      : column.defaultDirection,
  };
  renderRecentPosts();
}

function sortRecentPosts(posts) {
  const { key, direction } = state.recentPostsSort;
  return [...posts].sort((first, second) => {
    const primary = compareRecentPosts(first, second, key, direction);
    if (primary) return primary;
    if (key !== "date") {
      const fallback = compareRecentPosts(first, second, "date", "desc");
      if (fallback) return fallback;
    }
    return String(first.id || "").localeCompare(String(second.id || ""));
  });
}

function compareRecentPosts(first, second, key, direction) {
  const column = RECENT_POST_SORT_COLUMNS[key] || RECENT_POST_SORT_COLUMNS.date;
  const firstValue = column.get(first);
  const secondValue = column.get(second);
  const firstMissing = isMissingSortValue(firstValue, column.type);
  const secondMissing = isMissingSortValue(secondValue, column.type);
  if (firstMissing && secondMissing) return 0;
  if (firstMissing) return 1;
  if (secondMissing) return -1;

  let comparison = 0;
  if (column.type === "number") {
    comparison = Number(firstValue) - Number(secondValue);
  } else if (column.type === "date") {
    comparison = Date.parse(firstValue) - Date.parse(secondValue);
  } else {
    comparison = String(firstValue).localeCompare(String(secondValue), undefined, {
      numeric: true,
      sensitivity: "base",
    });
  }
  if (comparison === 0) return 0;
  return direction === "asc" ? comparison : -comparison;
}

function isMissingSortValue(value, type) {
  if (type === "number") return value === null || value === undefined || value === "" || !Number.isFinite(Number(value));
  if (type === "date") return !Number.isFinite(Date.parse(value));
  return value === null || value === undefined || String(value).trim() === "";
}

function renderRecentPostSortHeaders() {
  document.querySelectorAll(".posts-table th[data-sort-key]").forEach((header) => {
    const active = header.dataset.sortKey === state.recentPostsSort.key;
    header.classList.toggle("is-sorted", active);
    header.classList.toggle("sort-asc", active && state.recentPostsSort.direction === "asc");
    header.classList.toggle("sort-desc", active && state.recentPostsSort.direction === "desc");
    header.setAttribute(
      "aria-sort",
      active ? (state.recentPostsSort.direction === "asc" ? "ascending" : "descending") : "none"
    );
  });
}

function recentPostRow(post) {
  const metrics = post.metrics || {};
  const voiceBadge = post.voice?.is_voice_example ? `<span class="voice-badge">Voice</span>` : "";
  return `
    <tr onclick='openPostDetails(${jsArg(post.id)})'>
      <td>${escapeHtml(formatDate(post.published_at))}</td>
      <td class="post-summary"><span>${escapeHtml(post.summary || summarize(post.text))}</span>${voiceBadge}</td>
      <td class="topic-cell">${postFieldControl(post, "topic", "row")}</td>
      <td class="post-type-cell">${postFieldControl(post, "post_type", "row")}</td>
      <td>${metric(metrics.likes)}</td>
      <td>${metric(metrics.replies)}</td>
      <td>${metric(metrics.reposts)}</td>
      <td>${metric(metrics.quotes)}</td>
      <td>${metric(metrics.bookmarks)}</td>
      <td>${metric(metrics.impressions)}</td>
      <td>${formatRate(metrics.engagement_rate)}</td>
      <td>${post.url ? `<a href="${escapeAttr(post.url)}" target="_blank" rel="noreferrer" onclick="event.stopPropagation()">Open</a>` : ""}</td>
    </tr>
  `;
}

function topicOptions() {
  const seen = new Set();
  const options = [];
  const add = (value) => {
    const topic = String(value || "").trim();
    const key = topic.toLowerCase();
    if (!topic || seen.has(key)) return;
    seen.add(key);
    options.push(topic);
  };
  DEFAULT_TOPIC_OPTIONS.forEach(add);
  (state.taxonomy.topics || []).forEach(add);
  (state.dashboard?.recent_posts || [])
    .map((post) => post.topic)
    .filter(Boolean)
    .sort((first, second) => String(first).localeCompare(String(second), undefined, { sensitivity: "base" }))
    .forEach(add);
  return options;
}

function postTypeOptions() {
  const seen = new Set();
  const options = [];
  const add = (value) => {
    const type = String(value || "").trim();
    const key = type.toLowerCase();
    if (!type || seen.has(key)) return;
    seen.add(key);
    options.push(type);
  };
  DEFAULT_POST_TYPE_OPTIONS.forEach(add);
  (state.taxonomy.postTypes || []).forEach(add);
  (state.dashboard?.recent_posts || [])
    .map((post) => post.post_type)
    .filter(Boolean)
    .sort((first, second) => String(first).localeCompare(String(second), undefined, { sensitivity: "base" }))
    .forEach(add);
  return options;
}

function topicLabel(value) {
  const topic = String(value || "").trim();
  if (!topic) return "";
  if (topic.toLowerCase() === "uncategorized") return "Uncategorized";
  return topic
    .split("/")
    .map((part) => label(part))
    .join("/");
}

function updateRecentPost(updatedPost) {
  if (!updatedPost || !state.dashboard?.recent_posts) return;
  state.dashboard.recent_posts = state.dashboard.recent_posts.map((post) =>
    post.id === updatedPost.id ? updatedPost : post
  );
}

function findRecentPost(id) {
  return (state.dashboard?.recent_posts || []).find((post) => post.id === id);
}

window.openPostDetails = (id) => {
  runButton(null, "", async () => {
    const [payload] = await Promise.all([
      api(`/api/posts/${encodeURIComponent(id)}`),
      loadTaxonomy(),
    ]);
    state.postDetail = payload.post;
    renderPostModal();
    openModal("postModal");
  });
};

function renderPostModal() {
  const post = state.postDetail;
  if (!post) return;
  const metrics = post.metrics || {};
  const voice = post.voice || {};
  $("postModalTitle").textContent = post.summary || "Post details";
  $("postModalBody").innerHTML = `
    <div class="post-review-top">
      <div class="full-text">${escapeHtml(post.text)}</div>
      ${post.url ? `<a class="button-link" href="${escapeAttr(post.url)}" target="_blank" rel="noreferrer">Open on X</a>` : ""}
    </div>
    <div class="compact-meta-row">
      ${compactMeta("Date", formatDateTime(post.published_at))}
      ${compactMetaHtml("Topic", postFieldControl(post, "topic", "modal"))}
      ${compactMetaHtml("Post type", postFieldControl(post, "post_type", "modal"))}
      ${compactMeta("Engagement rate", formatRate(metrics.engagement_rate))}
    </div>
    <div class="compact-metrics-row" aria-label="Post metrics">
      ${compactMetric("Likes", metrics.likes)}
      ${compactMetric("Replies", metrics.replies)}
      ${compactMetric("Reposts", metrics.reposts)}
      ${compactMetric("Quotes", metrics.quotes)}
      ${compactMetric("Bookmarks", metrics.bookmarks)}
      ${compactMetric("Impressions", metrics.impressions)}
    </div>
    <section class="voice-edit-section">
      <label class="inline-check">
        <input id="postVoiceExample" type="checkbox" onchange="savePostVoiceFromModal()" ${voice.is_voice_example ? "checked" : ""}>
        Use this as a Voice Example
      </label>
      <label id="postVoiceReasonField" class="voice-reason-field ${voice.is_voice_example ? "" : "hidden"}">
        Why keep this as a voice example?
        <textarea id="postWhyWorked" rows="3" placeholder="Optional note for what Koinoniko should learn from this post." onblur="savePostVoiceFromModal({ quiet: true })">${escapeHtml(voice.why_this_worked || "")}</textarea>
      </label>
      <span id="postVoiceSaveStatus" class="inline-save-status" aria-live="polite"></span>
    </section>
  `;
}

function compactMeta(labelText, value) {
  return `
    <div class="compact-meta-item">
      <span>${escapeHtml(labelText)}</span>
      <strong>${escapeHtml(value ?? "")}</strong>
    </div>
  `;
}

function compactMetaHtml(labelText, htmlValue) {
  return `
    <div class="compact-meta-item">
      <span>${escapeHtml(labelText)}</span>
      ${htmlValue}
    </div>
  `;
}

function compactMetric(labelText, value) {
  return `<span><strong>${metric(value)}</strong> ${escapeHtml(labelText)}</span>`;
}

function postFieldControl(post, field, context = "row") {
  const isEditing =
    state.editingPostField?.postId === post.id &&
    state.editingPostField?.field === field &&
    state.editingPostField?.context === context;
  const currentValue = field === "topic" ? (post.topic || "uncategorized") : (post.post_type || "");
  const query = isEditing ? state.editingPostField.query ?? currentValue : currentValue;
  const displayValue = field === "topic" ? topicLabel(currentValue) : label(currentValue);
  const fieldName = field === "topic" ? "Topic" : "Post type";
  if (isEditing) {
    return `
      <div class="field-combo" onclick="event.stopPropagation()" data-field-editor-post="${escapeAttr(post.id)}" data-field-editor-field="${escapeAttr(field)}" data-field-editor-context="${escapeAttr(context)}">
        <input
          class="field-combo-input"
          value="${escapeAttr(query)}"
          placeholder="${escapeAttr(fieldName)}"
          aria-label="${escapeAttr(fieldName)}"
          aria-expanded="true"
          aria-controls="${escapeAttr(fieldMenuId(post.id, field, context))}"
          data-field-editor-input
          data-field-editor-post="${escapeAttr(post.id)}"
          data-field-editor-field="${escapeAttr(field)}"
          data-field-editor-context="${escapeAttr(context)}"
          oninput='updatePostFieldQuery(event, ${jsArg(post.id)}, ${jsArg(field)}, ${jsArg(context)})'
          onkeydown='handlePostFieldKeydown(event, ${jsArg(post.id)}, ${jsArg(field)}, ${jsArg(context)})'
        >
        <div class="field-combo-menu" id="${escapeAttr(fieldMenuId(post.id, field, context))}" role="listbox">
          ${postFieldOptionRows(post.id, field, query, Boolean(state.editingPostField?.dirty), context)}
        </div>
      </div>
    `;
  }
  return `
    <button
      class="inline-field-chip${field === "topic" && currentValue.toLowerCase() === "uncategorized" ? " is-uncategorized" : ""}"
      type="button"
      title="Edit ${escapeAttr(fieldName)}"
      onclick='startPostFieldEdit(event, ${jsArg(post.id)}, ${jsArg(field)}, ${jsArg(context)})'
    >
      ${escapeHtml(displayValue || "Unset")}
    </button>
  `;
}

window.startPostFieldEdit = (event, id, field, context = "row") => {
  event.stopPropagation();
  closeTransientDropdowns({ keepPostFieldEditor: true });
  const post = findRecentPost(id) || state.postDetail;
  const currentValue = field === "topic" ? (post?.topic || "uncategorized") : (post?.post_type || "");
  state.editingPostField = { postId: id, field, context, query: currentValue, dirty: false };
  renderRecentPosts();
  renderPostModal();
  focusPostFieldEditor(id, field, context);
};

window.cancelPostFieldEdit = (event) => {
  event?.stopPropagation?.();
  state.editingPostField = null;
  renderRecentPosts();
  renderPostModal();
};

window.updatePostFieldQuery = (event, id, field, context = "row") => {
  event.stopPropagation();
  if (
    !state.editingPostField ||
    state.editingPostField.postId !== id ||
    state.editingPostField.field !== field ||
    state.editingPostField.context !== context
  ) {
    return;
  }
  state.editingPostField = { ...state.editingPostField, query: event.currentTarget.value, dirty: true };
  const menu = $(fieldMenuId(id, field, context));
  if (menu) {
    menu.innerHTML = postFieldOptionRows(id, field, event.currentTarget.value, true, context);
  }
};

window.handlePostFieldKeydown = (event, id, field, context = "row") => {
  if (event.key === "Escape") {
    event.preventDefault();
    window.cancelPostFieldEdit(event);
    return;
  }
  if (event.key !== "Enter") return;
  event.preventDefault();
  event.stopPropagation();
  savePostFieldValue(id, field, event.currentTarget.value, context);
};

window.choosePostFieldOption = (event, id, field, value, context = "row") => {
  event.preventDefault();
  event.stopPropagation();
  savePostFieldValue(id, field, value, context);
};

async function savePostFieldValue(id, field, rawValue, context = "row") {
  const value = String(rawValue || "").trim();
  if (!value) {
    showMessage(field === "topic" ? "Topic is required." : "Post type is required.", true);
    return;
  }
  setPostFieldEditorDisabled(id, field, context, true);
  try {
    const payload = await api(`/api/posts/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({ [field]: value }),
    });
    updateRecentPost(payload.post);
    if (state.postDetail?.id === id) {
      state.postDetail = payload.post;
    }
    state.editingPostField = null;
    await loadTaxonomy();
    renderRecentPosts();
    renderPostModal();
    showMessage(field === "topic" ? "Topic updated." : "Post type updated.");
  } catch (error) {
    showMessage(error.message, true);
    setPostFieldEditorDisabled(id, field, context, false);
  }
}

function postFieldOptionRows(id, field, query, filterOptions, context = "row") {
  const typed = String(query || "").trim();
  const normalizedQuery = typed.toLowerCase();
  const options = postFieldOptions(field).filter((option) => {
    if (!filterOptions || !normalizedQuery) return true;
    return option.value.toLowerCase().includes(normalizedQuery) || option.label.toLowerCase().includes(normalizedQuery);
  });
  const rows = options.map((option) => postFieldOptionRow(id, field, option, context));
  if (typed) {
    rows.push(`
      <button
        class="field-combo-option create-option"
        type="button"
        role="option"
        onmousedown="event.preventDefault()"
        onclick="choosePostFieldOption(event, ${escapeAttr(jsArg(id))}, ${escapeAttr(jsArg(field))}, ${escapeAttr(jsArg(typed))}, ${escapeAttr(jsArg(context))})"
      >
        <strong>Use "${escapeHtml(typed)}"</strong>
        <span>Save exact text</span>
      </button>
    `);
  }
  if (!rows.length) {
    return `<div class="field-combo-empty">Type to create a new ${field === "topic" ? "topic" : "post type"}.</div>`;
  }
  return rows.join("");
}

function postFieldOptionRow(id, field, option, context = "row") {
  return `
    <button
      class="field-combo-option"
      type="button"
      role="option"
      onmousedown="event.preventDefault()"
      onclick="choosePostFieldOption(event, ${escapeAttr(jsArg(id))}, ${escapeAttr(jsArg(field))}, ${escapeAttr(jsArg(option.value))}, ${escapeAttr(jsArg(context))})"
    >
      <strong>${escapeHtml(option.label)}</strong>
    </button>
  `;
}

function postFieldOptions(field) {
  const values = field === "topic" ? topicOptions() : postTypeOptions();
  return values.map((value) => ({
    value,
    label: field === "topic" ? topicLabel(value) : label(value),
  }));
}

function createPostFieldInputId(field) {
  return field === "topic" ? "createPostTopic" : "createPostType";
}

function createPostFieldMenuId(field) {
  return field === "topic" ? "createPostTopicMenu" : "createPostTypeMenu";
}

function createPostFieldName(field) {
  return field === "topic" ? "topic" : "post type";
}

function createPostFieldOptionRows(field, query, filterOptions) {
  const typed = String(query || "").trim();
  const normalizedQuery = typed.toLowerCase();
  const options = postFieldOptions(field).filter((option) => {
    if (!filterOptions || !normalizedQuery) return true;
    return option.value.toLowerCase().includes(normalizedQuery) || option.label.toLowerCase().includes(normalizedQuery);
  });
  const rows = options.map((option) => `
    <button
      class="field-combo-option"
      type="button"
      role="option"
      onmousedown="event.preventDefault()"
      onclick="chooseCreatePostFieldOption(event, ${escapeAttr(jsArg(field))}, ${escapeAttr(jsArg(option.value))})"
    >
      <strong>${escapeHtml(option.label)}</strong>
    </button>
  `);
  if (typed) {
    rows.push(`
      <button
        class="field-combo-option create-option"
        type="button"
        role="option"
        onmousedown="event.preventDefault()"
        onclick="chooseCreatePostFieldOption(event, ${escapeAttr(jsArg(field))}, ${escapeAttr(jsArg(typed))})"
      >
        <strong>Use "${escapeHtml(typed)}"</strong>
        <span>Use exact text</span>
      </button>
    `);
  }
  if (!rows.length) {
    return `<div class="field-combo-empty">Type to create a new ${createPostFieldName(field)}.</div>`;
  }
  return rows.join("");
}

function renderCreatePostFieldMenu(field, filterOptions = false) {
  const input = $(createPostFieldInputId(field));
  const menu = $(createPostFieldMenuId(field));
  if (!input || !menu) return;
  menu.innerHTML = createPostFieldOptionRows(field, input.value, filterOptions);
}

function openCreatePostFieldMenu(field, filterOptions = false) {
  closeTransientDropdowns({ exceptCreatePostField: field });
  renderCreatePostFieldMenu(field, filterOptions);
  const input = $(createPostFieldInputId(field));
  const menu = $(createPostFieldMenuId(field));
  if (!input || !menu) return;
  menu.classList.remove("hidden");
  input.setAttribute("aria-expanded", "true");
}

function closeCreatePostFieldMenus(exceptField = "") {
  if (typeof exceptField !== "string") exceptField = "";
  ["topic", "post_type"].forEach((field) => {
    if (field === exceptField) return;
    const input = $(createPostFieldInputId(field));
    const menu = $(createPostFieldMenuId(field));
    if (menu) menu.classList.add("hidden");
    if (input) input.setAttribute("aria-expanded", "false");
  });
}

function setCreatePostFieldValue(field, rawValue) {
  const value = String(rawValue || "").trim();
  if (!value) {
    showMessage(field === "topic" ? "Topic is required." : "Post type is required.", true);
    return;
  }
  $(createPostFieldInputId(field)).value = value;
  closeCreatePostFieldMenus();
}

window.chooseCreatePostFieldOption = (event, field, value) => {
  event.preventDefault();
  event.stopPropagation();
  setCreatePostFieldValue(field, value);
};

function updateCreatePostFieldQuery(event, field) {
  event.stopPropagation();
  openCreatePostFieldMenu(field, true);
}

function handleCreatePostFieldKeydown(event, field) {
  if (event.key === "Escape") {
    event.preventDefault();
    event.stopPropagation();
    closeCreatePostFieldMenus();
    return;
  }
  if (event.key !== "Enter") return;
  event.preventDefault();
  event.stopPropagation();
  setCreatePostFieldValue(field, event.currentTarget.value);
}

function setupCreatePostFieldCombo(field) {
  const input = $(createPostFieldInputId(field));
  const combo = document.querySelector(`[data-create-post-field="${cssAttr(field)}"]`);
  if (!input || !combo) return;
  combo.addEventListener("click", (event) => {
    event.stopPropagation();
    openCreatePostFieldMenu(field, false);
    if (event.target !== input) input.focus();
  });
  input.addEventListener("focus", (event) => {
    event.stopPropagation();
    openCreatePostFieldMenu(field, false);
  });
  input.addEventListener("input", (event) => updateCreatePostFieldQuery(event, field));
  input.addEventListener("keydown", (event) => handleCreatePostFieldKeydown(event, field));
}

function fieldMenuId(id, field, context = "row") {
  return `field-menu-${id}-${field}-${context}`;
}

function focusPostFieldEditor(id, field, context = "row") {
  window.requestAnimationFrame(() => {
    const input = document.querySelector(
      `[data-field-editor-input][data-field-editor-post="${cssAttr(id)}"][data-field-editor-field="${cssAttr(field)}"][data-field-editor-context="${cssAttr(context)}"]`
    );
    if (!input) return;
    input.focus();
    input.select();
  });
}

function setPostFieldEditorDisabled(id, field, context = "row", disabled) {
  document
    .querySelectorAll(`[data-field-editor-post="${cssAttr(id)}"][data-field-editor-field="${cssAttr(field)}"][data-field-editor-context="${cssAttr(context)}"] input, [data-field-editor-post="${cssAttr(id)}"][data-field-editor-field="${cssAttr(field)}"][data-field-editor-context="${cssAttr(context)}"] button`)
    .forEach((node) => {
      node.disabled = disabled;
    });
}

function closePostFieldEditor() {
  if (!state.editingPostField) return;
  state.editingPostField = null;
  renderRecentPosts();
  if (state.postDetail && !$("postModal").classList.contains("hidden")) {
    renderPostModal();
  }
}

window.togglePostVoiceReason = () => {
  const checkbox = $("postVoiceExample");
  const field = $("postVoiceReasonField");
  if (!checkbox || !field) return;
  field.classList.toggle("hidden", !checkbox.checked);
};

window.savePostVoiceFromModal = async ({ quiet = false } = {}) => {
  window.togglePostVoiceReason();
  await savePostVoice({ quiet });
};

async function savePostVoice({ quiet = false } = {}) {
  const checkbox = $("postVoiceExample");
  if (!state.postDetail || !checkbox) return;
  const originallyDisabled = checkbox.disabled;
  checkbox.disabled = true;
  setPostVoiceSaveStatus("Saving...");
  try {
    const post = state.postDetail;
    const payload = await api(`/api/posts/${encodeURIComponent(post.id)}/voice`, {
      method: "POST",
      body: JSON.stringify({
        voice_example: checkbox.checked,
        why_this_worked: $("postWhyWorked")?.value || "",
      }),
    });
    state.postDetail = payload.post;
    updateRecentPost(payload.post);
    setPostVoiceSaveStatus("Saved");
    if (!quiet) showMessage(payload.message || "Voice settings saved.");
    await refresh();
    if (state.view === "voice") await loadVoice();
  } catch (error) {
    setPostVoiceSaveStatus("Could not save", true);
    showMessage(error.message, true);
  } finally {
    checkbox.disabled = originallyDisabled;
  }
}

function setPostVoiceSaveStatus(text, isError = false) {
  const status = $("postVoiceSaveStatus");
  if (!status) return;
  status.textContent = text;
  status.classList.toggle("error", isError);
}

function renderDashboardTrends() {
  const groups = state.dashboard?.trend_groups || {};
  $("trendDailyButton").classList.toggle("active", state.selectedTrendWindow === "daily");
  $("trendWeeklyButton").classList.toggle("active", state.selectedTrendWindow === "weekly");
  const trends = groups[state.selectedTrendWindow] || [];
  renderTrendList("trendList", trends, `No ${state.selectedTrendWindow} trends loaded yet.`);
}

function renderTrendList(targetId, trends, emptyText) {
  $(targetId).innerHTML = trends.length
    ? trends.slice(0, 5).map(dashboardTrendRow).join("")
    : `<p class="muted">${escapeHtml(emptyText)}</p>`;
}

function dashboardTrendRow(item) {
  const summary = trendCardSummary(item);
  return `
    <article class="trend-row" role="button" tabindex="0" onclick='openTrendDetails(${jsArg(item.id)})' onkeydown='openTrendDetailsFromKey(event, ${jsArg(item.id)})'>
      <div class="meta-row">
        <span>${escapeHtml(topicLabel(item.topic || ""))}</span>
        ${item.source_url ? `<a href="${escapeAttr(item.source_url)}" target="_blank" rel="noreferrer" onclick="event.stopPropagation()">Source</a>` : ""}
      </div>
      <h3>${escapeHtml(item.title)}</h3>
      ${summary ? `<p class="trend-summary">${escapeHtml(summary)}</p>` : ""}
      <div class="actions left">
        <button class="secondary" onclick='useTrendInPost(event, ${jsArg(item.id)})'>Use in Post</button>
      </div>
    </article>
  `;
}

function trendCardSummary(item) {
  return cleanString(item.summary)
    || summarize(item.source_text_excerpt || item.source_excerpt || "", 150)
    || cleanString(item.why_it_matters)
    || cleanString(item.leadpoet_angle);
}

function trendRelevanceText(item) {
  const explicit = cleanString(item.why_it_matters);
  if (explicit) return explicit;
  const angle = cleanString(item.leadpoet_angle);
  if (angle) return angle;
  const summary = cleanString(item.summary);
  if (summary) return summary;
  const topic = topicLabel(item.topic || "this topic");
  return `Worth watching because it may affect buyer priorities, sales workflows, or the timing behind ${topic}.`;
}

function renderDashboardPatterns() {
  const groups = state.dashboard?.pattern_groups || {};
  $("patternWeeklyButton").classList.toggle("active", state.selectedPatternWindow === "weekly");
  $("patternMonthlyButton").classList.toggle("active", state.selectedPatternWindow === "monthly");
  const patterns = groups[state.selectedPatternWindow] || [];
  $("patternList").innerHTML = patterns.length
    ? patterns.slice(0, 5).map(patternCard).join("")
    : `<p class="muted">No ${escapeHtml(state.selectedPatternWindow)} patterns yet. Add patterns manually or refresh Pattern Scout.</p>`;
}

function patternCard(pattern) {
  const summary = patternCardSummary(pattern);
  return `
    <article class="pattern-card" role="button" tabindex="0" onclick='openPatternDetails(${jsArg(pattern.id)})' onkeydown='openPatternDetailsFromKey(event, ${jsArg(pattern.id)})'>
      <div class="meta-row">
        <span>${escapeHtml(label(pattern.category || "other"))}</span>
        <span>${escapeHtml(topicLabel(pattern.topic || "No topic"))}</span>
        ${pattern.source_url ? `<a href="${escapeAttr(pattern.source_url)}" target="_blank" rel="noreferrer" onclick="event.stopPropagation()">Source</a>` : ""}
      </div>
      <h3>${escapeHtml(pattern.pattern_name || "Observed pattern")}</h3>
      ${summary ? `<p class="pattern-summary">${escapeHtml(summary)}</p>` : ""}
      <div class="actions left">
        ${patternSaveButton(pattern)}
        <button class="secondary" onclick='usePatternInPost(event, ${jsArg(pattern.id)})'>Use Pattern</button>
      </div>
    </article>
  `;
}

function patternCardSummary(pattern) {
  return cleanString(pattern.source_summary)
    || cleanString(pattern.structure)
    || cleanString(pattern.why_it_worked)
    || cleanString(pattern.adaptation_rule);
}

function patternSaveButton(pattern) {
  if (pattern.status === "saved") {
    return `<button class="secondary" type="button" disabled>Saved</button>`;
  }
  return `<button class="secondary" type="button" onclick='savePattern(event, ${jsArg(pattern.id)})'>Save</button>`;
}

window.openTrendDetailsFromKey = (event, id) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  event.preventDefault();
  window.openTrendDetails(id);
};

window.openTrendDetails = (id) => {
  const trend = findTrendItem(id);
  if (!trend) return;
  const sourceDetails = trendSourceDetails(trend);

  $("trendModalTitle").textContent = trend.title || "Trend details";
  $("trendModalBody").innerHTML = `
    <div class="detail-grid">
      ${detail("Topic", topicLabel(trend.topic || "Unknown"))}
      ${detail("Urgency", label(trend.urgency || "medium"))}
      ${detail("Type", label(trend.source_type || "trend"))}
      ${detail("Date", formatDateTime(trend.date))}
      ${detail("Source", trend.source_author || trend.source_title || "Unknown")}
    </div>
    ${trendSection("What happened", trend.summary)}
    ${trendSection("Why it matters", trend.why_it_matters)}
    ${trendSection("Leadpoet angle", trend.leadpoet_angle)}
    ${trendListSection("Suggested hook", trendHookList(trend))}
    ${trendSection("Source", trend.source_url ? `<a href="${escapeAttr(trend.source_url)}" target="_blank" rel="noreferrer">${escapeHtml(trend.source_author || trend.source_title || trend.source_url)}</a>` : escapeHtml(trend.source_author || "No source URL stored."), true)}
    <details class="trend-detail-section source-details">
      <summary>Source details</summary>
      <ul class="bullet-list compact">${sourceDetails.map(trendBullet).join("")}</ul>
      ${trend.source_text_excerpt ? `<div class="source-text">${escapeHtml(trend.source_text_excerpt)}</div>` : ""}
    </details>
    <div class="actions left">
      ${trend.source_url ? `<a class="button-link" href="${escapeAttr(trend.source_url)}" target="_blank" rel="noreferrer">Open Source</a>` : ""}
    </div>
  `;
  openModal("trendModal");
};

window.useTrendInPost = (event, id) => {
  event.stopPropagation();
  const trend = findTrendItem(id);
  if (!trend) return;
  state.selectedTrendForCreate = trend;
  state.selectedPatternForCreate = null;
  $("createPostInstruction").value = trend.leadpoet_angle || trend.title || "";
  $("createPostTopic").value = trend.topic || "Leadpoet";
  renderCreatePostPatternHint();
  openCreatePostModal();
};

function findTrendItem(id) {
  if (!id) return null;
  const groups = state.dashboard?.trend_groups || {};
  return [
    ...(groups.daily || []),
    ...(groups.weekly || []),
    ...(state.dashboard?.trends || []),
    ...(state.savedTrends || []),
  ].find((item) => item.id === id);
}

function findPatternItem(id) {
  if (!id) return null;
  const groups = state.dashboard?.pattern_groups || {};
  return [
    ...(groups.monthly || []),
    ...(groups.weekly || []),
    ...(state.dashboard?.saved_patterns || []),
  ].find((item) => item.id === id);
}

function allCreatePostTrends() {
  const groups = state.dashboard?.trend_groups || {};
  const trends = [
    ...(groups.daily || []),
    ...(groups.weekly || []),
    ...(state.dashboard?.trends || []),
    ...(state.savedTrends || []),
  ];
  return uniqueById(trends);
}

function savedCreatePostPatterns() {
  return uniqueById([
    ...(state.selectedPatternForCreate ? [state.selectedPatternForCreate] : []),
    ...(state.dashboard?.saved_patterns || []),
  ]);
}

function uniqueById(items) {
  const seen = new Set();
  return (items || []).filter((item) => {
    if (!item?.id || seen.has(item.id)) return false;
    seen.add(item.id);
    return true;
  });
}

function optionHtml(value, text) {
  return `<option value="${escapeAttr(value)}">${escapeHtml(text)}</option>`;
}

function renderCreatePostSourceSelectors() {
  const trendSelect = $("createPostTrend");
  const patternSelect = $("createPostPattern");
  if (!trendSelect || !patternSelect) return;

  const selectedTrendId = state.selectedTrendForCreate?.id || trendSelect.value || "";
  const trends = allCreatePostTrends();
  trendSelect.innerHTML = [
    optionHtml("", trends.length ? "No trend" : "No loaded trends"),
    ...trends.map((trend) => optionHtml(trend.id, trendDropdownLabel(trend))),
  ].join("");
  trendSelect.value = trends.some((trend) => trend.id === selectedTrendId) ? selectedTrendId : "";
  trendSelect.disabled = !trends.length;

  const selectedPatternId = state.selectedPatternForCreate?.id || patternSelect.value || "";
  const patterns = savedCreatePostPatterns();
  patternSelect.innerHTML = [
    optionHtml("", patterns.length ? "No pattern" : "No saved patterns"),
    ...patterns.map((pattern) => optionHtml(pattern.id, patternDropdownLabel(pattern))),
  ].join("");
  patternSelect.value = patterns.some((pattern) => pattern.id === selectedPatternId) ? selectedPatternId : "";
  patternSelect.disabled = !patterns.length;

  syncCreatePostSelectionsFromInputs();
}

function trendDropdownLabel(trend) {
  const topic = topicLabel(trend.topic || "");
  const title = trend.title || "Untitled trend";
  return topic ? `${topic} - ${title}` : title;
}

function patternDropdownLabel(pattern) {
  const name = pattern.pattern_name || "Saved pattern";
  const topic = topicLabel(pattern.topic || "");
  return topic ? `${topic} - ${name}` : name;
}

function syncCreatePostSelectionsFromInputs() {
  state.selectedTrendForCreate = findTrendItem($("createPostTrend")?.value || "") || null;
  state.selectedPatternForCreate = findPatternItem($("createPostPattern")?.value || "") || null;
  renderCreatePostPatternHint();
}

window.openPatternDetailsFromKey = (event, id) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  event.preventDefault();
  window.openPatternDetails(id);
};

window.openPatternDetails = (id) => {
  runButton(null, "", async () => {
    const payload = await api(`/api/patterns/${encodeURIComponent(id)}`);
    state.patternDetail = payload.pattern;
    renderPatternModal();
    openModal("patternModal");
  });
};

function renderPatternModal() {
  const pattern = state.patternDetail;
  if (!pattern) return;
  const metrics = pattern.metrics || {};
  $("patternModalTitle").textContent = pattern.pattern_name || "Pattern details";
  $("patternModalBody").innerHTML = `
    <p class="pattern-rule">Use the structure, not the wording.</p>
    <div class="full-text">${escapeHtml(pattern.source_text || pattern.source_summary || "No source text stored.")}</div>
    <div class="compact-meta-row">
      ${compactMeta("Category", label(pattern.category || "other"))}
      ${compactMeta("Topic", topicLabel(pattern.topic || "No topic"))}
      ${compactMeta("Author", pattern.source_author || "Unknown")}
      ${compactMeta("Signal", patternSignal(pattern))}
    </div>
    <div class="compact-metrics-row" aria-label="Pattern metrics">
      ${compactMetric("Likes", metrics.likes)}
      ${compactMetric("Replies", metrics.replies)}
      ${compactMetric("Reposts", metrics.reposts)}
      ${compactMetric("Quotes", metrics.quotes)}
      ${compactMetric("Bookmarks", metrics.bookmarks)}
      ${compactMetric("Impressions", metrics.impressions)}
    </div>
    <section class="trend-detail-section">
      <h3>Pattern analysis</h3>
      <ul class="bullet-list">
        ${trendBullet(["Structure", pattern.structure || "Not set"])}
        ${trendBullet(["Hook style", pattern.hook_style || "Not set"])}
        ${trendBullet(["Pacing", pattern.pacing || "Not set"])}
        ${trendBullet(["Tone", pattern.tone || "Not set"])}
        ${trendBullet(["Formatting", pattern.formatting || "Not set"])}
        ${trendBullet(["Why this worked", pattern.why_it_worked || "Not set"])}
        ${trendBullet(["Adaptation rule", pattern.adaptation_rule || "Use the structure, not the wording."])}
      </ul>
    </section>
    <div class="actions left">
      ${patternSaveButton(pattern)}
      <button onclick='usePatternInPost(event, ${jsArg(pattern.id)})'>Use Pattern</button>
      ${pattern.source_url ? `<a class="button-link secondary-link" href="${escapeAttr(pattern.source_url)}" target="_blank" rel="noreferrer">Open Source</a>` : ""}
      <button class="secondary" onclick='archivePattern(event, ${jsArg(pattern.id)})'>Archive</button>
      <button class="quiet-button" onclick='ignorePattern(event, ${jsArg(pattern.id)})'>Ignore</button>
    </div>
  `;
}

function renderCreatePostPatternHint() {
  const hint = $("createPostPatternHint");
  if (!hint) return;
  const trend = state.selectedTrendForCreate;
  const pattern = state.selectedPatternForCreate;
  const rows = [];
  if (trend) {
    rows.push(`
      <div>
        <strong>${escapeHtml(trend.title || "Selected trend")}</strong>
        <span>${escapeHtml(trend.leadpoet_angle || trend.summary || "Current trend context")}</span>
      </div>
      <button class="quiet-button" type="button" onclick="clearSelectedTrend(event)">Remove</button>
    `);
  }
  if (pattern) {
    rows.push(`
      <div>
        <strong>${escapeHtml(pattern.pattern_name || "Selected pattern")}</strong>
        <span>${escapeHtml(pattern.structure || pattern.adaptation_rule || "Use the structure, not the wording.")}</span>
      </div>
      <button class="quiet-button" type="button" onclick="clearSelectedPattern(event)">Remove</button>
    `);
  }
  if (!rows.length) {
    hint.classList.add("hidden");
    hint.innerHTML = "";
    return;
  }
  hint.classList.remove("hidden");
  hint.innerHTML = rows.map((row) => `<div class="selected-source-row">${row}</div>`).join("");
}

window.clearSelectedTrend = (event) => {
  event?.stopPropagation();
  state.selectedTrendForCreate = null;
  if ($("createPostTrend")) $("createPostTrend").value = "";
  renderCreatePostPatternHint();
};

window.clearSelectedPattern = (event) => {
  event?.stopPropagation();
  state.selectedPatternForCreate = null;
  if ($("createPostPattern")) $("createPostPattern").value = "";
  renderCreatePostPatternHint();
};

window.usePatternInPost = (event, id) => {
  event.stopPropagation();
  const pattern = state.patternDetail?.id === id ? state.patternDetail : findPatternItem(id);
  if (!pattern) return;
  state.selectedPatternForCreate = pattern;
  $("createPostTopic").value = pattern.topic || $("createPostTopic").value || "Leadpoet";
  if (!$("createPostInstruction").value.trim()) {
    $("createPostInstruction").value = pattern.structure || pattern.pattern_name || "";
  }
  renderCreatePostPatternHint();
  closeModal("patternModal");
  openCreatePostModal();
};

window.archivePattern = (event, id) => updatePatternStatus(event, id, "archive");

window.ignorePattern = (event, id) => updatePatternStatus(event, id, "ignore");

window.savePattern = (event, id) => updatePatternStatus(event, id, "save");

function updatePatternStatus(event, id, action) {
  event.preventDefault();
  event.stopPropagation();
  runButton(event.currentTarget, "Saving...", async () => {
    await api(`/api/patterns/${encodeURIComponent(id)}/${action}`, { method: "POST", body: "{}" });
    if (action !== "save") {
      closeModal("patternModal");
    }
    showMessage(action === "archive" ? "Pattern archived." : action === "save" ? "Pattern saved to library." : "Pattern ignored.");
    await refresh();
    if (action === "save" && state.patternDetail?.id === id) {
      state.patternDetail = findPatternItem(id) || state.patternDetail;
      renderPatternModal();
    }
  });
}

function patternSignal(pattern) {
  const metrics = pattern.metrics || {};
  if (metrics.bookmarks !== null && metrics.bookmarks !== undefined) return `${metric(metrics.bookmarks)} bookmarks`;
  if (metrics.replies !== null && metrics.replies !== undefined) return `${metric(metrics.replies)} replies`;
  if (metrics.reposts !== null && metrics.reposts !== undefined) return `${metric(metrics.reposts)} reposts`;
  if (metrics.likes !== null && metrics.likes !== undefined) return `${metric(metrics.likes)} likes`;
  if (metrics.impressions !== null && metrics.impressions !== undefined) return `${metric(metrics.impressions)} impressions`;
  return pattern.engagement_notes || "No signal yet";
}

function trendSection(title, value, htmlValue = false) {
  const cleaned = cleanString(value);
  if (!cleaned) return "";
  return `
    <section class="trend-detail-section">
      <h3>${escapeHtml(title)}</h3>
      <p>${htmlValue ? value : escapeHtml(cleaned)}</p>
    </section>
  `;
}

function trendListSection(title, values) {
  if (!values.length) return "";
  return `
    <section class="trend-detail-section">
      <h3>${escapeHtml(title)}</h3>
      <ul class="bullet-list">${values.map((value) => `<li>${escapeHtml(value)}</li>`).join("")}</ul>
    </section>
  `;
}

function trendHookList(trend) {
  if (Array.isArray(trend.suggested_hooks)) return trend.suggested_hooks.map(cleanString).filter(Boolean).slice(0, 3);
  return cleanString(trend.suggested_hooks)
    .split(/\n+|;/)
    .map(cleanString)
    .filter(Boolean)
    .slice(0, 3);
}

function trendSourceDetails(trend) {
  const keyPoints = Array.isArray(trend.key_points) ? trend.key_points.join("; ") : "";
  const claims = Array.isArray(trend.important_claims) ? trend.important_claims.join("; ") : "";
  const angles = Array.isArray(trend.post_angles) ? trend.post_angles.join("; ") : "";
  return [
    ["Source URL", trend.source_url],
    ["Source author", trend.source_author],
    ["Published date", trend.source_published_at],
    ["Raw snippet", trend.source_excerpt],
    ["Key points", keyPoints],
    ["Important claims", claims],
    ["Post angles", angles],
    ["Enrichment status", trend.enrichment_status],
    ["Source confidence", trend.source_confidence],
    ["Enrichment error", trend.enrichment_error],
  ].filter(([, value]) => cleanString(value));
}

function trendBullet([labelText, value]) {
  return `<li><strong>${escapeHtml(labelText)}:</strong> ${escapeHtml(cleanString(value))}</li>`;
}

function cleanString(value) {
  return String(value ?? "").replace(/\s+/g, " ").trim();
}

function renderReviewer() {
  closeReviewAddMenu();
  $("reviewCount").textContent = String(state.reviewItems.length);
  $("scheduledCount").textContent = String(state.scheduledItems.length);
  $("reviewList").innerHTML = state.reviewItems.length
    ? state.reviewItems.map((item) => queueCard(item, "review")).join("")
    : reviewEmptyState();
  $("scheduledList").innerHTML = state.scheduledItems.length
    ? state.scheduledItems.map((item) => queueCard(item, "scheduled")).join("")
    : emptyState("No approved posts scheduled yet.");
}

function queueCard(item, column) {
  const scheduled = item.scheduled_at ? formatDateTime(item.scheduled_at) : "";
  const canPost = item.status === "approved" && state.status?.x_write_configured;
  const quality = qualityLabel(item.quality_mode);
  const trend = trendSummary(item);
  return `
    <article class="queue-card" draggable="true" ondragstart='dragQueue(event, ${jsArg(item.id)})'>
      <button class="card-open" onclick='openQueueDetail(${jsArg(item.id)})'>
        <div class="meta-row">
          <span class="tag">${escapeHtml(label(item.status))}</span>
          <span>${escapeHtml(topicLabel(item.topic || "No topic"))}</span>
          <span>${escapeHtml(label(item.post_type || "post"))}</span>
          ${quality ? `<span>${escapeHtml(quality)}</span>` : ""}
          ${trend ? `<span>${escapeHtml(trend)}</span>` : ""}
        </div>
        <h3>${escapeHtml(item.focus || summarize(item.angle || item.text, 88))}</h3>
        <p>${escapeHtml(summarize(item.text, 150))}</p>
        <div class="schedule-line">${escapeHtml(scheduled ? `Scheduled ${scheduled}` : "Not scheduled")}</div>
      </button>
      <div class="card-actions">
        <button onclick='openQueueDetail(${jsArg(item.id)})'>Review / Edit</button>
        ${item.status !== "approved" ? `<button class="secondary" onclick='approveQueue(event, ${jsArg(item.id)})'>Approve</button>` : ""}
        ${item.status === "approved" ? `<button class="secondary" onclick='postNow(event, ${jsArg(item.id)})' ${canPost ? "" : "disabled"} title="${state.status?.x_write_configured ? "" : "X write auth is not connected."}">Post now</button>` : ""}
        ${column === "scheduled" ? `<button class="quiet-button" onclick='moveToReview(event, ${jsArg(item.id)})'>Move to review</button>` : `<button class="quiet-button" onclick='skipQueue(event, ${jsArg(item.id)})'>Skip</button>`}
      </div>
    </article>
  `;
}

window.dragQueue = (event, id) => {
  state.draggedQueueId = id;
  event.dataTransfer.setData("text/plain", id);
};

async function handleDrop(target) {
  const id = state.draggedQueueId;
  state.draggedQueueId = "";
  if (!id) return;
  if (target === "scheduled") {
    await moveToScheduled(id);
  } else {
    await api(`/api/queue/${encodeURIComponent(id)}/needs-review`, { method: "POST", body: "{}" });
    showMessage("Moved back to review.");
    await refresh();
  }
}

async function moveToScheduled(id) {
  const item = findQueueItem(id);
  let scheduledAt = item?.scheduled_at || "";
  if (!scheduledAt || item?.status !== "approved") {
    scheduledAt = window.prompt("Scheduled date/time", scheduledAt || nextDefaultSchedule()) || "";
  }
  if (!scheduledAt.trim()) {
    showMessage("Schedule required before moving to Scheduled.", true);
    return;
  }
  await api(`/api/queue/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify({ scheduled_at: scheduledAt }),
  });
  await api(`/api/queue/${encodeURIComponent(id)}/approve`, { method: "POST", body: "{}" });
  showMessage("Approved and scheduled.");
  await refresh();
}

window.openQueueDetail = (id) => {
  runButton(null, "", async () => {
    const payload = await api(`/api/queue/${encodeURIComponent(id)}`);
    state.queueDetail = payload.item;
    renderQueueModal();
    openModal("queueModal");
  });
};

function renderQueueModal() {
  const item = state.queueDetail;
  if (!item) return;
  const canUseAI = Boolean(state.status?.provider_configured);
  const canPost = item.status === "approved" && Boolean(state.status?.x_write_configured);
  const scheduledValue = dateTimeLocalValue(item.scheduled_at);
  $("queueModalTitle").textContent = item.focus || summarize(item.text, 70);
  $("queueModalBody").innerHTML = `
    <label>Post text<textarea id="detailText" rows="8">${escapeHtml(item.text)}</textarea></label>
    <div class="form-grid">
      <label>Topic<input id="detailTopic" value="${escapeAttr(item.topic)}"></label>
      <label>Post type<input id="detailPostType" value="${escapeAttr(item.post_type)}"></label>
      <label>Scheduled time<input id="detailScheduled" type="datetime-local" value="${escapeAttr(scheduledValue)}"></label>
      <label>Status<input value="${escapeAttr(label(item.status))}" disabled></label>
    </div>
    <div class="schedule-shortcuts" aria-label="Schedule shortcuts">
      <button class="secondary" type="button" onclick='setDetailSchedulePreset(event, "tomorrow_morning")'>Tomorrow AM</button>
      <button class="secondary" type="button" onclick='setDetailSchedulePreset(event, "next_weekday_morning")'>Next Weekday AM</button>
      <button class="secondary" type="button" onclick='setDetailSchedulePreset(event, "two_hours")'>+2 Hours</button>
      <button class="quiet-button" type="button" onclick="clearDetailSchedule(event)">Clear</button>
    </div>
    <label>Reviewer notes<textarea id="detailNotes" rows="3">${escapeHtml(item.reviewer_notes || "")}</textarea></label>
    <div class="detail-grid">
      ${detail("Quality", qualityLabel(item.quality_mode) || "Unknown")}
      ${detail("Trend/source", trendSummary(item) || "None")}
      ${detail("Why suggested", item.angle || item.reviewer_notes || "Suggested from Social Brain context.")}
      ${detail("Draft", item.draft_id || "No draft link")}
    </div>
    <div class="modal-actions">
      <button onclick="saveQueueDetail(event)">Save changes</button>
      <button class="secondary" onclick="reviseDetail(event)" ${canUseAI ? "" : "disabled"} title="${canUseAI ? "" : "Provider-backed generation is not connected."}">Improve with AI</button>
      <button class="secondary" onclick="generateBestVersion(event)" ${canUseAI ? "" : "disabled"} title="${canUseAI ? "" : "Provider-backed generation is not connected."}">Generate Best Version</button>
      ${item.status !== "approved" ? `<button class="secondary" onclick='approveQueue(event, ${jsArg(item.id)})'>Approve</button>` : `<button class="secondary" onclick='moveToReview(event, ${jsArg(item.id)})'>Move to review</button>`}
      ${item.status === "approved" ? `<button class="secondary" onclick='postNow(event, ${jsArg(item.id)})' ${canPost ? "" : "disabled"} title="${state.status?.x_write_configured ? "" : "X write auth is not connected."}">Post now</button>` : ""}
      <button class="quiet-button" onclick='skipQueue(event, ${jsArg(item.id)})'>Skip</button>
    </div>
    <div class="revision-box">
      <label>AI instruction<textarea id="revisionInstruction" rows="3" placeholder="Make it sharper, less corporate, or tie it more directly to Leadpoet."></textarea></label>
      <div class="intent-actions">
        <button class="secondary" onclick='reviseWithPreset(event, "Make this more natural and less polished.")' ${canUseAI ? "" : "disabled"}>Make More Natural</button>
        <button class="secondary" onclick='reviseWithPreset(event, "Make this sharper and more opinionated.")' ${canUseAI ? "" : "disabled"}>Make Sharper</button>
        <button class="secondary" onclick='reviseWithPreset(event, "Make this more founder-led and personal to Gavin.")' ${canUseAI ? "" : "disabled"}>Make More Founder-Led</button>
        <button class="secondary" onclick='reviseWithPreset(event, "Shorten this while keeping the point.")' ${canUseAI ? "" : "disabled"}>Shorten</button>
        <button class="secondary" onclick='reviseWithPreset(event, "Tie this more clearly to Leadpoet without making it salesy.")' ${canUseAI ? "" : "disabled"}>Tie More to Leadpoet</button>
        <button class="secondary" onclick='reviseWithPreset(event, "Generate the strongest final version of this queued post.", true)' ${canUseAI ? "" : "disabled"}>Generate Best Version</button>
      </div>
      <div id="revisionList" class="revision-list">${revisionList(item.versions || [])}</div>
    </div>
  `;
}

function revisionList(versions) {
  return versions.length
    ? versions.map((version) => `
      <article class="revision">
        <div class="revision-text">${escapeHtml(version.text)}</div>
        ${version.edit_instruction ? `<p class="muted">${escapeHtml(version.edit_instruction)}</p>` : ""}
        <button class="secondary" onclick='acceptRevision(event, ${jsArg(version.queue_id)}, ${jsArg(version.id)})'>Accept revision</button>
      </article>
    `).join("")
    : `<p class="muted">Revision options will appear here. Accepting one updates the draft but does not approve it.</p>`;
}

window.saveQueueDetail = (event) => {
  runButton(event.currentTarget, "Saving...", async () => {
    const id = state.queueDetail.id;
    const payload = await api(`/api/queue/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({
        text: $("detailText").value,
        topic: $("detailTopic").value,
        post_type: $("detailPostType").value,
        scheduled_at: $("detailScheduled").value,
        reviewer_notes: $("detailNotes").value,
      }),
    });
    state.queueDetail = payload.item;
    showMessage("Post updated.");
    await refresh();
    renderQueueModal();
  });
};

window.reviseDetail = (event) => {
  runButton(event.currentTarget, "Improving...", async () => {
    const instruction = $("revisionInstruction").value.trim();
    if (!instruction) throw new Error("Add an AI revision instruction first.");
    const payload = await api(`/api/queue/${encodeURIComponent(state.queueDetail.id)}/revise`, {
      method: "POST",
      body: JSON.stringify({ instruction, variants: 3 }),
    });
    state.queueDetail = {
      ...state.queueDetail,
      versions: payload.versions,
    };
    showMessage("Revision options generated.");
    renderQueueModal();
  });
};

window.generateBestVersion = (event) => {
  reviseWithPreset(event, "Generate the strongest final version of this queued post.", true);
};

window.reviseWithPreset = (event, instruction, bestVersion = false) => {
  $("revisionInstruction").value = instruction;
  runButton(event.currentTarget, "Generating...", async () => {
    const payload = await api(`/api/queue/${encodeURIComponent(state.queueDetail.id)}/revise`, {
      method: "POST",
      body: JSON.stringify({ instruction, variants: bestVersion ? 1 : 3, best_version: bestVersion }),
    });
    state.queueDetail = {
      ...state.queueDetail,
      versions: payload.versions,
    };
    showMessage(bestVersion ? "Best Version generated. Accept it when it is ready." : "Revision options generated.");
    renderQueueModal();
  });
};

window.acceptRevision = (event, queueId, versionId) => {
  runButton(event.currentTarget, "Accepting...", async () => {
    const payload = await api(`/api/queue/${encodeURIComponent(queueId)}/versions/${encodeURIComponent(versionId)}`, {
      method: "POST",
      body: "{}",
    });
    state.queueDetail = payload.item;
    showMessage("Revision accepted. Review before approving.");
    await refresh();
    renderQueueModal();
  });
};

window.approveQueue = (event, id) => {
  runButton(event.currentTarget, "Approving...", async () => {
    const item = findQueueItem(id) || state.queueDetail;
    const scheduledInput = $("detailScheduled");
    const scheduledAt = scheduledInput?.value || dateTimeLocalValue(item?.scheduled_at);
    if (!scheduledAt.trim()) {
      if (scheduledInput) {
        scheduledInput.value = schedulePresetValue("tomorrow_morning");
        scheduledInput.focus();
      }
      throw new Error("Choose a scheduled time before approval.");
    }
    if (scheduledAt !== item?.scheduled_at) {
      await api(`/api/queue/${encodeURIComponent(id)}`, {
        method: "PATCH",
        body: JSON.stringify({ scheduled_at: scheduledAt }),
      });
    }
    const payload = await api(`/api/queue/${encodeURIComponent(id)}/approve`, { method: "POST", body: "{}" });
    state.queueDetail = payload.item;
    showMessage("Post approved and scheduled.");
    await refresh();
    if (!$("queueModal").classList.contains("hidden")) renderQueueModal();
  });
};

window.setDetailSchedulePreset = (event, preset) => {
  event.preventDefault();
  const input = $("detailScheduled");
  if (!input) return;
  input.value = schedulePresetValue(preset);
  input.focus();
};

window.clearDetailSchedule = (event) => {
  event.preventDefault();
  const input = $("detailScheduled");
  if (!input) return;
  input.value = "";
  input.focus();
};

window.moveToReview = (event, id) => {
  runButton(event.currentTarget, "Moving...", async () => {
    const payload = await api(`/api/queue/${encodeURIComponent(id)}/needs-review`, { method: "POST", body: "{}" });
    state.queueDetail = payload.item;
    showMessage("Moved back to review.");
    await refresh();
    if (!$("queueModal").classList.contains("hidden")) renderQueueModal();
  });
};

window.skipQueue = (event, id) => {
  runButton(event.currentTarget, "Skipping...", async () => {
    await api(`/api/queue/${encodeURIComponent(id)}/skip`, { method: "POST", body: "{}" });
    showMessage("Post skipped.");
    closeModal("queueModal");
    await refresh();
  });
};

window.postNow = (event, id) => {
  runButton(event.currentTarget, "Posting...", async () => {
    const payload = await api(`/api/queue/${encodeURIComponent(id)}/post-now`, { method: "POST", body: "{}" });
    const result = payload.result || {};
    if (result.error) throw new Error(result.error);
    showMessage("Posted to X.");
    closeModal("queueModal");
    await refresh();
  });
};

function openCreatePostModal() {
  $("createPostResult").innerHTML = "";
  renderCreatePostSourceSelectors();
  renderCreatePostPatternHint();
  openModal("createPostModal");
  window.setTimeout(() => $("createPostInstruction").focus(), 0);
}

async function createPost() {
  const instruction = $("createPostInstruction").value.trim();
  if (!instruction) throw new Error("Post idea / instruction is required.");
  syncCreatePostSelectionsFromInputs();
  const trendId = state.selectedTrendForCreate?.id || "";
  const patternId = state.selectedPatternForCreate?.id || "";
  const payload = await api("/api/content/create-post", {
    method: "POST",
    body: JSON.stringify({
      instruction,
      topic: $("createPostTopic").value || "Leadpoet",
      post_type: $("createPostType").value || "founder_insight",
      use_trends: Boolean(trendId),
      trend_ids: trendId ? [trendId] : [],
      use_patterns: Boolean(patternId),
      pattern_ids: patternId ? [patternId] : [],
      best_version: true,
      scheduled_at: $("createPostScheduled").value,
      extra_context: $("createPostExtra").value,
    }),
  });
  const warnings = payload.warnings || [];
  $("createPostResult").innerHTML = warnings.length
    ? warnings.map((warning) => `<p>${escapeHtml(warning)}</p>`).join("")
    : "";
  showMessage(warnings.length ? `Created post for review. ${warnings[0]}` : "Created post for review.");
  closeModal("createPostModal");
  $("createPostInstruction").value = "";
  $("createPostExtra").value = "";
  $("createPostScheduled").value = "";
  if ($("createPostTrend")) $("createPostTrend").value = "";
  if ($("createPostPattern")) $("createPostPattern").value = "";
  state.selectedTrendForCreate = null;
  state.selectedPatternForCreate = null;
  renderCreatePostPatternHint();
  await refresh();
  setView("reviewer");
}

async function planWeek() {
  const payload = await api("/api/plan-week", {
    method: "POST",
    body: JSON.stringify({
      week_start: $("planWeekStart").value,
      posts: Number($("planPosts").value || 5),
      topic_mix: $("planTopicMix").value,
      default_post_type: $("planPostType").value,
      cadence: $("planCadence").value,
      trend_window_days: 14,
      use_trends: $("planUseTrends").checked,
      use_patterns: $("planUsePatterns").checked,
      allow_evergreen: $("planAllowEvergreen").checked,
      best_version: true,
      instruction: $("planInstruction").value,
      variants_per_slot: 1,
    }),
  });
  const warnings = payload.warnings || [];
  $("planResult").innerHTML = warnings.length
    ? warnings.map((warning) => `<p>${escapeHtml(warning)}</p>`).join("")
    : "";
  showMessage(`Created ${payload.queued.length} posts for review.`);
  closeModal("planModal");
  state.view = "reviewer";
  setView("reviewer");
  await refresh();
}

async function syncPosts() {
  const autoCategorize = Boolean(state.status?.provider_configured);
  const payload = await api("/api/account/sync-x-posts", {
    method: "POST",
    body: JSON.stringify({
      username: DEFAULT_SYNC_USERNAME,
      limit: 25,
      ...(autoCategorize ? { auto_categorize: true } : { default_topic: "Leadpoet" }),
      default_post_type: "founder_insight",
      exclude_replies: true,
      exclude_retweets: true,
    }),
  });
  const result = payload.result || {};
  showMessage(`X posts synced. Imported ${result.imported || 0}. Recategorized ${result.recategorized || 0}. Duplicates: ${result.duplicates || 0}.`);
  await refreshAfterAccountAction();
}

async function syncMetrics() {
  const payload = await api("/api/account/sync-x-metrics", {
    method: "POST",
    body: JSON.stringify({ since_days: 30, limit: 250 }),
  });
  const result = payload.result || {};
  showMessage(`Metrics updated: ${result.updated || 0}. Skipped: ${result.skipped || 0}.`);
  await refreshAfterAccountAction();
}

async function syncAllXData() {
  const autoCategorize = Boolean(state.status?.provider_configured);
  const payload = await api("/api/account/sync-x-all", {
    method: "POST",
    body: JSON.stringify({
      username: DEFAULT_SYNC_USERNAME,
      post_limit: 25,
      metrics_limit: 250,
      ...(autoCategorize ? { auto_categorize: true } : { default_topic: "Leadpoet" }),
      default_post_type: "founder_insight",
      exclude_replies: true,
      exclude_retweets: true,
      since_days: 30,
    }),
  });
  const posts = payload.posts || {};
  const metrics = payload.metrics || {};
  showMessage(`X data synced. Imported ${posts.imported || 0}; recategorized ${posts.recategorized || 0}; metrics updated ${metrics.updated || 0}.`);
  await refreshAfterAccountAction();
}

async function refreshTrendsFromAccount() {
  const payload = await api("/api/trends/refresh", {
    method: "POST",
    body: JSON.stringify({ limit: 5 }),
  });
  const daily = payload.trend_groups?.daily?.length || 0;
  const weekly = payload.trend_groups?.weekly?.length || 0;
  showMessage(`Trends refreshed. Daily ${daily}; weekly ${weekly}.`);
  await refreshAfterAccountAction();
}

async function refreshPatternsFromAccount() {
  const payload = await api("/api/patterns/refresh", {
    method: "POST",
    body: JSON.stringify({ limit: 5, windows: ["weekly", "monthly"] }),
  });
  const weekly = payload.windows?.weekly?.stored || 0;
  const monthly = payload.windows?.monthly?.stored || 0;
  showMessage(`Patterns refreshed. Weekly ${weekly}; monthly ${monthly}; skipped ${payload.skipped || 0}.`);
  await refreshAfterAccountAction();
}

async function refreshAfterAccountAction() {
  await refresh();
  if (state.view === "account") {
    await loadAccount();
  }
}

async function loadVoice() {
  const [status, current, examples, suggestions, coverage, rules, avoidPhrases] = await Promise.all([
    api("/api/voice/status"),
    api("/api/voice/current"),
    api("/api/voice/examples"),
    api("/api/voice/suggestions"),
    api("/api/voice/coverage"),
    api("/api/voice/rules"),
    api("/api/voice/avoid-phrases"),
  ]);
  state.voice = {
    status,
    current,
    examples: examples.items || [],
    suggestions: suggestions.items || [],
    coverage,
    rules,
    avoidPhrases: avoidPhrases.items || [],
  };
  state.voiceAvoidDraft = [...state.voice.avoidPhrases];
  renderVoice();
}

function renderVoice() {
  const status = state.voice?.status || {};
  $("updateVoiceButton").disabled = status.status === "needs_examples";
  renderCurrentVoice();
  renderVoiceCoverage();
  renderVoiceRules();
  renderVoiceTopicOptions();
  renderVoiceExamples();
  renderVoiceSuggestions();
}

function renderCurrentVoice() {
  const current = state.voice?.current || {};
  if (!current.exists) {
    $("currentVoiceSummary").innerHTML = `<p class="empty-copy">${escapeHtml(current.empty_message || "No Voice yet. Mark a few strong posts as Voice Examples, then update Voice.")}</p>`;
    return;
  }
  const sections = cleanVoiceSections(current.sections || []);
  $("currentVoiceSummary").innerHTML = sections.length
    ? sections.map((section) => `
      <article class="voice-summary-section">
        <h3>${escapeHtml(section.title)}</h3>
        <ul>${(section.items || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
      </article>
    `).join("")
    : `<div class="source-text">${escapeHtml(current.content || "")}</div>`;
}

function cleanVoiceSections(sections) {
  const titles = {
    "Recurring voice traits": "Voice traits",
    "Voice traits": "Voice traits",
    "What strong posts tend to do": "What strong posts tend to do",
    "Avoid": "Guardrails",
    "What to avoid": "Guardrails",
    "Reusable writing principles": "Reusable writing principles",
  };
  const preferredOrder = [
    "Voice traits",
    "Guardrails",
    "Reusable writing principles",
  ];
  const cleaned = sections
    .map((section) => ({
      title: titles[section.title] || "",
      items: section.items || [],
    }))
    .filter((section) => section.title && section.items.length);
  return preferredOrder
    .map((title) => cleaned.find((section) => section.title === title))
    .filter(Boolean);
}

function renderVoiceCoverage() {
  const coverage = state.voice?.coverage || {};
  const topics = coverage.topics || [];
  const postTypes = coverage.post_types || [];
  $("voiceCoverageRecommendation").textContent = coverage.recommendation || "Voice coverage looks balanced enough for now.";
  $("voiceCoverageGrid").innerHTML = (topics.length || postTypes.length)
    ? [
        voiceCoverageLine("Topics covered", topics),
        voiceCoverageLine("Post types covered", postTypes.map((item) => ({ ...item, name: label(item.name) }))),
      ].join("")
    : `<p class="empty-copy compact">Add a few Voice Examples to see coverage.</p>`;
}

function voiceCoverageLine(title, items) {
  const summary = items
    .map((item) => `${topicLabel(item.name || "Unknown")} ${metric(item.count || 0)}`)
    .join(" · ");
  return `
    <div class="voice-coverage-line">
      <span>${escapeHtml(title)}</span>
      <strong>${escapeHtml(summary || "No examples yet")}</strong>
    </div>
  `;
}

function renderVoiceRules() {
  const rules = state.voice?.rules || {};
  const avoidCount = (state.voiceAvoidDraft || []).length;
  const groups = rules.groups || [];
  const rulesCount = groups.reduce((total, group) => total + (group.items || []).length, 0) + avoidCount;
  $("voiceRulesCount").textContent = `${metric(rulesCount || rules.rules_count || 0)} active rules`;
  $("voiceRulesSummary").textContent = rules.summary || "Positioning, Voice, Review Filters, and Writing Guardrails";
  $("voiceRulesList").innerHTML = groups.length
    ? groups.map((group) => `
      <section class="voice-rule-group">
        <div class="voice-rule-group-heading">
          <div>
            <h3>${escapeHtml(group.title || "Rules")}</h3>
            <p>${escapeHtml(group.description || "")}</p>
          </div>
          <span>${metric((group.items || []).length)}</span>
        </div>
        <ul>${(group.items || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
      </section>
    `).join("")
    : `<p class="empty-copy compact">No active rules loaded yet.</p>`;
  renderAvoidPhrases();
}

function renderAvoidPhrases() {
  const phrases = state.voiceAvoidDraft || [];
  $("voiceAvoidPhraseList").innerHTML = phrases.length
    ? phrases.map((phrase, index) => `
      <button class="phrase-chip" type="button" onclick="removeAvoidPhrase(${index})" title="Remove phrase">
        <span>${escapeHtml(phrase)}</span>
        <span aria-hidden="true">x</span>
      </button>
    `).join("")
    : `<p class="empty-copy compact">No avoid phrases set.</p>`;
}

function addAvoidPhrase() {
  const input = $("voiceAvoidPhraseInput");
  const phrase = input.value.trim().replace(/\s+/g, " ");
  if (!phrase) return;
  const existing = new Set((state.voiceAvoidDraft || []).map((item) => item.toLowerCase()));
  if (!existing.has(phrase.toLowerCase())) {
    state.voiceAvoidDraft = [...(state.voiceAvoidDraft || []), phrase];
  }
  input.value = "";
  setVoiceAvoidSaveStatus("Unsaved changes");
  renderAvoidPhrases();
}

window.removeAvoidPhrase = (index) => {
  state.voiceAvoidDraft = (state.voiceAvoidDraft || []).filter((_, itemIndex) => itemIndex !== index);
  setVoiceAvoidSaveStatus("Unsaved changes");
  renderAvoidPhrases();
};

async function saveAvoidPhrases() {
  const payload = await api("/api/voice/avoid-phrases", {
    method: "POST",
    body: JSON.stringify({ phrases: state.voiceAvoidDraft || [] }),
  });
  state.voice.avoidPhrases = payload.items || [];
  state.voiceAvoidDraft = [...state.voice.avoidPhrases];
  if (state.voice.rules) {
    state.voice.rules.avoid_phrases = [...state.voiceAvoidDraft];
    state.voice.rules.rules_count = (state.voice.rules.groups || []).reduce((total, group) => total + (group.items || []).length, 0) + state.voiceAvoidDraft.length;
  }
  setVoiceAvoidSaveStatus("Saved");
  renderVoiceRules();
  showMessage("Writing Guardrails saved.");
}

function setVoiceAvoidSaveStatus(text, isError = false) {
  const status = $("voiceAvoidSaveStatus");
  status.textContent = text || "";
  status.classList.toggle("error", isError);
}

function renderVoiceTopicOptions() {
  const select = $("voiceTopicFilter");
  const current = select.value || "all";
  const topics = [...new Set((state.voice?.examples || []).map((post) => post.topic).filter(Boolean))].sort();
  select.innerHTML = [
    `<option value="all">All Topics</option>`,
    ...topics.map((topic) => `<option value="${escapeAttr(topic)}">${escapeHtml(topicLabel(topic))}</option>`),
  ].join("");
  select.value = topics.includes(current) ? current : "all";
}

function renderVoiceExamples() {
  const examples = filteredVoiceExamples();
  $("voiceExamplesList").innerHTML = examples.length
    ? examples.map((post) => voiceExampleCard(post, { removable: true })).join("")
    : `<p class="empty-copy">No Voice Examples yet. Mark a few strong posts so Koinoniko can learn Gavin's voice.</p>`;
}

function filteredVoiceExamples() {
  const search = $("voiceSearch").value.trim().toLowerCase();
  const topic = $("voiceTopicFilter").value;
  const sort = $("voiceSort").value;
  const examples = (state.voice?.examples || []).filter((post) => {
    if (topic !== "all" && post.topic !== topic) return false;
    const haystack = [post.text, post.summary, post.topic, post.post_type, post.why_this_worked].join(" ").toLowerCase();
    return !search || haystack.includes(search);
  });
  return [...examples].sort((first, second) => {
    if (sort === "performance") return performanceScore(second) - performanceScore(first);
    return Date.parse(second.published_at || 0) - Date.parse(first.published_at || 0);
  });
}

function renderVoiceSuggestions() {
  const suggestions = state.voice?.suggestions || [];
  $("voiceSuggestionsList").innerHTML = suggestions.length
    ? suggestions.map((post) => voiceSuggestionCard(post)).join("")
    : `<p class="empty-copy">No suggestions yet. Sync metrics to identify high-performing posts.</p>`;
}

function voiceExampleCard(post, { removable = false } = {}) {
  const whyThisWorked = voiceDetailValue(post, "why_this_worked");
  return `
    <article class="voice-card">
      <div class="voice-card-main">
        <div class="meta-row">
          <span>${escapeHtml(formatDate(post.published_at))}</span>
          <span>${escapeHtml(topicLabel(post.topic || "No topic"))}</span>
          <span>${escapeHtml(label(post.post_type || "post"))}</span>
          <span>${escapeHtml(voicePerformanceSignal(post))}</span>
        </div>
        <p class="voice-post-preview">${escapeHtml(summarize(post.text, 190))}</p>
        <p class="voice-why"><strong>Why this worked</strong><span>${escapeHtml(whyThisWorked || "No note yet.")}</span></p>
      </div>
      <details class="advanced-fields voice-example-fields" id="${escapeAttr(voiceDetailPanelId(post.id))}">
        <summary>Voice details</summary>
        <div class="form-grid">
          ${voiceDetailTextarea(post, "why_this_worked", "Why this worked", 3)}
          ${voiceDetailInput(post, "hook", "Hook")}
          ${voiceDetailInput(post, "structure", "Structure")}
          ${voiceDetailInput(post, "tone", "Tone")}
          ${voiceDetailInput(post, "cta", "CTA")}
          ${voiceDetailInput(post, "formatting", "Formatting")}
          ${voiceDetailInput(post, "pattern", "Pattern")}
        </div>
        <div class="actions left">
          <button class="secondary" onclick='suggestWhyWorked(event, ${jsArg(post.id)})'>Suggest why this worked</button>
          <button class="secondary" onclick='saveVoiceExampleDetails(event, ${jsArg(post.id)})'>Save Details</button>
        </div>
      </details>
      <div class="actions left">
        <button class="secondary" onclick='openPostDetails(${jsArg(post.id)})'>Open Post</button>
        ${post.url ? `<a class="button-link secondary-link" href="${escapeAttr(post.url)}" target="_blank" rel="noreferrer">Open on X</a>` : ""}
        ${removable ? `<button class="quiet-button" onclick='removeVoiceExample(event, ${jsArg(post.id)})'>Remove</button>` : ""}
      </div>
    </article>
  `;
}

function voiceDetailInput(post, field, labelText) {
  return `
    <label>${escapeHtml(labelText)}
      <input id="${escapeAttr(voiceDetailFieldId(post.id, field))}" value="${escapeAttr(voiceDetailValue(post, field))}">
    </label>
  `;
}

function voiceDetailTextarea(post, field, labelText, rows = 3) {
  return `
    <label class="full-span">${escapeHtml(labelText)}
      <textarea id="${escapeAttr(voiceDetailFieldId(post.id, field))}" rows="${escapeAttr(rows)}">${escapeHtml(voiceDetailValue(post, field))}</textarea>
    </label>
  `;
}

function voiceDetailFieldId(postId, field) {
  return `voice-detail-${field}-${postId}`;
}

function voiceDetailPanelId(postId) {
  return `voice-detail-panel-${postId}`;
}

function voiceDetailValue(post, field) {
  const voice = post.voice || {};
  if (field === "why_this_worked") return voice.why_this_worked ?? post.why_this_worked ?? "";
  return voice[field] ?? "";
}

function voiceSuggestionCard(post) {
  return `
    <article class="voice-card suggested">
      <div class="voice-card-main">
        <div class="meta-row">
          <span>${escapeHtml(post.reason || "Suggested by performance")}</span>
          <span>${escapeHtml(topicLabel(post.topic || "No topic"))}</span>
        </div>
        <p class="voice-post-preview">${escapeHtml(summarize(post.text, 170))}</p>
      </div>
      <div class="actions left">
        <button onclick='markSuggestedVoiceExample(event, ${jsArg(post.id)})'>Mark as Voice Example</button>
        <button class="secondary" onclick='openPostDetails(${jsArg(post.id)})'>Open post</button>
      </div>
    </article>
  `;
}

function performanceScore(post) {
  const metrics = post.metrics || {};
  return Number(metrics.engagement_rate || 0) * 100000
    + Number(metrics.bookmarks || 0) * 10
    + Number(metrics.replies || 0) * 8
    + Number(metrics.reposts || 0) * 6
    + Number(metrics.impressions || 0) / 1000;
}

function voicePerformanceSignal(post) {
  const metrics = post.metrics || {};
  if (metrics.engagement_rate !== null && metrics.engagement_rate !== undefined) return `${formatRate(metrics.engagement_rate)} ER`;
  if (metrics.bookmarks !== null && metrics.bookmarks !== undefined) return `${metric(metrics.bookmarks)} bookmarks`;
  if (metrics.replies !== null && metrics.replies !== undefined) return `${metric(metrics.replies)} replies`;
  if (metrics.reposts !== null && metrics.reposts !== undefined) return `${metric(metrics.reposts)} reposts`;
  if (metrics.impressions !== null && metrics.impressions !== undefined) return `${metric(metrics.impressions)} impressions`;
  return "No metrics yet";
}

async function updateVoice() {
  await api("/api/voice/update", {
    method: "POST",
    body: "{}",
  });
  await loadVoice();
}

window.markSuggestedVoiceExample = (event, id) => {
  runButton(event.currentTarget, "Saving...", async () => {
    const payload = await api(`/api/posts/${encodeURIComponent(id)}/voice`, {
      method: "POST",
      body: JSON.stringify({ voice_example: true }),
    });
    showMessage(payload.message || "Saved as a Voice Example.");
    await refresh();
    await loadVoice();
  });
};

window.saveVoiceExampleDetails = (event, id) => {
  event.stopPropagation();
  runButton(event.currentTarget, "Saving...", async () => {
    const payload = await api(`/api/posts/${encodeURIComponent(id)}/voice`, {
      method: "POST",
      body: JSON.stringify({
        voice_example: true,
        why_this_worked: $(voiceDetailFieldId(id, "why_this_worked"))?.value || "",
        hook: $(voiceDetailFieldId(id, "hook"))?.value || "",
        structure: $(voiceDetailFieldId(id, "structure"))?.value || "",
        tone: $(voiceDetailFieldId(id, "tone"))?.value || "",
        cta: $(voiceDetailFieldId(id, "cta"))?.value || "",
        formatting: $(voiceDetailFieldId(id, "formatting"))?.value || "",
        pattern: $(voiceDetailFieldId(id, "pattern"))?.value || "",
      }),
    });
    if (state.postDetail?.id === id) {
      state.postDetail = payload.post;
    }
    showMessage(payload.message || "Voice details saved.");
    await refresh();
    await loadVoice();
  });
};

window.suggestWhyWorked = (event, id) => {
  event.stopPropagation();
  runButton(event.currentTarget, "Suggesting...", async () => {
    const payload = await api(`/api/voice/examples/${encodeURIComponent(id)}/suggest-note`, {
      method: "POST",
      body: JSON.stringify({ post_id: id }),
    });
    const field = $(voiceDetailFieldId(id, "why_this_worked"));
    const panel = $(voiceDetailPanelId(id));
    if (panel) panel.open = true;
    if (field) {
      field.value = payload.suggested_note || "";
      field.focus();
    }
    showMessage("Suggestion added. Review it, then Save Voice Details.");
  });
};

window.removeVoiceExample = (event, id) => {
  runButton(event.currentTarget, "Removing...", async () => {
    await api(`/api/posts/${encodeURIComponent(id)}/voice`, {
      method: "POST",
      body: JSON.stringify({ voice_example: false }),
    });
    showMessage("Removed from Voice Examples.");
    await refresh();
    await loadVoice();
  });
};

async function publishDue() {
  const payload = await api("/api/publish-due", {
    method: "POST",
    body: JSON.stringify({ limit: 20 }),
  });
  const failures = (payload.results || []).filter((result) => result.error);
  showMessage(
    failures.length
      ? `Processed ${payload.results.length} due posts. ${failures.length} failed.`
      : `Processed ${payload.results.length} due posts.`
  );
  await refresh();
}

async function loadTrends() {
  const payload = await api("/api/trends?limit=100&status=unused");
  state.savedTrends = payload.items || [];
  $("savedTrendList").innerHTML = state.savedTrends.length
    ? state.savedTrends.map(trendCard).join("")
    : `<p class="muted">No saved trends yet. The scheduled trend refresh will add current items.</p>`;
}

function trendCard(item) {
  return `
    <article class="trend-card" role="button" tabindex="0" onclick='openTrendDetails(${jsArg(item.id)})' onkeydown='openTrendDetailsFromKey(event, ${jsArg(item.id)})'>
      <div class="meta-row">
        <span class="tag">${escapeHtml(item.urgency || "medium")}</span>
        <span>${escapeHtml(topicLabel(item.topic || ""))}</span>
        <span>${escapeHtml(label(item.source_type || ""))}</span>
      </div>
      <h3>${escapeHtml(item.title)}</h3>
      <p>${escapeHtml(item.summary || "")}</p>
      ${item.leadpoet_angle ? `<p class="muted">${escapeHtml(item.leadpoet_angle)}</p>` : ""}
      <div class="actions left">
        <button class="secondary" onclick='markTrend(event, ${jsArg(item.id)}, "used")'>Mark used</button>
        <button class="quiet-button" onclick='markTrend(event, ${jsArg(item.id)}, "skipped")'>Ignore</button>
        ${item.source_url ? `<a href="${escapeAttr(item.source_url)}" target="_blank" rel="noreferrer" onclick="event.stopPropagation()">Source</a>` : ""}
      </div>
    </article>
  `;
}

window.markTrend = (event, id, status) => {
  event.stopPropagation();
  runButton(event.currentTarget, "Saving...", async () => {
    await api(`/api/trends/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    });
    showMessage(status === "used" ? "Trend marked used." : "Trend ignored.");
    await loadTrends();
    await refresh();
  });
};

async function loadAccount() {
  state.account = await api("/api/account");
  renderAccount();
}

function renderAccount() {
  const account = state.account || {};
  const status = account.status || state.status || {};
  const settings = account.settings || {};
  const sync = account.sync || {};
  const syncStatus = account.sync_status || {};
  const stats = account.stats || {};
  const connections = account.connections || status.connections || fallbackConnections(status);
  const connectionEntries = accountConnectionEntries(connections);
  const connectedCount = connectionEntries.filter(([, connection]) => connectionStatus(connection) === "connected").length;
  $("accountConnectionsSummary").textContent = `${connectedCount} connected`;
  $("accountStatusGrid").innerHTML = connectionEntries.map(([key, connection]) => connectionRow(key, connection)).join("");
  $("accountSyncGrid").innerHTML = [
    syncRow("Last X post sync", sync.x_posts_sync, "New posts imported from X"),
    syncRow("Last X metrics sync", sync.x_metrics_sync, "Fresh public metric snapshots"),
    syncRow("Last trend refresh", sync.trends_sync, "Daily and weekly trend context"),
    syncRow("Last pattern refresh", sync.patterns_sync, "Reusable post mechanics from Pattern Scout"),
  ].join("");
  const models = settings.models || {};
  const effectiveModels = settings.effective_models || {};
  const defaultModel = models.default || effectiveModels.default || "";
  $("accountModel").value = defaultModel;
  setModelInput("accountStrategistModel", models.strategist, effectiveModels.strategist || defaultModel);
  setModelInput("accountNaturalWriterModel", models.natural_writer, effectiveModels.natural_writer || defaultModel);
  setModelInput("accountEvaluatorModel", models.evaluator, effectiveModels.evaluator || defaultModel);
  $("accountModelSummary").textContent = modelRoutingSummary(models, effectiveModels, defaultModel);
  $("accountSyncSummary").textContent = syncSummary(sync, syncStatus, stats);
  const security = account.security || {};
  $("accountUserSummary").textContent = `Signed in as ${security.username || "configured user"}`;
  $("accountSecurityBlock").innerHTML = `
    <div class="security-row">
      <div>
        <span>Signed in as</span>
        <strong>${escapeHtml(security.username || "Configured user")}</strong>
      </div>
      <form method="post" action="/logout">
        <button class="secondary" type="submit">Log out</button>
      </form>
    </div>
  `;
  const trendsButton = $("accountRefreshTrendsButton");
  const patternsButton = $("accountRefreshPatternsButton");
  const syncMenuButton = $("accountSyncMenuButton");
  const xReadConfigured = Boolean(status.x_read_configured);
  ["accountSyncPostsButton", "accountSyncMetricsButton", "accountSyncAllButton"].forEach((id) => {
    const button = $(id);
    button.disabled = !xReadConfigured;
    button.title = xReadConfigured ? "" : "X read auth is not connected.";
  });
  const scrapingdogStatus = connectionStatus(connections.scrapingdog);
  trendsButton.disabled = scrapingdogStatus !== "connected";
  trendsButton.title = trendsButton.disabled ? "Connect ScrapingDog to refresh live trend context." : "Refresh live trend context.";
  patternsButton.disabled = scrapingdogStatus !== "connected" || !status.provider_configured;
  patternsButton.title = patternsButton.disabled ? "Connect ScrapingDog and the LLM provider to refresh patterns." : "Refresh Pattern Scout.";
  syncMenuButton.disabled = !xReadConfigured && trendsButton.disabled && patternsButton.disabled;
  syncMenuButton.title = syncMenuButton.disabled ? "Connect at least one sync source to run syncs." : "Choose a sync action.";
  renderAccountDetailDrawer();
}

function accountConnectionEntries(connections) {
  return [
    ["provider", connections.provider],
    ["openrouter", connections.openrouter],
    ["x_read", connections.x_read],
    ["x_write", connections.x_write],
    ["scrapingdog", connections.scrapingdog],
    ["daily_sync", connections.daily_sync],
  ];
}

function modelRoutingSummary(models, effectiveModels, defaultModel) {
  const fallback = defaultModel || effectiveModels.default || models.default || "Unset";
  return [
    `Default: ${modelDisplayName(effectiveModels.default || models.default || fallback)}`,
    `Strategist: ${modelDisplayName(effectiveModels.strategist || models.strategist || fallback)}`,
  ].join(" · ");
}

function modelDisplayName(value) {
  const model = String(value || "").trim();
  if (!model) return "Unset";
  return label(model.split("/").pop())
    .replace(/\bAi\b/g, "AI")
    .replace(/\bGpt\b/g, "GPT")
    .replace(/\bLlm\b/g, "LLM");
}

function syncSummary(sync, syncStatus, stats) {
  const postSync = sync?.x_posts_sync?.last_completed_at || "";
  const metricSync = sync?.x_metrics_sync?.last_completed_at || "";
  const latest = latestDate([postSync, metricSync]);
  if (latest) return `Posts and metrics last synced ${formatDateTime(latest)}.`;
  if (stats?.latest_imported_x_post_date) return `Latest imported post ${formatDateTime(stats.latest_imported_x_post_date)}.`;
  const error = syncStatus?.latest_error || latestSyncError(sync);
  if (error) return "Sync needs attention.";
  return "Posts and metrics have not synced yet.";
}

function latestDate(values) {
  return values
    .filter(Boolean)
    .sort((first, second) => Date.parse(second) - Date.parse(first))[0] || "";
}

function setModelInput(id, savedValue, effectiveValue) {
  const input = $(id);
  const value = savedValue || effectiveValue || "";
  input.value = value;
  input.dataset.inherited = savedValue ? "false" : "true";
  input.dataset.inheritedValue = savedValue ? "" : value;
}

function modelValueForSave(id) {
  const input = $(id);
  if (input.dataset.inherited === "true" && input.value === input.dataset.inheritedValue) {
    return "";
  }
  return input.value;
}

function fallbackConnections(status) {
  return {
    provider: {
      status: status.provider_configured ? "connected" : "not_connected",
      label: "LLM Provider",
      description: "Used for post generation and AI rewrites.",
    },
    openrouter: {
      status: status.openrouter_api_key_configured ? "connected" : "not_connected",
      label: "OpenRouter",
      description: "Routes model calls for Standard and Best Version generation.",
    },
    x_read: {
      status: status.x_read_configured ? "connected" : "not_connected",
      label: "X Read",
      description: "Imports posts and refreshes public metrics.",
    },
    x_write: {
      status: status.x_write_configured ? "connected" : "not_connected",
      label: "X Write",
      description: "Publishes approved scheduled posts.",
    },
    scrapingdog: {
      status: status.scrapingdog_configured ? "connected" : "not_connected",
      label: "ScrapingDog",
      description: "Source used to refresh trends and Pattern Scout examples.",
    },
    daily_sync: {
      status: status.daily_sync_configured ? "connected" : "not_configured",
      label: "Daily Sync",
      description: "Checks X for new posts and metrics through protected cron.",
    },
  };
}

function connectionRow(key, connection) {
  const item = connection || {};
  const status = connectionStatus(item);
  return `
    <article class="connection-row">
      <div>
        <strong>${escapeHtml(item.label || label(key))}</strong>
        <p>${escapeHtml(item.description || "")}</p>
      </div>
      <span class="status-badge ${escapeAttr(status)}">${escapeHtml(statusText(status))}</span>
    </article>
  `;
}

function connectionStatus(connection) {
  return String(connection?.status || "not_connected");
}

function statusText(status) {
  if (status === "connected") return "Connected";
  if (status === "not_configured") return "Not configured";
  if (status === "needs_setup") return "Needs setup";
  return "Not connected";
}

function syncRow(title, sync, description = "") {
  const item = sync || {};
  const summary = item.summary || {};
  const pieces = [];
  if (summary.imported !== undefined) pieces.push(`imported ${summary.imported}`);
  if (summary.duplicates !== undefined) pieces.push(`duplicates ${summary.duplicates}`);
  if (summary.updated !== undefined) pieces.push(`updated ${summary.updated}`);
  if (summary.new_posts_imported !== undefined) pieces.push(`imported ${summary.new_posts_imported}`);
  if (summary.metrics_updated !== undefined) pieces.push(`metrics ${summary.metrics_updated}`);
  if (summary.daily_trends !== undefined) pieces.push(`daily ${summary.daily_trends}`);
  if (summary.weekly_trends !== undefined) pieces.push(`weekly ${summary.weekly_trends}`);
  if (summary.enriched !== undefined) pieces.push(`enriched ${summary.enriched}`);
  if (summary.partial !== undefined) pieces.push(`partial ${summary.partial}`);
  if (summary.failed !== undefined) pieces.push(`failed ${summary.failed}`);
  const detail = [
    item.last_completed_at ? `Last synced ${formatDateTime(item.last_completed_at)}` : "Never synced",
    pieces.join(", "),
  ].filter(Boolean).join(" · ");
  return `
    <article class="sync-row">
      <div>
        <strong>${escapeHtml(title)}</strong>
        <p>${escapeHtml(description)}</p>
      </div>
      <span>${escapeHtml(detail)}</span>
    </article>
  `;
}

function latestSyncError(sync) {
  const items = [sync?.patterns_sync, sync?.trends_sync, sync?.x_daily_sync, sync?.x_posts_sync, sync?.x_metrics_sync].filter(Boolean);
  return items.find((item) => item.error_message)?.error_message || "";
}

async function saveAccount() {
  state.account = await api("/api/account", {
    method: "PATCH",
    body: JSON.stringify({
      models: {
        default: $("accountModel").value,
        strategist: modelValueForSave("accountStrategistModel"),
        natural_writer: modelValueForSave("accountNaturalWriterModel"),
        evaluator: modelValueForSave("accountEvaluatorModel"),
      },
    }),
  });
  renderAccount();
  showMessage("Account settings saved.");
}

async function resetAccountModels() {
  state.account = await api("/api/account", {
    method: "PATCH",
    body: JSON.stringify({ reset_model_settings: true }),
  });
  renderAccount();
  showMessage("Model settings reset to environment defaults.");
}

async function testProvider() {
  const payload = await api("/api/account/test-provider", {
    method: "POST",
    body: "{}",
  });
  showMessage(payload.message || "Provider test succeeded.");
}

function openModal(id) {
  closeTransientDropdowns();
  $(id).classList.remove("hidden");
  $(id).setAttribute("aria-hidden", "false");
}

function closeModal(id) {
  $(id).classList.add("hidden");
  $(id).setAttribute("aria-hidden", "true");
  closeCreatePostFieldMenus();
  if (id === "postModal") {
    state.postDetail = null;
    state.editingPostField = null;
    renderRecentPosts();
  }
}

window.closeModal = closeModal;

function isOpenById(id) {
  const node = $(id);
  return Boolean(node && !node.classList.contains("hidden"));
}

function hasOpenCreatePostFieldMenu() {
  return ["topic", "post_type"].some((field) => isOpenById(createPostFieldMenuId(field)));
}

function hasOpenTransientDropdown() {
  return Boolean(
    state.editingPostField ||
      isOpenById("reviewAddMenu") ||
      isOpenById("accountSyncMenu") ||
      hasOpenCreatePostFieldMenu()
  );
}

function closeTransientDropdowns({ exceptCreatePostField = "", keepPostFieldEditor = false } = {}) {
  closeReviewAddMenu();
  closeAccountSyncMenu();
  closeCreatePostFieldMenus(exceptCreatePostField);
  if (!keepPostFieldEditor) {
    closePostFieldEditor();
  }
}

function closeVisibleModal() {
  const modal = Array.from(document.querySelectorAll(".modal:not(.hidden)")).pop();
  if (!modal?.id) return false;
  closeModal(modal.id);
  return true;
}

function handleGlobalEscape(event) {
  if (event.key !== "Escape" || event.defaultPrevented) return;
  if (hasOpenTransientDropdown()) {
    event.preventDefault();
    closeTransientDropdowns();
    return;
  }
  if (state.accountDetail.open) {
    event.preventDefault();
    closeAccountDetailDrawer();
    return;
  }
  if (closeVisibleModal()) {
    event.preventDefault();
  }
}

window.openTrendsModal = async () => {
  openModal("trendsModal");
  await loadTrends();
};

function findQueueItem(id) {
  return [...state.reviewItems, ...state.scheduledItems].find((item) => item.id === id);
}

function emptyState(text) {
  return `<div class="empty-state">${escapeHtml(text)}</div>`;
}

function reviewEmptyState() {
  return emptyState("No posts to review.");
}

function setReviewAddMenuOpen(open) {
  $("reviewAddMenu").classList.toggle("hidden", !open);
  $("reviewAddButton").setAttribute("aria-expanded", open ? "true" : "false");
}

function toggleReviewAddMenu(event) {
  event.stopPropagation();
  if ($("reviewAddButton").disabled) return;
  const shouldOpen = $("reviewAddMenu").classList.contains("hidden");
  closeTransientDropdowns();
  setReviewAddMenuOpen(shouldOpen);
}

function closeReviewAddMenu() {
  if (!$("reviewAddMenu")) return;
  setReviewAddMenuOpen(false);
}

function setAccountSyncMenuOpen(open) {
  $("accountSyncMenu").classList.toggle("hidden", !open);
  $("accountSyncMenuButton").setAttribute("aria-expanded", open ? "true" : "false");
}

function toggleAccountSyncMenu(event) {
  event.stopPropagation();
  if ($("accountSyncMenuButton").disabled) return;
  const shouldOpen = $("accountSyncMenu").classList.contains("hidden");
  closeTransientDropdowns();
  setAccountSyncMenuOpen(shouldOpen);
}

function closeAccountSyncMenu() {
  if (!$("accountSyncMenu")) return;
  setAccountSyncMenuOpen(false);
}

function runAccountSyncMenuAction(event, loadingText, work) {
  event.stopPropagation();
  closeAccountSyncMenu();
  runButton($("accountSyncMenuButton"), loadingText, work);
}

const ACCOUNT_DETAIL_CONTENT = {
  connections: {
    title: "Connections",
    subtitle: "Service status and what each integration powers.",
  },
  models: {
    title: "Model Routing",
    subtitle: "Choose the models used for strategy, writing, and evaluation.",
  },
  sync: {
    title: "Sync",
    subtitle: "Last successful runs for imports, metrics, trends, and patterns.",
  },
  profile: {
    title: "Account",
    subtitle: "Current session and sign-out controls.",
  },
};

function openAccountDetailDrawer(panel = "connections") {
  state.accountDetail = { open: true, panel };
  renderAccountDetailDrawer();
  window.setTimeout(() => $("accountDetailCloseButton")?.focus(), 0);
}

function closeAccountDetailDrawer() {
  closeAccountSyncMenu();
  state.accountDetail.open = false;
  renderAccountDetailDrawer();
}

function renderAccountDetailDrawer() {
  const drawer = $("accountDetailDrawer");
  if (!drawer) return;
  const activePanel = state.accountDetail.panel || "connections";
  const content = ACCOUNT_DETAIL_CONTENT[activePanel] || ACCOUNT_DETAIL_CONTENT.connections;
  drawer.classList.toggle("hidden", !state.accountDetail.open);
  drawer.setAttribute("aria-hidden", state.accountDetail.open ? "false" : "true");
  $("accountDetailTitle").textContent = content.title;
  $("accountDetailSubtitle").textContent = content.subtitle;
  document.querySelectorAll("[data-account-panel]").forEach((panel) => {
    panel.classList.toggle("hidden", panel.dataset.accountPanel !== activePanel);
  });
}

function openAccountDetailFromRow(event, panel) {
  if (event.target.closest("button, a, input, select, textarea, form, .action-menu")) return;
  openAccountDetailDrawer(panel);
}

function handleAccountDetailRowKeydown(event, panel) {
  if (event.key !== "Enter" && event.key !== " ") return;
  if (event.target !== event.currentTarget) return;
  event.preventDefault();
  openAccountDetailDrawer(panel);
}

function setAssistantOpen(open) {
  state.assistant.open = open;
  renderAssistantDrawer();
  if (open) {
    loadAssistantStarterPrompts().catch(() => {
      state.assistant.prompts = ASSISTANT_FALLBACK_STARTERS;
      renderAssistantDrawer();
    });
    window.setTimeout(() => $("assistantInput")?.focus(), 80);
  }
}

function renderAssistantDrawer() {
  const drawer = $("assistantDrawer");
  const launcher = $("assistantLauncher");
  if (!drawer || !launcher) return;
  drawer.classList.toggle("hidden", !state.assistant.open);
  drawer.classList.toggle("expanded", state.assistant.expanded);
  drawer.setAttribute("aria-hidden", state.assistant.open ? "false" : "true");
  launcher.setAttribute("aria-expanded", state.assistant.open ? "true" : "false");
  const expandButton = $("assistantExpandButton");
  const expandLabel = state.assistant.expanded ? "Compress Ask Koinoniko" : "Expand Ask Koinoniko";
  expandButton.setAttribute("aria-label", expandLabel);
  expandButton.setAttribute("aria-expanded", state.assistant.expanded ? "true" : "false");
  expandButton.title = expandLabel;
  const starters = $("assistantStarterPrompts");
  starters.classList.toggle("hidden", !shouldShowAssistantStarters());
  if (shouldShowAssistantStarters()) {
    const prompts = state.assistant.prompts.length ? state.assistant.prompts : ASSISTANT_FALLBACK_STARTERS;
    starters.innerHTML = prompts
      .map((prompt) => `<button class="assistant-starter" type="button" data-prompt="${escapeAttr(prompt)}">${escapeHtml(prompt)}</button>`)
      .join("");
  } else {
    starters.innerHTML = "";
  }
  const messages = state.assistant.messages.map(assistantMessageHtml);
  if (state.assistant.loading) {
    messages.push(`<div class="assistant-message assistant assistant-thinking"><span class="assistant-loading">Thinking</span></div>`);
  }
  const messageList = $("assistantMessages");
  messageList.classList.toggle("has-chat", Boolean(messages.length));
  messageList.innerHTML = messages.length
    ? messages.join("")
    : `<div class="assistant-empty">Ask what is working, what feels weak, or what Gavin should post next.</div>`;
  $("assistantSendButton").disabled = state.assistant.loading;
  scrollAssistantToBottom();
}

function shouldShowAssistantStarters() {
  return state.assistant.open && state.assistant.messages.length === 0 && !state.assistant.loading;
}

async function loadAssistantStarterPrompts() {
  if (state.assistant.prompts.length) return;
  const payload = await api("/api/assistant/starter-prompts");
  state.assistant.prompts = (payload.items || []).filter(Boolean);
  renderAssistantDrawer();
}

function toggleAssistantExpanded() {
  state.assistant.expanded = !state.assistant.expanded;
  renderAssistantDrawer();
}

function assistantMessageHtml(message) {
  const role = message.role === "user" ? "user" : "assistant";
  const errorClass = message.error ? " error" : "";
  return `
    <div class="assistant-message ${role}${errorClass}">
      <div class="assistant-message-text">${assistantRichTextHtml(message.text)}</div>
      ${assistantSourcesHtml(message.sources || [])}
      ${assistantActionsHtml(message.actions || [])}
    </div>
  `;
}

function assistantRichTextHtml(text) {
  const lines = String(text || "").split(/\r?\n/);
  const html = [];
  let paragraph = [];
  let listType = "";
  let listItems = [];
  let quoteLines = [];

  const flushParagraph = () => {
    if (!paragraph.length) return;
    html.push(`<p>${assistantInlineHtml(paragraph.join(" "))}</p>`);
    paragraph = [];
  };
  const flushList = () => {
    if (!listItems.length) return;
    html.push(`<${listType}>${listItems.map((item) => `<li>${assistantInlineHtml(item)}</li>`).join("")}</${listType}>`);
    listItems = [];
    listType = "";
  };
  const flushQuote = () => {
    if (!quoteLines.length) return;
    html.push(`<blockquote>${assistantInlineHtml(quoteLines.join(" "))}</blockquote>`);
    quoteLines = [];
  };
  const flushAll = () => {
    flushParagraph();
    flushList();
    flushQuote();
  };

  lines.forEach((rawLine) => {
    const line = rawLine.trim();
    if (!line) {
      flushAll();
      return;
    }
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      flushAll();
      const level = heading[1].length === 1 ? "h3" : "h4";
      html.push(`<${level}>${assistantInlineHtml(heading[2])}</${level}>`);
      return;
    }
    const quote = line.match(/^>\s?(.+)$/);
    if (quote) {
      flushParagraph();
      flushList();
      quoteLines.push(quote[1]);
      return;
    }
    const unordered = line.match(/^[-*]\s+(.+)$/);
    const ordered = line.match(/^\d+[.)]\s+(.+)$/);
    if (unordered || ordered) {
      flushParagraph();
      flushQuote();
      const nextType = ordered ? "ol" : "ul";
      if (listType && listType !== nextType) flushList();
      listType = nextType;
      listItems.push((unordered || ordered)[1]);
      return;
    }
    flushList();
    flushQuote();
    paragraph.push(line);
  });
  flushAll();
  return html.join("");
}

function assistantInlineHtml(value) {
  return escapeHtml(value)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
}

function assistantSourcesHtml(sources) {
  const visible = sources.filter((source) => source.label).slice(0, 4);
  if (!visible.length) return "";
  return `
    <div class="assistant-sources">
      <span>Sources</span>
      ${visible.map((source) => `<small>${escapeHtml(source.label)}${source.metric ? ` · ${escapeHtml(source.metric)}` : ""}</small>`).join("")}
    </div>
  `;
}

function assistantActionsHtml(actions) {
  const visible = actions.filter((action) => action.requires_confirmation).slice(0, 3);
  if (!visible.length) return "";
  return `
    <div class="assistant-actions">
      ${visible.map((action) => `<span>${escapeHtml(action.label)} · confirm first</span>`).join("")}
    </div>
  `;
}

async function sendAssistantMessage() {
  const input = $("assistantInput");
  const text = input.value.trim();
  if (!text || state.assistant.loading) return;
  if (!state.assistant.conversationId) {
    state.assistant.conversationId = `browser-${Date.now()}`;
  }
  state.assistant.messages.push({ role: "user", text });
  input.value = "";
  state.assistant.loading = true;
  renderAssistantDrawer();
  try {
    const payload = await api("/api/assistant/chat", {
      method: "POST",
      body: JSON.stringify({
        message: text,
        conversation_id: state.assistant.conversationId,
      }),
    });
    state.assistant.messages.push({
      role: "assistant",
      text: payload.answer || "",
      sources: payload.sources || [],
      actions: payload.suggested_actions || [],
    });
  } catch (error) {
    state.assistant.messages.push({
      role: "assistant",
      text: error.message,
      error: true,
    });
  } finally {
    state.assistant.loading = false;
    renderAssistantDrawer();
  }
}

function scrollAssistantToBottom() {
  const messages = $("assistantMessages");
  if (!messages) return;
  window.requestAnimationFrame(() => {
    messages.scrollTop = messages.scrollHeight;
  });
}

function detail(labelText, value) {
  return `
    <div class="detail-item">
      <span>${escapeHtml(labelText)}</span>
      <strong>${escapeHtml(value ?? "")}</strong>
    </div>
  `;
}

function detailHtml(labelText, valueHtml) {
  return `
    <div class="detail-item">
      <span>${escapeHtml(labelText)}</span>
      ${valueHtml}
    </div>
  `;
}

function summarize(text, maxLength = 96) {
  const cleaned = String(text || "").replace(/\s+/g, " ").trim();
  if (!cleaned) return "Untitled post";
  const firstSentence = cleaned.split(". ")[0];
  const summary = firstSentence.length >= 18 ? firstSentence : cleaned;
  return summary.length <= maxLength ? summary : `${summary.slice(0, maxLength - 3).trim()}...`;
}

function label(value) {
  const acronyms = {
    ai: "AI",
    api: "API",
    cta: "CTA",
    er: "ER",
    gtm: "GTM",
    llm: "LLM",
    llms: "LLMs",
    url: "URL",
    x: "X",
  };
  return String(value || "")
    .replaceAll("_", " ")
    .replaceAll("-", " ")
    .split(/\s+/)
    .filter(Boolean)
    .map((word) => {
      const normalized = word.toLowerCase();
      if (acronyms[normalized]) return acronyms[normalized];
      return `${normalized.slice(0, 1).toUpperCase()}${normalized.slice(1)}`;
    })
    .join(" ");
}

function qualityLabel(value) {
  const mode = String(value || "");
  if (mode === "best_version") return "Best Version";
  if (mode === "standard") return "Standard";
  return "";
}

function trendSummary(item) {
  if (item.trend_used) return item.trend_used;
  return (item.trend_sources || [])
    .map((trend) => trend.title)
    .filter(Boolean)
    .join(", ");
}

function metric(value) {
  return value === null || value === undefined ? "-" : formatNumber(value);
}

function formatNumber(value) {
  return new Intl.NumberFormat().format(value || 0);
}

function formatRate(value) {
  if (value === null || value === undefined) return "-";
  return `${(Number(value) * 100).toFixed(2)}%`;
}

function formatDate(value) {
  if (!value) return "";
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) return String(value).slice(0, 10);
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" }).format(new Date(parsed));
}

function formatDateTime(value) {
  if (!value) return "Not scheduled";
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) return value;
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(parsed));
}

function nextDefaultSchedule() {
  const date = new Date(Date.now() + 24 * 60 * 60 * 1000);
  date.setMinutes(0, 0, 0);
  return date.toISOString();
}

function dateTimeLocalValue(value) {
  if (!value) return "";
  const date = value instanceof Date ? value : new Date(value);
  if (!Number.isFinite(date.getTime())) return String(value).slice(0, 16);
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60 * 1000);
  return local.toISOString().slice(0, 16);
}

function schedulePresetValue(preset) {
  const date = new Date();
  if (preset === "two_hours") {
    date.setTime(Date.now() + 2 * 60 * 60 * 1000);
    date.setMinutes(0, 0, 0);
    if (date.getTime() <= Date.now()) date.setHours(date.getHours() + 1);
    return dateTimeLocalValue(date);
  }
  date.setDate(date.getDate() + 1);
  if (preset === "next_weekday_morning") {
    while (date.getDay() === 0 || date.getDay() === 6) {
      date.setDate(date.getDate() + 1);
    }
  }
  date.setHours(9, 0, 0, 0);
  return dateTimeLocalValue(date);
}

function latestDate(values) {
  const parsed = values
    .filter(Boolean)
    .map((value) => ({ value, time: Date.parse(value) }))
    .filter((item) => Number.isFinite(item.time))
    .sort((a, b) => b.time - a.time);
  return parsed[0]?.value || "";
}

function setup() {
  setupSidebar();
  document.querySelectorAll(".nav-button[data-view]").forEach((button) => {
    button.addEventListener("click", () => setView(button.dataset.view));
  });
  document.querySelectorAll("[data-close-modal]").forEach((node) => {
    node.addEventListener("click", () => closeModal(node.dataset.closeModal));
  });
  ["postSearch", "postVoiceFilter", "postDateFrom", "postDateTo"].forEach((id) => {
    $(id).addEventListener("input", renderRecentPosts);
  });
  ["voiceSearch", "voiceTopicFilter", "voiceSort"].forEach((id) => {
    $(id).addEventListener("input", renderVoiceExamples);
  });
  $("voiceContextButton").addEventListener("click", () => openModal("voiceContextModal"));
  $("voiceRulesButton").addEventListener("click", () => openModal("voiceRulesModal"));
  $("voiceAvoidAddButton").addEventListener("click", addAvoidPhrase);
  $("voiceAvoidSaveButton").addEventListener("click", (event) => runButton(event.currentTarget, "Saving...", saveAvoidPhrases));
  $("voiceAvoidPhraseInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      addAvoidPhrase();
    }
  });
  document.querySelectorAll(".posts-table th[data-sort-key]").forEach((header) => {
    header.querySelector("button")?.addEventListener("click", () => setRecentPostsSort(header.dataset.sortKey));
  });

  $("reviewAddButton").addEventListener("click", toggleReviewAddMenu);
  $("reviewAddMenu").addEventListener("click", (event) => event.stopPropagation());
  $("reviewAddPostButton").addEventListener("click", () => {
    closeReviewAddMenu();
    openCreatePostModal();
  });
  $("reviewAddPlanButton").addEventListener("click", () => {
    closeReviewAddMenu();
    openModal("planModal");
  });
  document.addEventListener("click", closeReviewAddMenu);
  [
    ["accountConnectionsRow", "connections"],
    ["accountModelsRow", "models"],
    ["accountSyncRow", "sync"],
    ["accountProfileRow", "profile"],
  ].forEach(([id, panel]) => {
    const row = $(id);
    row.addEventListener("click", (event) => openAccountDetailFromRow(event, panel));
    row.addEventListener("keydown", (event) => handleAccountDetailRowKeydown(event, panel));
  });
  $("accountConnectionsManageButton").addEventListener("click", (event) => {
    event.stopPropagation();
    openAccountDetailDrawer("connections");
  });
  $("accountModelsManageButton").addEventListener("click", (event) => {
    event.stopPropagation();
    openAccountDetailDrawer("models");
  });
  $("accountSyncManageButton").addEventListener("click", (event) => {
    event.stopPropagation();
    openAccountDetailDrawer("sync");
  });
  $("accountDetailCloseButton").addEventListener("click", closeAccountDetailDrawer);
  $("accountDetailBackdrop").addEventListener("click", closeAccountDetailDrawer);
  $("accountSyncMenuButton").addEventListener("click", toggleAccountSyncMenu);
  $("accountSyncMenu").addEventListener("click", (event) => event.stopPropagation());
  document.addEventListener("click", closePostFieldEditor);
  setupCreatePostFieldCombo("topic");
  setupCreatePostFieldCombo("post_type");
  document.addEventListener("click", closeCreatePostFieldMenus);
  document.addEventListener("keydown", handleGlobalEscape);
  $("generatePostButton").addEventListener("click", (event) => runButton(event.currentTarget, "Generating...", createPost));
  $("planWeekButton").addEventListener("click", (event) => runButton(event.currentTarget, "Creating...", planWeek));
  $("trendDailyButton").addEventListener("click", () => {
    state.selectedTrendWindow = "daily";
    renderDashboardTrends();
  });
  $("trendWeeklyButton").addEventListener("click", () => {
    state.selectedTrendWindow = "weekly";
    renderDashboardTrends();
  });
  $("patternWeeklyButton").addEventListener("click", () => {
    state.selectedPatternWindow = "weekly";
    renderDashboardPatterns();
  });
  $("patternMonthlyButton").addEventListener("click", () => {
    state.selectedPatternWindow = "monthly";
    renderDashboardPatterns();
  });
  $("createPostTrend").addEventListener("change", syncCreatePostSelectionsFromInputs);
  $("createPostPattern").addEventListener("change", syncCreatePostSelectionsFromInputs);
  $("loadTrendsButton").addEventListener("click", (event) => runButton(event.currentTarget, "Loading...", loadTrends));
  $("saveAccountButton").addEventListener("click", (event) => runButton(event.currentTarget, "Saving...", saveAccount));
  $("accountSyncAllButton").addEventListener("click", (event) => runAccountSyncMenuAction(event, "Syncing...", syncAllXData));
  $("accountSyncPostsButton").addEventListener("click", (event) => runAccountSyncMenuAction(event, "Syncing...", syncPosts));
  $("accountSyncMetricsButton").addEventListener("click", (event) => runAccountSyncMenuAction(event, "Syncing...", syncMetrics));
  $("accountRefreshTrendsButton").addEventListener("click", (event) => runAccountSyncMenuAction(event, "Syncing...", refreshTrendsFromAccount));
  $("accountRefreshPatternsButton").addEventListener("click", (event) => runAccountSyncMenuAction(event, "Syncing...", refreshPatternsFromAccount));
  document.addEventListener("click", closeAccountSyncMenu);
  $("updateVoiceButton").addEventListener("click", (event) => runButton(event.currentTarget, "Updating...", updateVoice));
  $("assistantLauncher").addEventListener("click", () => setAssistantOpen(true));
  $("assistantExpandButton").addEventListener("click", toggleAssistantExpanded);
  $("assistantCloseButton").addEventListener("click", () => setAssistantOpen(false));
  $("assistantStarterPrompts").addEventListener("click", (event) => {
    const button = event.target.closest("[data-prompt]");
    if (!button) return;
    $("assistantInput").value = button.dataset.prompt || "";
    sendAssistantMessage().catch((error) => showMessage(error.message, true));
  });
  $("assistantForm").addEventListener("submit", (event) => {
    event.preventDefault();
    sendAssistantMessage().catch((error) => showMessage(error.message, true));
  });
  $("assistantInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendAssistantMessage().catch((error) => showMessage(error.message, true));
    }
  });

  $("reviewList").addEventListener("dragover", (event) => event.preventDefault());
  $("reviewList").addEventListener("drop", (event) => {
    event.preventDefault();
    runButton(null, "", () => handleDrop("review")).catch((error) => showMessage(error.message, true));
  });
  $("scheduledList").addEventListener("dragover", (event) => event.preventDefault());
  $("scheduledList").addEventListener("drop", (event) => {
    event.preventDefault();
    runButton(null, "", () => handleDrop("scheduled")).catch((error) => showMessage(error.message, true));
  });

  const monday = new Date();
  monday.setDate(monday.getDate() + ((8 - monday.getDay()) % 7 || 7));
  $("planWeekStart").value = monday.toISOString().slice(0, 10);
  refresh().catch((error) => showMessage(error.message, true));
}

setup();
