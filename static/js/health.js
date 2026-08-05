// Health: an overview you can drill into, plus a raw explorer.
//
// Three views behind one section. Overview is tiles; clicking one opens a
// detail view for that metric (labelled chart, full stats, day-of-week
// shape, which device supplied it); the raw table is every stored row with
// filters. Range applies across all three.
//
// Works with no provider connected - manual entry and pushed data land in
// the same place, so connecting Google is an upgrade, not a prerequisite.

const HEALTH_ORDER = ["steps", "sleep_minutes", "active_minutes",
                      "exercise_minutes", "distance_km", "calories",
                      "workout_hr", "weight_kg"];

const HEALTH_RANGES = [
  { days: 7, label: "7d" }, { days: 30, label: "30d" },
  { days: 90, label: "90d" }, { days: 365, label: "1y" },
  { days: 3650, label: "All" },
];

let hRange = 30;
let hView = "overview";     // overview | detail | raw
let hMetric = null;

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

  const data = await API.get(`/health?days=${hRange}`);
  const { summary, provider } = data;

  const providerCtl = provider.connected
    ? `<button class="btn" id="h-sync">Sync now</button>
       <button class="btn" id="h-disconnect">Disconnect</button>`
    : provider.configured
      ? `<button class="btn primary" id="h-connect">Connect Google Health</button>`
      : `<span class="chat-status warn">provider not configured</span>`;

  panel.innerHTML = `
    <div class="section-head-row">
      <h1 class="section-title">Health</h1>
      <div class="head-actions">${providerCtl}</div>
    </div>

    <div class="h-controls">
      <div class="h-views">
        ${[["overview", "Overview"], ["raw", "All data"]].map(([v, l]) =>
          `<button class="board-tab ${hView !== "detail" && hView === v ? "active" : ""}"
                   data-hview="${v}">${l}</button>`).join("")}
        ${hView === "detail"
          ? `<button class="board-tab active" data-hview="detail">${esc(metricLabel(hMetric))}</button>`
          : ""}
      </div>
      <div class="h-ranges">
        ${HEALTH_RANGES.map((r) =>
          `<button class="chip ${r.days === hRange ? "on c-amber" : ""}"
                   data-hrange="${r.days}">${r.label}</button>`).join("")}
      </div>
    </div>

    <div id="h-body"><div class="loading">Loading…</div></div>`;

  panel.querySelectorAll("[data-hrange]").forEach((b) =>
    b.addEventListener("click", () => { hRange = Number(b.dataset.hrange); renderHealth(); }));
  panel.querySelectorAll("[data-hview]").forEach((b) =>
    b.addEventListener("click", () => {
      if (b.dataset.hview !== "detail") { hView = b.dataset.hview; hMetric = null; }
      renderHealth();
    }));

  el("h-connect")?.addEventListener("click", async () => {
    const r = await API.get("/health/connect");
    window.open(r.url, "_blank", "noopener");
    toast("Finish in the new tab, then hit Sync", "info", 6000);
  });
  el("h-sync")?.addEventListener("click", async () => {
    const btn = el("h-sync");
    btn.disabled = true; btn.textContent = "Syncing…";
    try {
      const r = await API.post("/health/sync", { days: 30 });
      toast(r.ok ? `Synced ${r.written} readings from ${r.scanned} points`
                 : (r.error || "Sync failed"), r.ok ? "info" : "error", 6000);
      if (r.errors?.length) console.warn("health sync partial:", r.errors);
    } catch (e) { /* toasted */ }
    renderHealth();
  });
  el("h-disconnect")?.addEventListener("click", async () => {
    if (!confirm("Disconnect Google Health? Existing readings are kept.")) return;
    await API.post("/health/disconnect", {});
    renderHealth();
  });

  const body = el("h-body");
  if (hView === "detail" && hMetric) await healthDetail(body);
  else if (hView === "raw") await healthRaw(body);
  else await healthOverview(body, summary);
}

function metricLabel(m) {
  return (window.OPSDECK.healthMetrics?.[m]?.label) || (m || "").replace(/_/g, " ");
}

