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
  reviewItems: [],
  scheduledItems: [],
  savedTrends: [],
  selectedTrendWindow: "daily",
  selectedPatternWindow: "daily",
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
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  const text = await response.text();
  const payload = text ? JSON.parse(text) : {};
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
  const box = $("message");
  box.textContent = text;
  box.classList.toggle("error", isError);
  box.classList.remove("hidden");
  window.setTimeout(() => box.classList.add("hidden"), isError ? 9000 : 5000);
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
      <div>
        <h3>Voice</h3>
        <p class="muted">${voice.is_voice_example ? "Used as a Voice Example." : "Not currently used as a Voice Example."}</p>
      </div>
      <label class="inline-check">
        <input id="postVoiceExample" type="checkbox" ${voice.is_voice_example ? "checked" : ""}>
        Use this as a Voice Example
      </label>
      <label>Why this worked
        <textarea id="postWhyWorked" rows="4" placeholder="What made this post sound like Gavin and work with the audience?">${escapeHtml(voice.why_this_worked || "")}</textarea>
      </label>
      <details class="advanced-fields">
        <summary>Advanced fields</summary>
        <div class="form-grid">
          <label>Hook<input id="postVoiceHook" value="${escapeAttr(voice.hook || "")}"></label>
          <label>Structure<input id="postVoiceStructure" value="${escapeAttr(voice.structure || "")}"></label>
          <label>Tone<input id="postVoiceTone" value="${escapeAttr(voice.tone || "")}"></label>
          <label>CTA<input id="postVoiceCta" value="${escapeAttr(voice.cta || "")}"></label>
          <label>Formatting<input id="postVoiceFormatting" value="${escapeAttr(voice.formatting || "")}"></label>
          <label>Pattern<input id="postVoicePattern" value="${escapeAttr(voice.pattern || "")}"></label>
        </div>
      </details>
      <div class="actions left">
        <button onclick="savePostVoice(event)">Save</button>
      </div>
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

window.savePostVoice = (event) => {
  runButton(event.currentTarget, "Saving...", async () => {
    const post = state.postDetail;
    const payload = await api(`/api/posts/${encodeURIComponent(post.id)}/voice`, {
      method: "POST",
      body: JSON.stringify({
        voice_example: $("postVoiceExample").checked,
        why_this_worked: $("postWhyWorked").value,
        hook: $("postVoiceHook").value,
        structure: $("postVoiceStructure").value,
        tone: $("postVoiceTone").value,
        cta: $("postVoiceCta").value,
        formatting: $("postVoiceFormatting").value,
        pattern: $("postVoicePattern").value,
      }),
    });
    state.postDetail = payload.post;
    showMessage(payload.message || "Voice settings saved.");
    await refresh();
    if (state.view === "voice") await loadVoice();
    renderPostModal();
  });
};

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
  const summary = cleanString(item.summary);
  const relevance = trendRelevanceText(item);
  const leadpoetAngle = cleanString(item.leadpoet_angle);
  return `
    <article class="trend-row" role="button" tabindex="0" onclick='openTrendDetails(${jsArg(item.id)})' onkeydown='openTrendDetailsFromKey(event, ${jsArg(item.id)})'>
      <div class="meta-row">
        <span>${escapeHtml(topicLabel(item.topic || ""))}</span>
        ${item.source_url ? `<a href="${escapeAttr(item.source_url)}" target="_blank" rel="noreferrer" onclick="event.stopPropagation()">Source</a>` : ""}
      </div>
      <h3>${escapeHtml(item.title)}</h3>
      ${summary ? `<p class="trend-summary">${escapeHtml(summary)}</p>` : ""}
      <p class="trend-relevance"><strong>Why relevant:</strong> ${escapeHtml(relevance)}</p>
      ${leadpoetAngle && leadpoetAngle !== relevance ? `<p class="trend-angle"><strong>Leadpoet angle:</strong> ${escapeHtml(leadpoetAngle)}</p>` : ""}
      <div class="actions left">
        <button class="secondary" onclick='useTrendInPost(event, ${jsArg(item.id)})'>Use in Post</button>
      </div>
    </article>
  `;
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
  $("patternDailyButton").classList.toggle("active", state.selectedPatternWindow === "daily");
  $("patternWeeklyButton").classList.toggle("active", state.selectedPatternWindow === "weekly");
  const patterns = groups[state.selectedPatternWindow] || [];
  $("patternList").innerHTML = patterns.length
    ? patterns.slice(0, 5).map(patternCard).join("")
    : `<p class="muted">No ${escapeHtml(state.selectedPatternWindow)} patterns yet. Add patterns manually or refresh Pattern Scout.</p>`;
}

