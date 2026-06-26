const DEFAULT_SYNC_USERNAME = "gavinzaentz";

const state = {
  view: "dashboard",
  dashboard: null,
  analytics: null,
  status: null,
  account: null,
  reviewItems: [],
  scheduledItems: [],
  draggedQueueId: "",
  queueDetail: null,
  recentPostsSort: { key: "date", direction: "desc" },
};

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

const views = {
  dashboard: {
    title: "Dashboard",
    meta: "What has been posted, what is current, and what is working.",
  },
  reviewer: {
    title: "Content Reviewer",
    meta: "Review, improve, approve, and schedule X posts.",
  },
  account: {
    title: "Account",
    meta: "Settings, model choice, and connection status.",
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
  $("viewTitle").textContent = views[view].title;
  $("viewMeta").textContent = views[view].meta;
  renderCurrentView();
}

async function refresh() {
  state.status = await api("/api/status");
  const [dashboard, analytics, review, scheduled] = await Promise.all([
    api("/api/dashboard"),
    api("/api/analytics"),
    loadQueueStatuses(["draft", "needs_review", "failed"]),
    api("/api/queue?limit=100&status=approved"),
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
  if (state.view === "account") loadAccount().catch((error) => showMessage(error.message, true));
}

function updateGlobalControls() {
  const reviewCount = state.reviewItems.length;
  $("reviewNavBadge").textContent = String(reviewCount);
  $("reviewNavBadge").classList.toggle("muted-badge", reviewCount === 0);

  const dueCount = (state.dashboard?.due || []).length;
  [$("publishDueButton"), $("publishDueReviewerButton")].forEach((button) => {
    if (!button) return;
    button.classList.toggle("hidden", dueCount === 0);
    button.textContent = dueCount ? `Post Due (${dueCount})` : "Post Due";
    button.disabled = dueCount === 0 || !state.status?.x_write_configured;
    button.title = state.status?.x_write_configured ? "" : "X write auth is not connected.";
  });

  [$("createPlanButton"), $("createPlanReviewerButton")].forEach((button) => {
    button.disabled = !state.status?.provider_configured;
    button.title = state.status?.provider_configured ? "" : "Provider-backed generation is not connected.";
  });

  $("syncPostsButton").disabled = !state.status?.x_read_configured;
  $("syncPostsButton").title = state.status?.x_read_configured ? "" : "X read auth is not connected.";

  $("syncMetricsButton").disabled = !state.status?.x_read_configured;
  $("syncMetricsButton").title = state.status?.x_read_configured ? "" : "X read auth is not connected.";

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
  renderSignals();
  renderDashboardSyncLine();
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
  const from = $("postDateFrom").value;
  const to = $("postDateTo").value;
  const filtered = (state.dashboard?.recent_posts || []).filter((post) => {
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
  return `
    <tr onclick='openPostDetails(${jsArg(post.id)})'>
      <td>${escapeHtml(formatDate(post.published_at))}</td>
      <td class="post-summary">${escapeHtml(post.summary || summarize(post.text))}</td>
      <td>${escapeHtml(post.topic || "")}</td>
      <td>${escapeHtml(label(post.post_type))}</td>
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

window.openPostDetails = (id) => {
  const post = (state.dashboard?.recent_posts || []).find((item) => item.id === id);
  if (!post) return;
  const metrics = post.metrics || {};
  $("postModalTitle").textContent = post.summary || "Post details";
  $("postModalBody").innerHTML = `
    <div class="full-text">${escapeHtml(post.text)}</div>
    <div class="detail-grid">
      ${detail("Date", formatDateTime(post.published_at))}
      ${detail("Topic", post.topic)}
      ${detail("Post type", label(post.post_type))}
      ${detail("Likes", metric(metrics.likes))}
      ${detail("Replies", metric(metrics.replies))}
      ${detail("Reposts", metric(metrics.reposts))}
      ${detail("Quotes", metric(metrics.quotes))}
      ${detail("Bookmarks", metric(metrics.bookmarks))}
      ${detail("Impressions", metric(metrics.impressions))}
      ${detail("Engagement rate", formatRate(metrics.engagement_rate))}
    </div>
    ${post.url ? `<a class="button-link" href="${escapeAttr(post.url)}" target="_blank" rel="noreferrer">Open on X</a>` : ""}
  `;
  openModal("postModal");
};

function renderDashboardTrends() {
  const trends = state.dashboard?.trends || [];
  $("dashboardTrendList").innerHTML = trends.length
    ? trends.slice(0, 10).map(dashboardTrendRow).join("")
    : `<p class="muted">No trends yet. Add or import trends to use them in weekly planning.</p>`;
}

function dashboardTrendRow(item) {
  return `
    <article class="trend-row">
      <div class="meta-row">
        <span class="tag">${escapeHtml(item.urgency || "medium")}</span>
        <span>${escapeHtml(item.topic || "")}</span>
        <span>${escapeHtml(label(item.source_type || "source"))}</span>
      </div>
      <h3>${escapeHtml(item.title)}</h3>
      <p>${escapeHtml(item.summary || "")}</p>
      ${item.why_it_matters ? `<p><strong>Why it matters</strong> ${escapeHtml(item.why_it_matters)}</p>` : ""}
      ${item.leadpoet_angle ? `<p><strong>Angle</strong> ${escapeHtml(item.leadpoet_angle)}</p>` : ""}
      <div class="actions left">
        <button class="secondary" onclick='useTrendInPlan(${jsArg(item.id)})'>Use in weekly plan</button>
        ${item.source_url ? `<a href="${escapeAttr(item.source_url)}" target="_blank" rel="noreferrer">Source</a>` : ""}
      </div>
    </article>
  `;
}

window.useTrendInPlan = (id) => {
  const trend = (state.dashboard?.trends || []).find((item) => item.id === id);
  if (trend?.topic) {
    $("planTopicMix").value = `${trend.topic}:3,sales LLMs:2`;
  }
  $("planUseTrends").checked = true;
  openModal("planModal");
};

function renderSignals() {
  const hints = (state.analytics?.hints || state.dashboard?.analytics_hints || []).slice(0, 5);
  $("signalList").innerHTML = hints.length
    ? hints.map((hint) => `<p>${escapeHtml(hint)}</p>`).join("")
    : `<p class="muted">Performance signals will appear after metrics sync.</p>`;
}

function renderDashboardSyncLine() {
  const sync = state.dashboard?.sync || {};
  const postsSync = sync.x_posts_sync || {};
  const metricsSync = sync.x_metrics_sync || {};
  const latest = latestDate([postsSync.last_completed_at, metricsSync.last_completed_at]);
  $("dashboardSyncLine").textContent = `Last X sync: ${latest ? formatDateTime(latest) : "never"}`;
}

function renderReviewer() {
  $("reviewCount").textContent = String(state.reviewItems.length);
  $("scheduledCount").textContent = String(state.scheduledItems.length);
  $("reviewList").innerHTML = state.reviewItems.length
    ? state.reviewItems.map((item) => queueCard(item, "review")).join("")
    : emptyState("No posts to review. Create a weekly plan to start.");
  $("scheduledList").innerHTML = state.scheduledItems.length
    ? state.scheduledItems.map((item) => queueCard(item, "scheduled")).join("")
    : emptyState("No approved posts scheduled yet.");
}

function queueCard(item, column) {
  const scheduled = formatDateTime(item.scheduled_at);
  const canPost = item.status === "approved" && state.status?.x_write_configured;
  return `
    <article class="queue-card" draggable="true" ondragstart='dragQueue(event, ${jsArg(item.id)})'>
      <button class="card-open" onclick='openQueueDetail(${jsArg(item.id)})'>
        <div class="meta-row">
          <span class="tag">${escapeHtml(label(item.status))}</span>
          <span>${escapeHtml(item.topic || "No topic")}</span>
          <span>${escapeHtml(label(item.post_type || "post"))}</span>
        </div>
        <h3>${escapeHtml(summarize(item.text, 88))}</h3>
        <p>${escapeHtml(item.reviewer_notes || item.angle || "Suggested from the current Social Brain context.")}</p>
        <div class="schedule-line">${escapeHtml(scheduled || "Not scheduled")}</div>
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
    showMessage("Schedule required before moving to Scheduled Content.", true);
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
  $("queueModalTitle").textContent = summarize(item.text, 70);
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
      ${detail("Trend/source", item.trend_ids?.length ? `${item.trend_ids.length} trend source(s)` : "None")}
      ${detail("Why suggested", item.angle || item.reviewer_notes || "Suggested from Social Brain context.")}
      ${detail("Draft", item.draft_id || "No draft link")}
    </div>
    <div class="modal-actions">
      <button onclick="saveQueueDetail(event)">Save changes</button>
      <button class="secondary" onclick="reviseDetail(event)" ${canUseAI ? "" : "disabled"} title="${canUseAI ? "" : "Provider-backed generation is not connected."}">Improve with AI</button>
      ${item.status !== "approved" ? `<button class="secondary" onclick='approveQueue(event, ${jsArg(item.id)})'>Approve</button>` : `<button class="secondary" onclick='moveToReview(event, ${jsArg(item.id)})'>Move to review</button>`}
      ${item.status === "approved" ? `<button class="secondary" onclick='postNow(event, ${jsArg(item.id)})' ${canPost ? "" : "disabled"} title="${state.status?.x_write_configured ? "" : "X write auth is not connected."}">Post now</button>` : ""}
      <button class="quiet-button" onclick='skipQueue(event, ${jsArg(item.id)})'>Skip</button>
    </div>
    <div class="revision-box">
      <label>AI instruction<textarea id="revisionInstruction" rows="3" placeholder="Make it sharper, less corporate, or tie it more directly to Leadpoet."></textarea></label>
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
      allow_evergreen: $("planAllowEvergreen").checked,
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
  const payload = await api("/api/account/sync-x-posts", {
    method: "POST",
    body: JSON.stringify({
      username: DEFAULT_SYNC_USERNAME,
      limit: 3200,
      default_topic: "Leadpoet",
      default_post_type: "founder_insight",
      exclude_replies: true,
      exclude_retweets: true,
      imitate: false,
    }),
  });
  const result = payload.result || {};
  showMessage(`X posts synced. Imported ${result.imported || 0}. Duplicates: ${result.duplicates || 0}.`);
  await refresh();
}

async function syncMetrics() {
  const payload = await api("/api/account/sync-x-metrics", {
    method: "POST",
    body: JSON.stringify({ since_days: 90, limit: 3200 }),
  });
  const result = payload.result || {};
  showMessage(`Metrics updated: ${result.updated || 0}. Skipped: ${result.skipped || 0}.`);
  await refresh();
}

async function syncAllXData() {
  const payload = await api("/api/account/sync-x-all", {
    method: "POST",
    body: JSON.stringify({
      username: DEFAULT_SYNC_USERNAME,
      limit: 3200,
      default_topic: "Leadpoet",
      default_post_type: "founder_insight",
      exclude_replies: true,
      exclude_retweets: true,
      since_days: 90,
      imitate: false,
    }),
  });
  const posts = payload.posts || {};
  const metrics = payload.metrics || {};
  showMessage(`X data synced. Imported ${posts.imported || 0}; metrics updated ${metrics.updated || 0}.`);
  await refresh();
}

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
  $("trendList").innerHTML = payload.items.length
    ? payload.items.map(trendCard).join("")
    : `<p class="muted">No trends yet. Add or import trends before planning trend-based content.</p>`;
}

function trendCard(item) {
  return `
    <article class="trend-card">
      <div class="meta-row">
        <span class="tag">${escapeHtml(item.urgency || "medium")}</span>
        <span>${escapeHtml(item.topic || "")}</span>
        <span>${escapeHtml(label(item.source_type || ""))}</span>
      </div>
      <h3>${escapeHtml(item.title)}</h3>
      <p>${escapeHtml(item.summary || "")}</p>
      ${item.leadpoet_angle ? `<p class="muted">${escapeHtml(item.leadpoet_angle)}</p>` : ""}
      <div class="actions left">
        <button class="secondary" onclick='markTrend(event, ${jsArg(item.id)}, "used")'>Mark used</button>
        <button class="quiet-button" onclick='markTrend(event, ${jsArg(item.id)}, "skipped")'>Ignore</button>
      </div>
    </article>
  `;
}

window.markTrend = (event, id, status) => {
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

async function addTrend() {
  await api("/api/trends", {
    method: "POST",
    body: JSON.stringify({
      title: $("trendTitle").value,
      topic: $("trendTopic").value,
      source_type: $("trendSourceType").value,
      urgency: $("trendUrgency").value,
      status: "unused",
      source_url: $("trendSourceUrl").value,
      summary: $("trendSummary").value,
      why_it_matters: $("trendWhy").value,
      leadpoet_angle: $("trendAngle").value,
    }),
  });
  ["trendTitle", "trendSourceUrl", "trendSummary", "trendWhy", "trendAngle"].forEach((id) => {
    $(id).value = "";
  });
  showMessage("Trend added.");
  await loadTrends();
  await refresh();
}

async function loadAccount() {
  state.account = await api("/api/account");
  renderAccount();
}

function renderAccount() {
  const account = state.account || {};
  const status = account.status || state.status || {};
  const settings = account.settings || {};
  const sync = account.sync || {};
  const stats = account.stats || {};
  $("accountStatusGrid").innerHTML = [
    statusCard("Provider", status.provider_configured, status.provider_configured ? "Connected" : "Not connected"),
    statusCard("OpenRouter key", status.openrouter_api_key_configured, status.openrouter_api_key_configured ? "Connected" : "Not connected"),
    statusCard("Model", Boolean(status.llm_model), status.llm_model ? `${status.llm_model} (${status.llm_model_source})` : "No model selected"),
    statusCard("X read", status.x_read_configured, status.x_read_configured ? "Imports and metrics are available" : "Imports and metrics are not connected"),
    statusCard("X write", status.x_write_configured, status.x_write_configured ? "Posting is available" : "Posting is not connected"),
    statusCard("Daily sync", status.daily_sync_configured, status.daily_sync_configured ? "Enabled for protected cron calls" : "Not configured"),
  ].join("");
  $("accountSyncGrid").innerHTML = [
    syncInfoCard("Last X post sync", sync.x_posts_sync),
    syncInfoCard("Last X metrics sync", sync.x_metrics_sync),
    syncInfoCard("Last daily sync", sync.x_daily_sync),
    statusCard("Total X posts stored", true, String(stats.total_x_posts_stored || 0)),
    statusCard("Latest imported X post", Boolean(stats.latest_imported_x_post_date), stats.latest_imported_x_post_date ? formatDateTime(stats.latest_imported_x_post_date) : "No X posts stored yet"),
    statusCard("Latest sync error", !(latestSyncError(sync)), latestSyncError(sync) || "None"),
  ].join("");
  $("accountModel").value = settings.llm_model || status.llm_model || "";
  $("accountModelMeta").textContent = settings.llm_model
    ? `Saved in account settings. Effective model: ${settings.effective_llm_model || settings.llm_model}.`
    : "Choose the model Social Brain should use for drafts and AI edits.";
}

function statusCard(title, configured, detail) {
  return `
    <article class="account-card">
      <div>
        <span>${escapeHtml(title)}</span>
        <strong>${configured ? "set" : "missing"}</strong>
      </div>
      <p>${escapeHtml(detail)}</p>
    </article>
  `;
}

function syncInfoCard(title, sync) {
  const item = sync || {};
  const summary = item.summary || {};
  const pieces = [];
  if (summary.imported !== undefined) pieces.push(`imported ${summary.imported}`);
  if (summary.duplicates !== undefined) pieces.push(`duplicates ${summary.duplicates}`);
  if (summary.updated !== undefined) pieces.push(`updated ${summary.updated}`);
  if (summary.new_posts_imported !== undefined) pieces.push(`imported ${summary.new_posts_imported}`);
  if (summary.metrics_updated !== undefined) pieces.push(`metrics ${summary.metrics_updated}`);
  const detail = [
    item.last_completed_at ? formatDateTime(item.last_completed_at) : "Never",
    pieces.join(", "),
  ].filter(Boolean).join(" · ");
  return `
    <article class="account-card">
      <div>
        <span>${escapeHtml(title)}</span>
        <strong>${escapeHtml(label(item.status || "not_run"))}</strong>
      </div>
      <p>${escapeHtml(detail)}</p>
    </article>
  `;
}

function latestSyncError(sync) {
  const items = [sync?.x_daily_sync, sync?.x_posts_sync, sync?.x_metrics_sync].filter(Boolean);
  return items.find((item) => item.error_message)?.error_message || "";
}

async function saveAccount() {
  state.account = await api("/api/account", {
    method: "PATCH",
    body: JSON.stringify({ llm_model: $("accountModel").value }),
  });
  renderAccount();
  showMessage("Account settings saved.");
}

function openModal(id) {
  $(id).classList.remove("hidden");
  $(id).setAttribute("aria-hidden", "false");
}

function closeModal(id) {
  $(id).classList.add("hidden");
  $(id).setAttribute("aria-hidden", "true");
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

function detail(labelText, value) {
  return `
    <div class="detail-item">
      <span>${escapeHtml(labelText)}</span>
      <strong>${escapeHtml(value ?? "")}</strong>
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
  return String(value || "").replaceAll("_", " ");
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
  document.querySelectorAll(".nav-button[data-view]").forEach((button) => {
    button.addEventListener("click", () => setView(button.dataset.view));
  });
  document.querySelectorAll("[data-close-modal]").forEach((node) => {
    node.addEventListener("click", () => closeModal(node.dataset.closeModal));
  });
  ["postSearch", "postDateFrom", "postDateTo"].forEach((id) => {
    $(id).addEventListener("input", renderRecentPosts);
  });
  document.querySelectorAll(".posts-table th[data-sort-key]").forEach((header) => {
    header.querySelector("button")?.addEventListener("click", () => setRecentPostsSort(header.dataset.sortKey));
  });

  $("refreshButton").addEventListener("click", (event) => runButton(event.currentTarget, "Refreshing...", refresh));
  $("createPlanButton").addEventListener("click", () => openModal("planModal"));
  $("createPlanReviewerButton").addEventListener("click", () => openModal("planModal"));
  $("syncPostsButton").addEventListener("click", (event) => runButton(event.currentTarget, "Syncing...", syncPosts));
  $("syncMetricsButton").addEventListener("click", (event) => runButton(event.currentTarget, "Syncing...", syncMetrics));
  $("publishDueButton").addEventListener("click", (event) => runButton(event.currentTarget, "Posting...", publishDue));
  $("publishDueReviewerButton").addEventListener("click", (event) => runButton(event.currentTarget, "Posting...", publishDue));
  $("planWeekButton").addEventListener("click", (event) => runButton(event.currentTarget, "Creating...", planWeek));
  $("openTrendsButton").addEventListener("click", () => window.openTrendsModal().catch((error) => showMessage(error.message, true)));
  $("openTrendsDashboardButton").addEventListener("click", () => window.openTrendsModal().catch((error) => showMessage(error.message, true)));
  $("manageTrendsFromPlanButton").addEventListener("click", () => window.openTrendsModal().catch((error) => showMessage(error.message, true)));
  $("loadTrendsButton").addEventListener("click", (event) => runButton(event.currentTarget, "Loading...", loadTrends));
  $("addTrendButton").addEventListener("click", (event) => runButton(event.currentTarget, "Adding...", addTrend));
  $("saveAccountButton").addEventListener("click", (event) => runButton(event.currentTarget, "Saving...", saveAccount));
  $("accountSyncPostsButton").addEventListener("click", (event) => runButton(event.currentTarget, "Syncing...", syncPosts));
  $("accountSyncMetricsButton").addEventListener("click", (event) => runButton(event.currentTarget, "Syncing...", syncMetrics));
  $("accountSyncAllButton").addEventListener("click", (event) => runButton(event.currentTarget, "Syncing...", syncAllXData));

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
