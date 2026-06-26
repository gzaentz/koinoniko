const state = {
  view: "dashboard",
  selectedQueueId: "",
  queueItems: [],
};

const views = {
  dashboard: { title: "Dashboard", meta: "Local review and posting queue" },
  queue: { title: "Content Queue", meta: "Review, approve, skip, and post approved X drafts" },
  edit: { title: "AI Edit Panel", meta: "Revise queued posts without overwriting automatically" },
  planner: { title: "Weekly Planner", meta: "Create a reviewable week of X posts" },
  analytics: { title: "Analytics", meta: "Latest performance by topic, type, and writing pattern" },
  trends: { title: "Trends", meta: "Manual trend context for timely posts" },
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

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function setView(view) {
  state.view = view;
  document.querySelectorAll(".view").forEach((node) => node.classList.remove("active"));
  document.querySelectorAll(".nav-button").forEach((node) => node.classList.toggle("active", node.dataset.view === view));
  $(`${view}View`).classList.add("active");
  $("viewTitle").textContent = views[view].title;
  $("viewMeta").textContent = views[view].meta;
  refresh();
}

async function refresh() {
  try {
    await loadStatus();
    if (state.view === "dashboard") await loadDashboard();
    if (state.view === "queue") await loadQueue();
    if (state.view === "edit" && state.selectedQueueId) await loadEditItem(state.selectedQueueId);
    if (state.view === "analytics") await loadAnalytics();
    if (state.view === "trends") await loadTrends();
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

async function loadDashboard() {
  const payload = await api("/api/dashboard");
  const counts = payload.queue_counts || {};
  $("summaryGrid").innerHTML = ["needs_review", "approved", "posted", "failed"].map((key) => `
    <div class="metric">
      <span>${label(key)}</span>
      <strong>${counts[key] || 0}</strong>
    </div>
  `).join("");
  $("dueList").innerHTML = payload.due.length
    ? payload.due.map(queueCard).join("")
    : `<p class="muted">No approved posts are due.</p>`;
  $("hintList").innerHTML = payload.analytics_hints.length
    ? payload.analytics_hints.map((hint) => `<p>${escapeHtml(hint)}</p>`).join("")
    : `<p class="muted">No analytics hints yet.</p>`;
}

async function loadQueue() {
  const status = $("queueStatus").value;
  const payload = await api(`/api/queue?limit=100${status ? `&status=${encodeURIComponent(status)}` : ""}`);
  state.queueItems = payload.items;
  $("queueList").innerHTML = payload.items.length
    ? payload.items.map(queueCard).join("")
    : `<p class="muted">No queued posts for this filter.</p>`;
}

function queueCard(item) {
  return `
    <article class="queue-card ${item.id === state.selectedQueueId ? "selected" : ""}">
      <div class="meta-row">
        <span class="tag">${escapeHtml(label(item.status))}</span>
        <span>${escapeHtml(item.topic)}</span>
        <span>${escapeHtml(item.post_type)}</span>
        <span>${escapeHtml(item.scheduled_at)}</span>
      </div>
      <div class="queue-text">${escapeHtml(item.text)}</div>
      ${item.reviewer_notes ? `<p class="muted">${escapeHtml(item.reviewer_notes)}</p>` : ""}
      ${item.failure_reason ? `<p>${escapeHtml(item.failure_reason)}</p>` : ""}
      <div class="actions left">
        <button onclick="selectQueue('${item.id}')">Edit</button>
        <button class="secondary" onclick="approveQueue('${item.id}')">Approve</button>
        <button class="secondary" onclick="skipQueue('${item.id}')">Skip</button>
        <button class="secondary" onclick="postNow('${item.id}')">Post Now</button>
      </div>
    </article>
  `;
}

window.selectQueue = async (id) => {
  state.selectedQueueId = id;
  $("editQueueId").value = id;
  setView("edit");
};

async function loadEditItem(id) {
  const payload = await api(`/api/queue/${encodeURIComponent(id)}`);
  const item = payload.item;
  state.selectedQueueId = item.id;
  $("editQueueId").value = item.id;
  $("editText").value = item.text || "";
  $("editTopic").value = item.topic || "";
  $("editPostType").value = item.post_type || "";
  $("editScheduled").value = item.scheduled_at || "";
  $("editStatus").value = item.status || "";
  $("editNotes").value = item.reviewer_notes || "";
  $("revisionList").innerHTML = (item.versions || []).map(revisionCard).join("");
}

async function saveEdit() {
  const id = $("editQueueId").value.trim();
  if (!id) return showMessage("Queue ID is required.", true);
  const payload = await api(`/api/queue/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify({
      text: $("editText").value,
      topic: $("editTopic").value,
      post_type: $("editPostType").value,
      scheduled_at: $("editScheduled").value,
      status: $("editStatus").value,
      reviewer_notes: $("editNotes").value,
    }),
  });
  showMessage("Queued post saved.");
  await loadEditItem(payload.item.id);
}

window.approveQueue = async (id) => {
  await api(`/api/queue/${encodeURIComponent(id)}/approve`, { method: "POST", body: "{}" });
  showMessage("Queued post approved.");
  await refresh();
};

window.skipQueue = async (id) => {
  await api(`/api/queue/${encodeURIComponent(id)}/skip`, { method: "POST", body: "{}" });
  showMessage("Queued post skipped.");
  await refresh();
};

window.postNow = async (id) => {
  const payload = await api(`/api/queue/${encodeURIComponent(id)}/post-now`, { method: "POST", body: "{}" });
  const result = payload.result;
  showMessage(result.error || "Posted to X.");
  await refresh();
};

async function reviseSelected() {
  const id = $("editQueueId").value.trim();
  if (!id) return showMessage("Queue ID is required.", true);
  const instruction = $("revisionInstruction").value.trim();
  if (!instruction) return showMessage("Revision instruction is required.", true);
  const payload = await api(`/api/queue/${encodeURIComponent(id)}/revise`, {
    method: "POST",
    body: JSON.stringify({ instruction, variants: 3 }),
  });
  $("revisionList").innerHTML = payload.versions.map(revisionCard).join("");
  showMessage("Revision options generated.");
}

function revisionCard(version) {
  return `
    <article class="revision">
      <div class="meta-row">
        <span>${escapeHtml(version.edit_type)}</span>
        <span>${escapeHtml(version.created_at)}</span>
      </div>
      <div class="revision-text">${escapeHtml(version.text)}</div>
      ${version.edit_instruction ? `<p class="muted">${escapeHtml(version.edit_instruction)}</p>` : ""}
      <button onclick="acceptRevision('${version.queue_id}', '${version.id}')">Accept Revision</button>
    </article>
  `;
}

window.acceptRevision = async (queueId, versionId) => {
  const payload = await api(`/api/queue/${encodeURIComponent(queueId)}/versions/${encodeURIComponent(versionId)}`, {
    method: "POST",
    body: "{}",
  });
  showMessage("Revision accepted.");
  await loadEditItem(payload.item.id);
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
      trend_window_days: Number($("planTrendWindow").value || 14),
      use_trends: $("planUseTrends").checked,
      allow_evergreen: $("planAllowEvergreen").checked,
      variants_per_slot: 1,
    }),
  });
  $("planResult").innerHTML = [
    ...payload.warnings.map((warning) => `<p>${escapeHtml(warning)}</p>`),
    ...payload.queued.map(queueCard),
  ].join("");
  showMessage(`Created ${payload.queued.length} queued posts.`);
}

async function loadAnalytics() {
  const payload = await api("/api/analytics");
  $("analyticsHints").innerHTML = payload.hints.length
    ? payload.hints.map((hint) => `<p>${escapeHtml(hint)}</p>`).join("")
    : `<p class="muted">No analytics hints yet.</p>`;
  const groups = [
    ["Post Types", payload.by_post_type],
    ["Topics", payload.by_topic],
    ["Hooks", payload.by_hook],
    ["Structures", payload.by_structure],
    ["Tones", payload.by_tone],
    ["CTAs", payload.by_cta],
    ["Formatting", payload.by_formatting],
    ["Patterns", payload.by_pattern],
  ];
  $("analyticsGrid").innerHTML = groups.map(([title, rows]) => analyticsTable(title, rows)).join("");
}

function analyticsTable(title, rows) {
  return `
    <section class="panel">
      <div class="panel-header"><h2>${escapeHtml(title)}</h2></div>
      <div class="table-like">
        <div class="table-row header">
          <span>Name</span><span>Posts</span><span>ER</span><span>Impr.</span><span>Replies</span>
        </div>
        ${(rows || []).slice(0, 8).map((row) => `
          <div class="table-row">
            <span>${escapeHtml(row.key)}</span>
            <span>${row.count}</span>
            <span>${row.avg_engagement_rate === null ? "" : `${(row.avg_engagement_rate * 100).toFixed(2)}%`}</span>
            <span>${Math.round(row.avg_impressions || 0)}</span>
            <span>${Math.round(row.avg_replies || 0)}</span>
          </div>
        `).join("") || `<div class="table-row"><span class="muted">No data</span></div>`}
      </div>
    </section>
  `;
}

async function loadTrends() {
  const status = $("trendFilterStatus").value;
  const payload = await api(`/api/trends?limit=100${status ? `&status=${encodeURIComponent(status)}` : ""}`);
  $("trendList").innerHTML = payload.items.length
    ? payload.items.map(trendCard).join("")
    : `<p class="muted">No trends for this filter.</p>`;
}

function trendCard(item) {
  return `
    <article class="trend-card">
      <div class="meta-row">
        <span class="tag">${escapeHtml(item.status)}</span>
        <span>${escapeHtml(item.topic)}</span>
        <span>${escapeHtml(item.urgency)}</span>
      </div>
      <h3>${escapeHtml(item.title)}</h3>
      <p>${escapeHtml(item.summary)}</p>
      <p class="muted">${escapeHtml(item.leadpoet_angle)}</p>
    </article>
  `;
}

async function addTrend() {
  await api("/api/trends", {
    method: "POST",
    body: JSON.stringify({
      title: $("trendTitle").value,
      topic: $("trendTopic").value,
      source_type: $("trendSourceType").value,
      urgency: $("trendUrgency").value,
      status: $("trendStatus").value,
      source_url: $("trendSourceUrl").value,
      summary: $("trendSummary").value,
      why_it_matters: $("trendWhy").value,
      leadpoet_angle: $("trendAngle").value,
    }),
  });
  ["trendTitle", "trendSourceUrl", "trendSummary", "trendWhy", "trendAngle"].forEach((id) => $(id).value = "");
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

function label(value) {
  return String(value || "").replaceAll("_", " ");
}

function setup() {
  document.querySelectorAll(".nav-button").forEach((button) => {
    button.addEventListener("click", () => setView(button.dataset.view));
  });
  $("refreshButton").addEventListener("click", refresh);
  $("syncMetricsButton").addEventListener("click", () => syncMetrics().catch((error) => showMessage(error.message, true)));
  $("publishDueButton").addEventListener("click", () => publishDue().catch((error) => showMessage(error.message, true)));
  $("loadQueueButton").addEventListener("click", () => loadQueue().catch((error) => showMessage(error.message, true)));
  $("loadEditButton").addEventListener("click", () => loadEditItem($("editQueueId").value.trim()).catch((error) => showMessage(error.message, true)));
  $("saveEditButton").addEventListener("click", () => saveEdit().catch((error) => showMessage(error.message, true)));
  $("approveEditButton").addEventListener("click", () => approveQueue($("editQueueId").value.trim()).catch((error) => showMessage(error.message, true)));
  $("skipEditButton").addEventListener("click", () => skipQueue($("editQueueId").value.trim()).catch((error) => showMessage(error.message, true)));
  $("postNowEditButton").addEventListener("click", () => postNow($("editQueueId").value.trim()).catch((error) => showMessage(error.message, true)));
  $("reviseButton").addEventListener("click", () => reviseSelected().catch((error) => showMessage(error.message, true)));
  $("planWeekButton").addEventListener("click", () => planWeek().catch((error) => showMessage(error.message, true)));
  $("loadTrendsButton").addEventListener("click", () => loadTrends().catch((error) => showMessage(error.message, true)));
  $("addTrendButton").addEventListener("click", () => addTrend().catch((error) => showMessage(error.message, true)));
  const monday = new Date();
  monday.setDate(monday.getDate() + ((8 - monday.getDay()) % 7 || 7));
  $("planWeekStart").value = monday.toISOString().slice(0, 10);
  refresh();
}

setup();