function patternCard(pattern) {
  return `
    <article class="pattern-card" role="button" tabindex="0" onclick='openPatternDetails(${jsArg(pattern.id)})' onkeydown='openPatternDetailsFromKey(event, ${jsArg(pattern.id)})'>
      <div class="meta-row">
        <span>${escapeHtml(label(pattern.category || "other"))}</span>
        <span>${escapeHtml(topicLabel(pattern.topic || "No topic"))}</span>
        <span>${escapeHtml(patternSignal(pattern))}</span>
      </div>
      <h3>${escapeHtml(pattern.pattern_name || "Observed pattern")}</h3>
      <p>${escapeHtml(pattern.structure || pattern.source_summary || "")}</p>
      ${pattern.why_it_worked ? `<p><strong>Why this worked:</strong> ${escapeHtml(pattern.why_it_worked)}</p>` : ""}
      <div class="actions left">
        <button class="secondary" onclick='usePatternInPost(event, ${jsArg(pattern.id)})'>Use Pattern</button>
      </div>
    </article>
  `;
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
  state.selectedPatternForCreate = null;
  $("createPostInstruction").value = trend.leadpoet_angle || trend.title || "";
  $("createPostTopic").value = trend.topic || "Leadpoet";
  $("createPostUseTrends").checked = true;
  $("createPostUsePatterns").checked = false;
  renderCreatePostPatternHint();
  openCreatePostModal();
};

function findTrendItem(id) {
  const groups = state.dashboard?.trend_groups || {};
  return [
    ...(groups.daily || []),
    ...(groups.weekly || []),
    ...(state.dashboard?.trends || []),
    ...(state.savedTrends || []),
  ].find((item) => item.id === id);
}

function findPatternItem(id) {
  const groups = state.dashboard?.pattern_groups || {};
  return [
    ...(groups.daily || []),
    ...(groups.weekly || []),
  ].find((item) => item.id === id);
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
  const pattern = state.selectedPatternForCreate;
  if (!pattern) {
    hint.classList.add("hidden");
    hint.innerHTML = "";
    return;
  }
  hint.classList.remove("hidden");
  hint.innerHTML = `
    <strong>${escapeHtml(pattern.pattern_name || "Selected pattern")}</strong>
    <span>${escapeHtml(pattern.structure || pattern.adaptation_rule || "Use the structure, not the wording.")}</span>
    <button class="quiet-button" type="button" onclick="clearSelectedPattern(event)">Remove</button>
  `;
}

window.clearSelectedPattern = (event) => {
  event?.stopPropagation();
  state.selectedPatternForCreate = null;
  $("createPostUsePatterns").checked = false;
  renderCreatePostPatternHint();
};

