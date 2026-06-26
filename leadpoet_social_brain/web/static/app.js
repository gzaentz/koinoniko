const state = {
  view: "dashboard",
  dashboard: null,
  analytics: null,
  reviewItems: [],
  scheduledItems: [],
  editingQueueId: "",
  revisingQueueId: "",
  itemDetails: {},
  analyticsOpen: false,
};

const views = {
  dashboard: {
    title: "Dashboard",
    meta: "What needs attention, what is scheduled, and what is working.",
  },
  reviewer: {
    title: "Content Reviewer",
    meta: "Review, improve, approve, and schedule X posts.",
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
  const payload = await response.json();
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
  window.setTimeout(() => box.classList.add("hidden"), 5000);
}

function runAction(work) {
  Promise.resolve()
    .then(work)
    .catch((error) => showMessage(error.message, true));
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
  refresh();
}

async function refresh() {
  try {
    await loadStatus();
    await loadDashboardData();
    updateDueButton();
    if (state.view === "dashboard") {
      await loadAnalytics();
      renderDashboard();
    }
    if (state.view === "reviewer") {
      await loadReviewer();
    }
  } catch (error) {
    showMessage(error.message, true);
  }
}

async function loadStatus() {
  const status = await api("/api/status");
  $("statusBlock").innerHTML = `
    <div class="status-pill"><span>Provider</span><strong>${status.provider_configured ? "set" : "missing"}</strong></div>
    <div class="status-pill"><span>X read</span><strong>${status.x_read_configured ? "set" : "missing"}</strong></div>
    <div class="status-pill"><span>X write</span><strong>${status.x_write_configured ? "set" : "missing"}</strong></div>
  `;
}

async function loadDashboardData() {
  const [dashboard, review, scheduled] = await Promise.all([
    api("/api/dashboard"),
    loadQueueStatuses(["needs_review", "draft", "failed"]),
    api("/api/queue?limit=100&status=approved"),
  ]);
  state.dashboard = dashboard;
  state.reviewItems = review;
  state.scheduledItems = scheduled.items || [];
}

async function loadQueueStatuses(statuses) {
  const payloads = await Promise.all(
    statuses.map((status) => api(`/api/queue?limit=100&status=${encodeURIComponent(status)}`))
  );
  return payloads.flatMap((payload) => payload.items || []);
}

async function loadReviewer() {
  state.reviewItems = await loadQueueStatuses(["needs_review", "draft", "failed"]);
  const scheduled = await api("/api/queue?limit=100&status=approved");
  state.scheduledItems = scheduled.items || [];
  renderReviewer();
}

async function loadAnalytics() {
  state.analytics = await api("/api/analytics");
}

function updateDueButton() {
  const due = state.dashboard?.due || [];
  $("publishDueButton").classList.toggle("hidden", due.length === 0);
  $("publishDueButton").textContent = due.length ? `Post Due (${due.length})` : "Post Due";
}

function renderDashboard() {
  const counts = state.dashboard?.queue_counts || {};
  const due = state.dashboard?.due || [];
  const reviewCount = state.reviewItems.length;
  const scheduledCount = state.scheduledItems.length;
  const postedThisWeek = countPostedThisWeek(state.dashboard?.recent_queue || []);
  const topSignal = firstSignal();

  $("dashboardCards").innerHTML = [
    metricCard("Needs Review", reviewCount, "Drafts waiting for a decision."),
    metricCard("Scheduled", scheduledCount, "Approved posts in the queue."),
    metricCard("Posted This Week", postedThisWeek, "Recent posts marked published."),
    metricCard("Best Signal", topSignal ? "Active" : "None", topSignal || "Performance signals will appear after metrics sync."),
  ].join("");

  $("attentionList").innerHTML = reviewCount
    ? `
      <p>${reviewCount} post${reviewCount === 1 ? "" : "s"} need review before they can publish.</p>
      ${state.reviewItems.slice(0, 2).map(compactQueueCard).join("")}
      <button onclick='setView("reviewer")'>Open Reviewer</button>
    `
    : `<p class="muted">No content is waiting for review.</p>`;

  const nextScheduled = nextScheduledItem();
  $("scheduleSnapshot").innerHTML = due.length
    ? `
      <p>${due.length} approved post${due.length === 1 ? " is" : "s are"} due now.</p>
      ${due.slice(0, 2).map(compactQueueCard).join("")}
    `
    : nextScheduled
      ? `
        <p class="muted">Next post due</p>
        ${compactQueueCard(nextScheduled)}
      `
      : `<p class="muted">No approved posts scheduled yet.</p>`;

  renderSignals();
  renderAnalyticsDetails();
}

function metricCard(title, value, note) {
  return `
    <article class="metric">
      <span>${escapeHtml(title)}</span>
      <strong>${escapeHtml(value)}</strong>
      <p>${escapeHtml(note)}</p>
    </article>
  `;
}

function renderSignals() {
  const hints = (state.analytics?.hints || state.dashboard?.analytics_hints || []).slice(0, 5);
  $("signalList").innerHTML = hints.length
    ? hints.map((hint) => `<p>${escapeHtml(hint)}</p>`).join("")
    : `<p class="muted">No performance data yet. Sync metrics after your first post goes live.</p>`;

  const best = (state.analytics?.best_examples || state.dashboard?.best_examples || [])[0];
  $("bestPost").innerHTML = best
    ? `
      <div class="best-post-card">
        <span class="eyebrow">Best recent post</span>
        <p>${escapeHtml(best.text)}</p>
        <div class="meta-row">
          <span>${escapeHtml(best.topic)}</span>
          <span>${escapeHtml(best.post_type)}</span>
        </div>
      </div>
    `
    : "";
}

function renderAnalyticsDetails() {
  const details = $("analyticsDetails");
  details.classList.toggle("hidden", !state.analyticsOpen);
  if (!state.analyticsOpen) return;
  if (!state.analytics) {
    details.innerHTML = `<p class="muted">Performance signals will appear after metrics sync.</p>`;
    return;
  }
  details.innerHTML = `
    <div class="analytics-summary">
      ${summaryList("Top topics", state.analytics.by_topic)}
      ${summaryList("Post types", state.analytics.by_post_type)}
      ${summaryList("Hooks", state.analytics.by_hook)}
    </div>
  `;
}

function summaryList(title, rows) {
  const items = (rows || []).slice(0, 4);
  return `
    <section>
      <h3>${escapeHtml(title)}</h3>
      ${items.length
        ? items.map((row) => `
          <p><strong>${escapeHtml(row.key)}</strong> ${metricPhrase(row)}</p>
        `).join("")
        : `<p class="muted">No data yet.</p>`}
    </section>
  `;
}

function metricPhrase(row) {
  const er = row.avg_engagement_rate === null || row.avg_engagement_rate === undefined
    ? "ER unavailable"
    : `${(row.avg_engagement_rate * 100).toFixed(2)}% ER`;
  const impressions = Math.round(row.avg_impressions || 0);
  return `${row.count} post${row.count === 1 ? "" : "s"}, ${er}, ${formatNumber(impressions)} avg impressions.`;
}

function renderReviewer() {
  $("reviewCount").textContent = state.reviewItems.length;
  $("scheduledCount").textContent = state.scheduledItems.length;
  $("reviewList").innerHTML = state.reviewItems.length
    ? state.reviewItems.map((item) => queueCard(item, "review")).join("")
    : emptyState("No posts to review. Create a weekly plan to start.");
  $("scheduledList").innerHTML = state.scheduledItems.length
    ? state.scheduledItems.map((item) => queueCard(item, "scheduled")).join("")
    : emptyState("No approved posts scheduled yet.");
}

function queueCard(item, column) {
  const isEditing = state.editingQueueId === item.id;
  const isRevising = state.revisingQueueId === item.id;
  return `
    <article class="queue-card ${isEditing || isRevising ? "selected" : ""}">
      <div class="meta-row">
        <span class="tag">${escapeHtml(label(item.status))}</span>
        <span>${escapeHtml(item.topic || "No topic")}</span>
        <span>${escapeHtml(item.post_type || "post")}</span>
      </div>
      <div class="queue-text">${escapeHtml(item.text)}</div>
      <div class="card-context">
        <p><strong>Scheduled</strong> ${escapeHtml(formatSchedule(item.scheduled_at))}</p>
        ${item.trend_ids && item.trend_ids.length
          ? `<p><strong>Trend</strong> ${item.trend_ids.length} source${item.trend_ids.length === 1 ? "" : "s"} used. <button class="text-button" onclick='openTrendsModal()'>View trends</button></p>`
          : ""}
        <p><strong>Why suggested</strong> ${escapeHtml(item.reviewer_notes || item.angle || "Suggested from the current Social Brain context.")}</p>
      </div>
      ${item.failure_reason ? `<p class="error-text">${escapeHtml(item.failure_reason)}</p>` : ""}
      <div class="card-actions">
        <button onclick='toggleEdit(${jsArg(item.id)})'>Edit</button>
        <button class="secondary" onclick='toggleRevise(${jsArg(item.id)})'>Improve with AI</button>
        ${item.status !== "approved" ? `<button class="secondary" onclick='approveQueue(${jsArg(item.id)})'>Approve</button>` : ""}
        <button class="quiet-button" onclick='toggleEdit(${jsArg(item.id)})'>Schedule</button>
        ${item.status === "approved" ? `<button class="secondary" onclick='postNow(${jsArg(item.id)})'>Post now</button>` : ""}
        ${column === "review" ? `<button class="quiet-button" onclick='skipQueue(${jsArg(item.id)})'>Skip</button>` : ""}
      </div>
      ${isEditing ? editPanel(item) : ""}
      ${isRevising ? revisionPanel(item) : ""}
    </article>
  `;
}

function compactQueueCard(item) {
  return `
    <article class="mini-card">
      <p>${escapeHtml(item.text)}</p>
      <div class="meta-row">
        <span>${escapeHtml(formatSchedule(item.scheduled_at))}</span>
        <span>${escapeHtml(item.topic || "")}</span>
      </div>
    </article>
  `;
}

function editPanel(item) {
  return `
    <div class="inline-panel">
      <label>Post text<textarea id="editText-${escapeAttr(item.id)}" rows="7">${escapeHtml(item.text)}</textarea></label>
      <div class="form-grid">
        <label>Topic<input id="editTopic-${escapeAttr(item.id)}" value="${escapeAttr(item.topic)}"></label>
        <label>Post type<input id="editPostType-${escapeAttr(item.id)}" value="${escapeAttr(item.post_type)}"></label>
        <label>Scheduled time<input id="editScheduled-${escapeAttr(item.id)}" value="${escapeAttr(item.scheduled_at)}"></label>
      </div>
      <label>Reviewer notes<textarea id="editNotes-${escapeAttr(item.id)}" rows="3">${escapeHtml(item.reviewer_notes || "")}</textarea></label>
      <div class="actions left">
        <button onclick='saveCardEdit(${jsArg(item.id)})'>Save changes</button>
        <button class="quiet-button" onclick='closeInlinePanels()'>Cancel</button>
      </div>
    </div>
  `;
}

function revisionPanel(item) {
  const details = state.itemDetails[item.id] || item;
  const versions = details.versions || [];
  return `
    <div class="inline-panel">
      <label>Revision instruction<textarea id="revisionInstruction-${escapeAttr(item.id)}" rows="3" placeholder="Make it sharper, more specific, and less polished."></textarea></label>
      <div class="actions left">
        <button onclick='reviseItem(${jsArg(item.id)})'>Generate revisions</button>
        <button class="quiet-button" onclick='closeInlinePanels()'>Cancel</button>
      </div>
      <div class="revision-list">
        ${versions.length ? versions.map(revisionCard).join("") : `<p class="muted">Revisions will appear here. Accepting one will update the draft but will not auto-approve it.</p>`}
      </div>
    </div>
  `;
}

function revisionCard(version) {
  return `
    <article class="revision">
      <div class="revision-text">${escapeHtml(version.text)}</div>
      ${version.edit_instruction ? `<p class="muted">${escapeHtml(version.edit_instruction)}</p>` : ""}
      <button class="secondary" onclick='acceptRevision(${jsArg(version.queue_id)}, ${jsArg(version.id)})'>Accept revision</button>
    </article>
  `;
}

window.toggleEdit = (id) => {
  state.editingQueueId = state.editingQueueId === id ? "" : id;
  state.revisingQueueId = "";
  renderReviewer();
};

window.toggleRevise = (id) => {
  runAction(async () => {
    state.revisingQueueId = state.revisingQueueId === id ? "" : id;
    state.editingQueueId = "";
    if (state.revisingQueueId && !state.itemDetails[id]) {
      const payload = await api(`/api/queue/${encodeURIComponent(id)}`);
      state.itemDetails[id] = payload.item;
    }
    renderReviewer();
  });
};

window.closeInlinePanels = () => {
  state.editingQueueId = "";
  state.revisingQueueId = "";
  renderReviewer();
};

window.saveCardEdit = (id) => {
  runAction(async () => {
    const payload = await api(`/api/queue/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({
        text: $(`editText-${id}`).value,
        topic: $(`editTopic-${id}`).value,
        post_type: $(`editPostType-${id}`).value,
        scheduled_at: $(`editScheduled-${id}`).value,
        reviewer_notes: $(`editNotes-${id}`).value,
      }),
    });
    state.itemDetails[id] = payload.item;
    showMessage("Post updated.");
    await refresh();
  });
};

window.approveQueue = (id) => {
  runAction(async () => {
    await api(`/api/queue/${encodeURIComponent(id)}/approve`, { method: "POST", body: "{}" });
    showMessage("Post approved and moved to Scheduled Content.");
    await refresh();
  });
};

window.skipQueue = (id) => {
  runAction(async () => {
    await api(`/api/queue/${encodeURIComponent(id)}/skip`, { method: "POST", body: "{}" });
    showMessage("Post skipped.");
    await refresh();
  });
};

window.postNow = (id) => {
  runAction(async () => {
    const payload = await api(`/api/queue/${encodeURIComponent(id)}/post-now`, { method: "POST", body: "{}" });
    const result = payload.result;
    showMessage(result.error || "Posted to X.", Boolean(result.error));
    await refresh();
  });
};

window.reviseItem = (id) => {
  runAction(async () => {
    const instruction = $(`revisionInstruction-${id}`).value.trim();
    if (!instruction) return showMessage("Add a revision instruction first.", true);
    const payload = await api(`/api/queue/${encodeURIComponent(id)}/revise`, {
      method: "POST",
      body: JSON.stringify({ instruction, variants: 3 }),
    });
    state.itemDetails[id] = {
      ...(state.itemDetails[id] || {}),
      id,
      versions: payload.versions,
    };
    showMessage("Revision options generated.");
    renderReviewer();
  });
};

window.acceptRevision = (queueId, versionId) => {
  runAction(async () => {
    const payload = await api(`/api/queue/${encodeURIComponent(queueId)}/versions/${encodeURIComponent(versionId)}`, {
      method: "POST",
      body: "{}",
    });
    state.itemDetails[queueId] = payload.item;
    state.revisingQueueId = "";
    state.editingQueueId = queueId;
    showMessage("Revision accepted. Review before approving.");
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
  setView("reviewer");
}

async function loadTrends() {
  const payload = await api("/api/trends?limit=100&status=unused");
  $("trendList").innerHTML = payload.items.length
    ? payload.items.map(trendCard).join("")
    : `<p class="muted">No unused trends.</p>`;
}

function trendCard(item) {
  return `
    <article class="trend-card">
      <div class="meta-row">
        <span class="tag">${escapeHtml(item.urgency || "medium")}</span>
        <span>${escapeHtml(item.topic || "")}</span>
      </div>
      <h3>${escapeHtml(item.title)}</h3>
      <p>${escapeHtml(item.summary || "")}</p>
      ${item.leadpoet_angle ? `<p class="muted">${escapeHtml(item.leadpoet_angle)}</p>` : ""}
      <div class="actions left">
        <button class="secondary" onclick='markTrend(${jsArg(item.id)}, "used")'>Mark used</button>
        <button class="quiet-button" onclick='markTrend(${jsArg(item.id)}, "skipped")'>Ignore</button>
      </div>
    </article>
  `;
}

window.markTrend = (id, status) => {
  runAction(async () => {
    await api(`/api/trends/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    });
    showMessage(status === "used" ? "Trend marked used." : "Trend ignored.");
    await loadTrends();
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
}

async function syncMetrics() {
  const payload = await api("/api/sync-metrics", {
    method: "POST",
    body: JSON.stringify({ since_days: 90, limit: 100 }),
  });
  const result = payload.result;
  showMessage(`Metrics updated: ${result.updated}. Skipped: ${result.skipped}.`);
  await refresh();
}

async function publishDue() {
  const payload = await api("/api/publish-due", {
    method: "POST",
    body: JSON.stringify({ limit: 20 }),
  });
  showMessage(`Processed ${payload.results.length} due posts.`);
  await refresh();
}

function openModal(id) {
  $(id).classList.remove("hidden");
  $(id).setAttribute("aria-hidden", "false");
}

function closeModal(id) {
  $(id).classList.add("hidden");
  $(id).setAttribute("aria-hidden", "true");
}

window.openTrendsModal = async () => {
  openModal("trendsModal");
  await loadTrends();
};

function firstSignal() {
  const hints = state.analytics?.hints || state.dashboard?.analytics_hints || [];
  return hints[0] || "";
}

function nextScheduledItem() {
  return [...state.scheduledItems].sort((a, b) => String(a.scheduled_at).localeCompare(String(b.scheduled_at)))[0];
}

function countPostedThisWeek(items) {
  const weekAgo = Date.now() - 7 * 24 * 60 * 60 * 1000;
  return items.filter((item) => {
    if (item.status !== "posted" || !item.posted_at) return false;
    const ts = Date.parse(item.posted_at);
    return Number.isFinite(ts) && ts >= weekAgo;
  }).length;
}

function emptyState(text) {
  return `<div class="empty-state">${escapeHtml(text)}</div>`;
}

function label(value) {
  return String(value || "").replaceAll("_", " ");
}

function formatSchedule(value) {
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

function formatNumber(value) {
  return new Intl.NumberFormat().format(value || 0);
}

function setup() {
  document.querySelectorAll(".nav-button[data-view]").forEach((button) => {
    button.addEventListener("click", () => setView(button.dataset.view));
  });
  document.querySelectorAll("[data-close-modal]").forEach((node) => {
    node.addEventListener("click", () => closeModal(node.dataset.closeModal));
  });
  $("createPlanButton").addEventListener("click", () => openModal("planModal"));
  $("createPlanReviewerButton").addEventListener("click", () => openModal("planModal"));
  $("syncMetricsButton").addEventListener("click", () => syncMetrics().catch((error) => showMessage(error.message, true)));
  $("publishDueButton").addEventListener("click", () => publishDue().catch((error) => showMessage(error.message, true)));
  $("planWeekButton").addEventListener("click", () => planWeek().catch((error) => showMessage(error.message, true)));
  $("openTrendsButton").addEventListener("click", () => window.openTrendsModal().catch((error) => showMessage(error.message, true)));
  $("manageTrendsFromPlanButton").addEventListener("click", () => window.openTrendsModal().catch((error) => showMessage(error.message, true)));
  $("loadTrendsButton").addEventListener("click", () => loadTrends().catch((error) => showMessage(error.message, true)));
  $("addTrendButton").addEventListener("click", () => addTrend().catch((error) => showMessage(error.message, true)));
  $("toggleAnalyticsButton").addEventListener("click", () => {
    state.analyticsOpen = !state.analyticsOpen;
    $("toggleAnalyticsButton").textContent = state.analyticsOpen ? "Hide details" : "View details";
    renderAnalyticsDetails();
  });
  const monday = new Date();
  monday.setDate(monday.getDate() + ((8 - monday.getDay()) % 7 || 7));
  $("planWeekStart").value = monday.toISOString().slice(0, 10);
  refresh();
}

setup();