// ---------- overview ----------
async function healthOverview(body, summary) {
  const allStats = await API.get(`/health/stats?days=${hRange}`);
  window.OPSDECK.healthMetrics = (await API.get("/health?days=1")).metrics;

  const tiles = HEALTH_ORDER.filter((k) => summary[k] || allStats[k]?.count).map((k) => {
    const s = summary[k] || {};
    const st = allStats[k] || {};
    let delta = "";
    if (s.today != null && s.avg) {
      const pct = Math.round(((s.today - s.avg) / s.avg) * 100);
      delta = Math.abs(pct) >= 5
        ? `<span class="h-delta ${pct > 0 ? "up" : "down"}">${pct > 0 ? "▲" : "▼"} ${Math.abs(pct)}%</span>`
        : `<span class="h-delta flat">≈ usual</span>`;
    }
    return `
      <button class="joint-card h-tile clickable" data-metric="${k}">
        <div class="h-label">${esc(st.label || metricLabel(k))}</div>
        <div class="h-value">${fmtMetric(k, s.today ?? null, st.unit)}</div>
        <div class="h-sub">${s.avg != null ? `avg ${fmtMetric(k, s.avg, st.unit)}` : ""} ${delta}</div>
        <div class="h-tile-foot">
          ${st.count ? `${st.count}d tracked · best ${fmtMetric(k, st.max, st.unit)}` : ""}
        </div>
      </button>`;
  }).join("");

  body.innerHTML = `
    ${tiles ? `<div class="h-tiles">${tiles}</div>`
            : `<p class="empty-state">Nothing logged yet. Connect a provider or add a reading below.</p>`}
    <p class="settings-hint">Click any metric to break it down.</p>

    <div class="joint-card h-add">
      <div class="block-title">Log a reading</div>
      <div class="field-row-inline">
        <select id="h-metric-in">
          ${HEALTH_ORDER.map((k) => `<option value="${k}">${esc(metricLabel(k))}</option>`).join("")}
        </select>
        <input type="number" id="h-value" step="any" placeholder="value">
        <input type="date" id="h-date" value="${todayISO()}">
        <button class="btn primary" id="h-log">Log</button>
      </div>
    </div>`;

  body.querySelectorAll("[data-metric]").forEach((b) =>
    b.addEventListener("click", () => {
      hMetric = b.dataset.metric; hView = "detail"; renderHealth();
    }));

  el("h-log").addEventListener("click", async () => {
    const v = el("h-value").value;
    if (v === "") { toast("Enter a value", "error"); return; }
    await API.post("/health", {
      metric: el("h-metric-in").value, value: Number(v), date: el("h-date").value,
    });
    toast("Logged"); renderHealth();
  });
}

// ---------- one metric, in depth ----------
async function healthDetail(body) {
  const d = await API.get(`/health/detail?metric=${encodeURIComponent(hMetric)}&days=${hRange}`);
  const { stats: st, series, weekday, sources } = d;

  if (!st.count) {
    body.innerHTML = `<p class="empty-state">No ${esc(metricLabel(hMetric))} readings in this range.</p>`;
    return;
  }

  const f = (v) => fmtMetric(hMetric, v, st.unit);
  const statRow = (label, value, hint) => `
    <div class="h-stat"><div class="h-stat-label">${label}</div>
      <div class="h-stat-value">${value}</div>
      ${hint ? `<div class="h-stat-hint">${hint}</div>` : ""}</div>`;

  const wdMax = Math.max(...weekday.map((w) => w.avg || 0)) || 1;

  body.innerHTML = `
    <div class="h-detail-head">
      <button class="btn small" id="h-back">← All metrics</button>
      <h2 class="block-title">${esc(st.label)} · last ${st.days > 999 ? "everything" : st.days + " days"}</h2>
    </div>

    <div class="h-stats">
      ${statRow("Average", f(st.avg), `median ${f(st.median)}`)}
      ${statRow("Best", f(st.max), fmtDate(st.best_day.date))}
      ${statRow("Lowest", f(st.min), fmtDate(st.worst_day.date))}
      ${statRow("Total", st.unit === "bpm" || hMetric === "weight_kg" ? "—" : f(st.total), "")}
      ${statRow("Tracked", `${st.count} days`, `${st.coverage_pct}% coverage`)}
      ${statRow("Trend", st.trend_pct == null ? "—"
          : `<span class="${st.trend_pct > 0 ? "up" : st.trend_pct < 0 ? "down" : ""}">${st.trend_pct > 0 ? "▲" : st.trend_pct < 0 ? "▼" : ""} ${Math.abs(st.trend_pct)}%</span>`,
          "2nd half vs 1st")}
    </div>

    <div class="joint-card">
      <div class="block-title">Daily</div>
      ${bigChart(series, hMetric, st)}
    </div>

    <div class="h-two-col">
      <div class="joint-card">
        <div class="block-title">By day of week</div>
        <div class="h-wd">
          ${weekday.map((w) => `
            <div class="h-wd-row">
              <span class="h-wd-name">${w.weekday}</span>
              <span class="h-wd-bar"><span style="width:${w.avg ? (w.avg / wdMax) * 100 : 0}%"></span></span>
              <span class="h-wd-val">${w.avg != null ? f(w.avg) : "—"}</span>
            </div>`).join("")}
        </div>
      </div>

      <div class="joint-card">
        <div class="block-title">Where it came from</div>
        ${sources.map((s) => `
          <div class="h-src-row">
            <span class="h-src-name">${esc(s.source)}</span>
            <span class="card-meta">${s.n} readings · ${fmtDate(s.first)} → ${fmtDate(s.last)}</span>
          </div>`).join("") || `<p class="empty-state small">No sources.</p>`}
      </div>
    </div>`;

  el("h-back").addEventListener("click", () => { hView = "overview"; hMetric = null; renderHealth(); });
}