window.usePatternInPost = (event, id) => {
  event.stopPropagation();
  const pattern = state.patternDetail?.id === id ? state.patternDetail : findPatternItem(id);
  if (!pattern) return;
  state.selectedPatternForCreate = pattern;
  $("createPostUsePatterns").checked = true;
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

function updatePatternStatus(event, id, action) {
  runButton(event.currentTarget, "Saving...", async () => {
    await api(`/api/patterns/${encodeURIComponent(id)}/${action}`, { method: "POST", body: "{}" });
    closeModal("patternModal");
    showMessage(action === "archive" ? "Pattern archived." : "Pattern ignored.");
    await refresh();
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
  $("queueModalTitle").textContent = item.focus || summarize(item.text, 70);
  $("queueModalBody").innerHTML = `
    <label>Post text<textarea id="detailText" rows="8">${escapeHtml(item.text)}</textarea></label>
    <div class="form-grid">
      <label>Topic<input id="detailTopic" value="${escapeAttr(item.topic)}"></label>
      <label>Post type<input id="detailPostType" value="${escapeAttr(item.post_type)}"></label>
      <label>Scheduled time<input id="detailScheduled" value="${escapeAttr(item.scheduled_at)}"></label>
      <label>Status<input value="${escapeAttr(label(item.status))}" disabled></label>
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
    if (!item?.scheduled_at) {
      const scheduledAt = window.prompt("Scheduled date/time", nextDefaultSchedule()) || "";
      if (!scheduledAt.trim()) throw new Error("Schedule required before approval.");
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
  renderCreatePostPatternHint();
  openModal("createPostModal");
  window.setTimeout(() => $("createPostInstruction").focus(), 0);
}

async function createPost() {
  const instruction = $("createPostInstruction").value.trim();
  if (!instruction) throw new Error("Post idea / instruction is required.");
  const payload = await api("/api/content/create-post", {
    method: "POST",
    body: JSON.stringify({
      instruction,
      topic: $("createPostTopic").value || "Leadpoet",
      post_type: $("createPostType").value || "founder_insight",
      use_trends: $("createPostUseTrends").checked,
      use_patterns: $("createPostUsePatterns").checked,
      pattern_ids: state.selectedPatternForCreate ? [state.selectedPatternForCreate.id] : [],
      best_version: $("createPostQuality").value === "best_version",
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
  $("createPostUseTrends").checked = false;
  $("createPostUsePatterns").checked = false;
  state.selectedPatternForCreate = null;
  renderCreatePostPatternHint();
  $("createPostQuality").value = "standard";
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
      best_version: $("planQuality").value === "best_version",
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
    body: JSON.stringify({ limit: 5, window: "daily" }),
  });
  showMessage(`Patterns refreshed. Stored ${payload.stored || 0}; skipped ${payload.skipped || 0}.`);
  await refreshAfterAccountAction();
}

async function refreshAfterAccountAction() {
  await refresh();
  if (state.view === "account") {
    await loadAccount();
  }
}

async function loadVoice() {
  const [status, current, examples, suggestions] = await Promise.all([
    api("/api/voice/status"),
    api("/api/voice/current"),
    api("/api/voice/examples"),
    api("/api/voice/suggestions"),
  ]);
  state.voice = {
    status,
    current,
    examples: examples.items || [],
    suggestions: suggestions.items || [],
  };
  renderVoice();
}

function renderVoice() {
  const voice = state.voice || {};
  const status = voice.status || {};
  $("voiceStatusLabel").textContent = status.label || "Loading";
  $("voiceStatusDescription").textContent = status.description || "";
  $("voiceExampleCount").textContent = String(status.examples_count || 0);
  $("voiceLastUpdated").textContent = status.last_updated_at ? formatDateTime(status.last_updated_at) : "Never";
  $("updateVoiceButton").disabled = status.status === "needs_examples";
  renderCurrentVoice();
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
    "Avoid": "What to avoid",
    "What to avoid": "What to avoid",
    "Reusable writing principles": "Reusable writing principles",
  };
  const preferredOrder = [
    "Voice traits",
    "What strong posts tend to do",
    "What to avoid",
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
  return `
    <article class="voice-card">
      <div class="voice-card-main">
        <div class="meta-row">
          <span>${escapeHtml(formatDate(post.published_at))}</span>
          <span>${escapeHtml(topicLabel(post.topic || "No topic"))}</span>
          <span>${escapeHtml(label(post.post_type || "post"))}</span>
          <span>${escapeHtml(voicePerformanceSignal(post))}</span>
        </div>
        <h3>${escapeHtml(post.summary || summarize(post.text, 110))}</h3>
        <p><strong>Why this worked:</strong> ${escapeHtml(post.why_this_worked || "No note yet.")}</p>
      </div>
      <div class="actions left">
        <button class="secondary" onclick='openPostDetails(${jsArg(post.id)})'>Edit</button>
        ${post.url ? `<a class="button-link secondary-link" href="${escapeAttr(post.url)}" target="_blank" rel="noreferrer">Open on X</a>` : ""}
        ${removable ? `<button class="quiet-button" onclick='removeVoiceExample(event, ${jsArg(post.id)})'>Remove</button>` : ""}
      </div>
    </article>
  `;
}

function voiceSuggestionCard(post) {
  return `
    <article class="voice-card suggested">
      <div class="voice-card-main">
        <div class="meta-row">
          <span>${escapeHtml(post.reason || "Suggested by performance")}</span>
          <span>${escapeHtml(topicLabel(post.topic || "No topic"))}</span>
        </div>
        <h3>${escapeHtml(post.summary || summarize(post.text, 110))}</h3>
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
  const payload = await api("/api/voice/update", {
    method: "POST",
    body: "{}",
  });
  showMessage(`${payload.message || "Voice updated from selected examples."} Used ${payload.examples_used || 0} Voice Examples.`);
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
  $("accountStatusGrid").innerHTML = [
    connectionRow("provider", connections.provider),
    connectionRow("openrouter", connections.openrouter),
    connectionRow("x_read", connections.x_read),
    connectionRow("x_write", connections.x_write),
    connectionRow("scrapingdog", connections.scrapingdog),
    connectionRow("daily_sync", connections.daily_sync),
  ].join("");
  $("accountSyncGrid").innerHTML = [
    syncRow("Last X post sync", sync.x_posts_sync, "New posts imported from X"),
    syncRow("Last X metrics sync", sync.x_metrics_sync, "Fresh public metric snapshots"),
    syncRow("Last trend refresh", sync.trends_sync, "Daily and weekly trend context"),
    syncRow("Last pattern refresh", sync.patterns_sync, "Reusable post mechanics from Pattern Scout"),
    syncPlainRow("Total X posts stored", String(stats.total_x_posts_stored || 0), "Stored authored X posts"),
    syncPlainRow("Latest imported X post", stats.latest_imported_x_post_date ? formatDateTime(stats.latest_imported_x_post_date) : "None yet", "Newest stored X post"),
    syncPlainRow("Latest sync error", syncStatus.latest_error || latestSyncError(sync) || "None", "Most recent sync issue"),
  ].join("");
  const models = settings.models || {};
  const effectiveModels = settings.effective_models || {};
  const defaultModel = models.default || effectiveModels.default || "";
  $("accountModel").value = defaultModel;
  setModelInput("accountStrategistModel", models.strategist, effectiveModels.strategist || defaultModel);
  setModelInput("accountNaturalWriterModel", models.natural_writer, effectiveModels.natural_writer || defaultModel);
  setModelInput("accountEvaluatorModel", models.evaluator, effectiveModels.evaluator || defaultModel);
  const security = account.security || {};
  $("accountSecurityBlock").innerHTML = `
    <div class="security-row">
      <div>
        <span>Signed in as</span>
        <strong>${escapeHtml(security.username || "Configured user")}</strong>
      </div>
      <form method="post" action="/logout">
        <button class="secondary" type="submit">Log Out</button>
      </form>
    </div>
    <p class="muted">Change password from the CLI with <code>set-web-login</code>.</p>
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

function syncPlainRow(title, value, description = "") {
  return `
    <article class="sync-row">
      <div>
        <strong>${escapeHtml(title)}</strong>
        <p>${escapeHtml(description)}</p>
      </div>
      <span>${escapeHtml(value)}</span>
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
  $(id).classList.remove("hidden");
  $(id).setAttribute("aria-hidden", "false");
}

function closeModal(id) {
  $(id).classList.add("hidden");
  $(id).setAttribute("aria-hidden", "true");
  if (id === "postModal") {
    state.postDetail = null;
    state.editingPostField = null;
    renderRecentPosts();
  }
}

window.closeModal = closeModal;

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
  setReviewAddMenuOpen($("reviewAddMenu").classList.contains("hidden"));
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
  setAccountSyncMenuOpen($("accountSyncMenu").classList.contains("hidden"));
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
  $("assistantExpandButton").textContent = state.assistant.expanded ? "Narrow" : "Expand";
  $("assistantExpandButton").setAttribute(
    "aria-label",
    state.assistant.expanded ? "Narrow Ask Koinoniko" : "Expand Ask Koinoniko"
  );
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
    messages.push(`<div class="assistant-message assistant"><span class="assistant-loading">Thinking...</span></div>`);
  }
  $("assistantMessages").innerHTML = messages.length
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
      <div class="assistant-message-text">${paragraphHtml(message.text)}</div>
      ${assistantSourcesHtml(message.sources || [])}
      ${assistantActionsHtml(message.actions || [])}
    </div>
  `;
}

function paragraphHtml(text) {
  const paragraphs = String(text || "")
    .split(/\n{2,}/)
    .map((part) => part.trim())
    .filter(Boolean);
  if (!paragraphs.length) return "";
  return paragraphs.map((part) => `<p>${escapeHtml(part).replaceAll("\n", "<br>")}</p>`).join("");
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
  $("accountSyncMenuButton").addEventListener("click", toggleAccountSyncMenu);
  $("accountSyncMenu").addEventListener("click", (event) => event.stopPropagation());
  document.addEventListener("click", closePostFieldEditor);
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
  $("patternDailyButton").addEventListener("click", () => {
    state.selectedPatternWindow = "daily";
    renderDashboardPatterns();
  });
  $("patternWeeklyButton").addEventListener("click", () => {
    state.selectedPatternWindow = "weekly";
    renderDashboardPatterns();
  });
  $("createPostUsePatterns").addEventListener("change", () => {
    if (!$("createPostUsePatterns").checked) {
      state.selectedPatternForCreate = null;
    }
    renderCreatePostPatternHint();
  });
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
