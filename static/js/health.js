// Health: connect a provider, see today's numbers against your own
// baseline, log anything by hand.
//
// The section works with zero provider connected - manual entry and pushed
// data (Tasker, Home Assistant, a Shortcut, curl) land in the same place.
// Connecting Google is an upgrade, not a prerequisite.

const HEALTH_ORDER = ["steps", "sleep_minutes", "active_minutes",
                      "exercise_minutes", "distance_km", "calories",
                      "workout_hr", "weight_kg"];

// Sleep reads better as 7h 10m than 430 min.
function fmtMetric(key, value, unit) {
  if (value === null || value === undefined) return "—";
  if (key === "sleep_minutes") {
    const h = Math.floor(value / 60), m = Math.round(value % 60);
    return `${h}h ${String(m).padStart(2, "0")}m`;
  }
  if (key === "steps" || key === "calories") return Math.round(value).toLocaleString();
  return `${Math.round(value * 10) / 10}${unit ? " " + unit : ""}`;
}

async function renderHealth() {
  const panel = el("panel");
  panel.innerHTML = `<div class="loading">Loading…</div>`;

  const data = await API.get("/health?days=30");
  const { summary, series, metrics, provider } = data;

  const connectBtn = provider.connected
    ? `<button class="btn" id="h-sync">Sync now</button>
       <button class="btn" id="h-disconnect">Disconnect</button>`
    : provider.configured
      ? `<button class="btn primary" id="h-connect">Connect Google Health</button>`
      : `<span class="chat-status warn" title="Set OPSDECK_GOOGLE_CLIENT_ID and OPSDECK_GOOGLE_CLIENT_SECRET in .env, then restart">provider not configured</span>`;

  const tiles = HEALTH_ORDER.filter((k) => summary[k]).map((k) => {
    const s = summary[k];
    // Compare against your own trailing average rather than a generic goal.
    let delta = "";
    if (s.today !== null && s.avg) {
      const pct = Math.round(((s.today - s.avg) / s.avg) * 100);
      if (Math.abs(pct) >= 5) {
        delta = `<span class="h-delta ${pct > 0 ? "up" : "down"}">${pct > 0 ? "▲" : "▼"} ${Math.abs(pct)}%</span>`;
      } else {
        delta = `<span class="h-delta flat">≈ usual</span>`;
      }
    }
    return `
      <div class="joint-card h-tile">
        <div class="h-label">${esc(s.label)}</div>
        <div class="h-value">${fmtMetric(k, s.today, s.unit)}</div>
        <div class="h-sub">${s.avg !== null ? `avg ${fmtMetric(k, s.avg, s.unit)}` : ""} ${delta}</div>
      </div>`;
  }).join("");

  panel.innerHTML = `
    <div class="section-head-row">
      <h1 class="section-title">Health</h1>
      <div class="head-actions">${connectBtn}</div>
    </div>
    <p class="section-sub">
      Today against your own 7-day baseline. Anything can write here —
      the connector, or a <code>POST /api/health</code> from your phone.
    </p>

    ${tiles ? `<div class="h-tiles">${tiles}</div>`
            : `<p class="empty-state">Nothing logged yet. Connect a provider or add a reading below.</p>`}

    <div class="joint-card h-add">
      <div class="block-title">Log a reading</div>
      <div class="field-row-inline">
        <select id="h-metric">
          ${HEALTH_ORDER.map((k) => `<option value="${k}">${esc(metrics[k].label)} (${esc(metrics[k].unit)})</option>`).join("")}
        </select>
        <input type="number" id="h-value" step="any" placeholder="value">
        <input type="date" id="h-date" value="${todayISO()}">
        <button class="btn primary" id="h-log">Log</button>
      </div>
    </div>

    <div class="joint-card">
      <div class="block-title">Last 30 days</div>
      <div id="h-chart">${healthChart(series)}</div>
    </div>`;

  el("h-log").addEventListener("click", async () => {
    const v = el("h-value").value;
    if (v === "") { toast("Enter a value", "error"); return; }
    await API.post("/health", {
      metric: el("h-metric").value, value: Number(v), date: el("h-date").value,
    });
    toast("Logged");
    renderHealth();
  });

  el("h-connect")?.addEventListener("click", async () => {
    const r = await API.get("/health/connect");
    // Google's consent screen must be a top-level navigation, not fetch.
    window.open(r.url, "_blank", "noopener");
    toast("Finish in the new tab, then hit Sync", "info", 6000);
  });

  el("h-sync")?.addEventListener("click", async () => {
    const btn = el("h-sync");
    btn.disabled = true; btn.textContent = "Syncing…";
    try {
      const r = await API.post("/health/sync", { days: 14 });
      toast(r.ok ? `Synced ${r.written} readings` : (r.error || "Sync failed"),
            r.ok ? "info" : "error", 6000);
      if (r.errors?.length) console.warn("health sync partial:", r.errors);
    } catch (e) { /* toasted */ }
    renderHealth();
  });

  el("h-disconnect")?.addEventListener("click", async () => {
    if (!confirm("Disconnect Google Health? Existing readings are kept.")) return;
    await API.post("/health/disconnect", {});
    renderHealth();
  });
}

// A tiny inline bar chart per metric - no library, same approach as the
// rest of the app.
function healthChart(series) {
  const byMetric = {};
  series.forEach((r) => (byMetric[r.metric] ||= []).push(r));
  const blocks = HEALTH_ORDER.filter((k) => byMetric[k]).map((k) => {
    const rows = byMetric[k];
    const max = Math.max(...rows.map((r) => r.value)) || 1;
    return `
      <div class="h-chart-row">
        <div class="h-chart-label">${esc(rows[0].metric.replace(/_/g, " "))}</div>
        <div class="h-bars">
          ${rows.map((r) => `
            <span class="h-bar" style="height:${Math.max(3, (r.value / max) * 100)}%"
                  title="${esc(r.local_date)}: ${fmtMetric(k, r.value, r.unit)}"></span>`).join("")}
        </div>
      </div>`;
  }).join("");
  return blocks || `<p class="empty-state small">No history yet.</p>`;
}