// A chart with real axis labels - the overview sparkline is for shape, this
// is for reading actual values off it.
function bigChart(series, metric, st) {
  if (!series.length) return `<p class="empty-state small">No data.</p>`;
  const max = st.max || 1;
  const f = (v) => fmtMetric(metric, v, st.unit);
  return `
    <div class="h-big">
      <div class="h-axis">
        <span>${f(max)}</span><span>${f(max / 2)}</span><span>0</span>
      </div>
      <div class="h-big-bars">
        ${series.map((r) => `
          <span class="h-bar big" style="height:${Math.max(2, (r.value / max) * 100)}%"
                title="${esc(r.local_date)} — ${esc(f(r.value))}${r.source ? " (" + esc(r.source) + ")" : ""}"></span>`).join("")}
      </div>
    </div>
    <div class="h-xaxis">
      <span>${fmtDate(series[0].local_date)}</span>
      <span>${fmtDate(series[series.length - 1].local_date)}</span>
    </div>`;
}

// ---------- raw explorer ----------
async function healthRaw(body) {
  const params = new URLSearchParams({ limit: "1000" });
  if (window.OPSDECK.hRawMetric) params.set("metric", window.OPSDECK.hRawMetric);
  if (window.OPSDECK.hRawSource) params.set("source", window.OPSDECK.hRawSource);
  const d = await API.get(`/health/raw?${params}`);

  body.innerHTML = `
    <div class="joint-card">
      <div class="block-title-row">
        <div class="block-title">Every reading</div>
        <span class="card-meta">${d.rows.length} rows</span>
      </div>
      <div class="field-row-inline">
        <select id="hr-metric">
          <option value="">All metrics</option>
          ${d.tracked.map((m) => `<option value="${m}" ${window.OPSDECK.hRawMetric === m ? "selected" : ""}>${esc(metricLabel(m))}</option>`).join("")}
        </select>
        <select id="hr-source">
          <option value="">All sources</option>
          ${d.sources.map((s) => `<option value="${escAttr(s.source)}" ${window.OPSDECK.hRawSource === s.source ? "selected" : ""}>${esc(s.source)} (${s.n})</option>`).join("")}
        </select>
        <button class="btn" id="hr-clear">Clear</button>
      </div>
      <div class="h-table-wrap">
        <table class="h-table">
          <thead><tr><th>Date</th><th>Metric</th><th>Value</th><th>Source</th><th>Recorded</th></tr></thead>
          <tbody>
            ${d.rows.map((r) => `
              <tr>
                <td class="mono">${esc(r.local_date)}</td>
                <td>${esc(metricLabel(r.metric))}</td>
                <td class="mono">${esc(fmtMetric(r.metric, r.value, r.unit))}</td>
                <td><span class="chip static ${r.source === "manual" ? "c-gray" : "c-teal"}">${esc(r.source)}</span></td>
                <td class="card-meta">${esc((r.recorded_at || "").slice(0, 16))}</td>
              </tr>`).join("") || `<tr><td colspan="5"><p class="empty-state small">Nothing matches.</p></td></tr>`}
          </tbody>
        </table>
      </div>
    </div>`;

  el("hr-metric").addEventListener("change", (e) => {
    window.OPSDECK.hRawMetric = e.target.value || null; renderHealth();
  });
  el("hr-source").addEventListener("change", (e) => {
    window.OPSDECK.hRawSource = e.target.value || null; renderHealth();
  });
  el("hr-clear").addEventListener("click", () => {
    window.OPSDECK.hRawMetric = null; window.OPSDECK.hRawSource = null; renderHealth();
  });
}
