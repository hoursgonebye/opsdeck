// Printer: the camera and the machine state, plus the real Fluidd UI embedded.
//
// The camera image is fetched with the API client and turned into a blob URL
// rather than pointed at ?token=... in an <img src>. Same pixels, but the API
// token stays out of the URL bar, out of history and out of any Referer -
// which matters more here than usual because this is the one section whose
// images a browser is asked to load repeatedly.
//
// Live streaming is opt-in for the same reason the server caps it: a held-open
// MJPEG socket is a real cost, and a still every couple of seconds is what
// watching a 6-hour print actually needs.

let prState = null;
let prConfig = null;
let prTimer = null;        // status + snapshot poll
let prLive = false;        // live MJPEG instead of polled stills
let prShowUI = false;      // Fluidd iframe expanded
let prBlobUrl = null;      // current snapshot object URL, revoked on replace
let prCamOk = true;

const PR_POLL_MS = 2000;

const PR_STATE_COLOR = {
  printing: "c-green", paused: "c-amber", complete: "c-blue",
  cancelled: "c-gray", error: "c-red", standby: "c-gray",
  shutdown: "c-red", disconnected: "c-red",
};

function prDuration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  return `${s}s`;
}

function prTemp(h) {
  if (!h) return "—";
  return h.target > 0
    ? `${h.temp.toFixed(1)}° <span class="ac-dim">/ ${h.target.toFixed(0)}°</span>`
    : `${h.temp.toFixed(1)}°`;
}

// Leaving this section must stop the polling. Without it, switching to Today
// leaves a timer hitting the printer forever and quietly holding the tab's
// battery hostage on a phone.
function stopPrinterPolling() {
  if (prTimer) { clearInterval(prTimer); prTimer = null; }
  if (prBlobUrl) { URL.revokeObjectURL(prBlobUrl); prBlobUrl = null; }
}

async function renderPrinter() {
  const panel = el("panel");
  panel.innerHTML = `<div class="loading">Loading…</div>`;
  stopPrinterPolling();

  try {
    prConfig = prConfig || await API.get("/printer/config");
  } catch (e) {
    panel.innerHTML = `<p class="empty-state">Printer module unavailable.</p>`;
    return;
  }
  prState = await API.get("/printer/status");

  panel.innerHTML = `
    <div class="section-head-row">
      <h1 class="section-title">Printer</h1>
      <div class="head-actions">
        <span id="pr-state-chip"></span>
        <a class="btn" href="${escAttr(prConfig.ui_url || prConfig.lan_url)}"
           target="_blank" rel="noopener">Open Fluidd</a>
      </div>
    </div>

    <div class="pr-grid">
      <div class="joint-card pr-cam-card">
        <div class="pr-cam-head">
          <span class="block-title">Camera</span>
          <button class="chip ${prLive ? "on c-amber" : ""}" id="pr-live">
            ${prLive ? "live" : "stills"}
          </button>
          <span class="pr-cam-note" id="pr-cam-note"></span>
        </div>
        <div class="pr-cam" id="pr-cam">
          <div class="pr-cam-placeholder">connecting…</div>
        </div>
      </div>

      <div class="joint-card pr-status-card" id="pr-status"></div>
    </div>

    <div class="block-title-row">
      <h2 class="block-title">Full dashboard</h2>
    </div>
    <div class="field-row-inline">
      <button class="btn" id="pr-toggle-ui">
        ${prShowUI ? "Hide" : "Show"} the Fluidd dashboard
      </button>
      <span class="settings-hint">Embedded over the tailnet. Full control —
      everything the printer's own page can do.</span>
    </div>
    <div id="pr-ui-wrap"></div>`;

  el("pr-live").addEventListener("click", () => {
    prLive = !prLive;
    renderPrinter();
  });
  el("pr-toggle-ui").addEventListener("click", () => {
    prShowUI = !prShowUI;
    renderPrinter();
  });

  paintStatus();
  mountCamera();
  renderFluidd();

  // One timer drives both the status card and, in stills mode, the image.
  prTimer = setInterval(async () => {
    if (activeSection !== "printer") { stopPrinterPolling(); return; }
    try { prState = await API.get("/printer/status"); } catch (e) { return; }
    paintStatus();
    if (!prLive) refreshSnapshot();
  }, PR_POLL_MS);
}

function paintStatus() {
  const s = prState || {};
  const chip = el("pr-state-chip");
  const label = s.online ? (s.state || "unknown") : "offline";
  if (chip) {
    chip.className = `chip static ${s.online ? (PR_STATE_COLOR[label] || "c-gray") : "c-red"}`;
    chip.textContent = label;
  }

  const box = el("pr-status");
  if (!box) return;

  if (!s.online) {
    box.innerHTML = `
      <div class="block-title">Offline</div>
      <p class="settings-hint">No answer from ${esc(s.host || "the printer")}.
      That is the normal look when the machine is powered down — this tab will
      pick it up again on its own once it is back.</p>
      <div class="pr-err mono">${esc(s.error || "")}</div>`;
    return;
  }

  if (s.klippy_state && s.klippy_state !== "ready") {
    box.innerHTML = `
      <div class="block-title">Klipper: ${esc(s.klippy_state)}</div>
      <p class="settings-hint">Moonraker is answering but Klipper is not ready —
      usually a shutdown or a config error. Open Fluidd to see the reason and
      restart the firmware.</p>`;
    return;
  }

  const printing = s.state === "printing" || s.state === "paused";
  const pct = Math.round((s.progress || 0) * 100);

  box.innerHTML = `
    <div class="pr-temps">
      <div class="pr-temp">
        <div class="h-label">Nozzle</div>
        <div class="pr-temp-val">${prTemp(s.extruder)}</div>
      </div>
      <div class="pr-temp">
        <div class="h-label">Bed</div>
        <div class="pr-temp-val">${prTemp(s.bed)}</div>
      </div>
    </div>

    ${printing || s.filename ? `
      <div class="pr-job">
        <div class="pr-file mono" title="${escAttr(s.filename)}">${esc(s.filename || "—")}</div>
        <div class="ac-bar pr-bar"><div class="ac-bar-fill" style="width:${pct}%"></div></div>
        <div class="pr-job-meta">
          <span><strong>${pct}%</strong></span>
          <span>elapsed ${prDuration(s.print_duration)}</span>
          <span>${s.eta_seconds !== null && s.eta_seconds !== undefined
            ? "left ~" + prDuration(s.eta_seconds) : "left —"}</span>
          <span>${s.filament_used_mm ? (s.filament_used_mm / 1000).toFixed(2) + " m" : ""}</span>
        </div>
      </div>` : `
      <p class="settings-hint">Idle — nothing queued. Temperatures above are
      live.</p>`}
    ${s.message ? `<div class="pr-msg">${esc(s.message)}</div>` : ""}`;
}

// ---------------------------------------------------------------- camera

function mountCamera() {
  const box = el("pr-cam");
  const note = el("pr-cam-note");
  if (!box) return;

  if (prLive) {
    // The one place a token in the URL is unavoidable: an <img> streaming
    // multipart cannot carry a custom header. Same-origin, tailnet-only.
    const url = `/api/printer/stream?token=${encodeURIComponent(window.OPSDECK.token)}`;
    box.innerHTML = `<img id="pr-img" alt="Printer camera (live)" src="${escAttr(url)}">`;
    note.textContent = "holding one connection open";
    el("pr-img").addEventListener("error", () => {
      box.innerHTML = `<div class="pr-cam-placeholder">Live stream unavailable.</div>`;
    });
    return;
  }

  box.innerHTML = `<img id="pr-img" alt="Printer camera">
    <div class="pr-cam-placeholder" id="pr-cam-ph">connecting…</div>`;
  note.textContent = `refreshing every ${PR_POLL_MS / 1000}s`;
  refreshSnapshot();
}

async function refreshSnapshot() {
  const img = el("pr-img");
  if (!img) return;
  let blob;
  try {
    const res = await fetch("/api/printer/snapshot", {
      headers: { "X-API-Token": window.OPSDECK.token },
    });
    if (!res.ok) throw new Error(String(res.status));
    blob = await res.blob();
  } catch (e) {
    if (prCamOk) {
      prCamOk = false;
      const ph = el("pr-cam-ph");
      if (ph) ph.textContent = "Camera unreachable.";
    }
    return;
  }
  prCamOk = true;
  el("pr-cam-ph")?.remove();
  const next = URL.createObjectURL(blob);
  img.src = next;
  // Revoke the previous frame only after swapping, or the visible image is
  // torn out from under the browser mid-decode and flickers.
  if (prBlobUrl) URL.revokeObjectURL(prBlobUrl);
  prBlobUrl = next;
}

// ---------------------------------------------------------------- Fluidd

function renderFluidd() {
  const wrap = el("pr-ui-wrap");
  if (!wrap) return;
  if (!prShowUI) { wrap.innerHTML = ""; return; }

  if (!prConfig.ui_url) {
    wrap.innerHTML = `<p class="empty-state">No HTTPS front is configured for the
      printer UI, so it cannot be embedded — open it directly instead.</p>`;
    return;
  }
  wrap.innerHTML = `
    <div class="pr-ui-frame">
      <iframe src="${escAttr(prConfig.ui_url)}" title="Fluidd"
              referrerpolicy="no-referrer"></iframe>
    </div>
    <p class="settings-hint">If this stays blank, the tailnet HTTPS front for the
    printer is down — <a href="${escAttr(prConfig.ui_url)}" target="_blank"
    rel="noopener">open it in a tab</a> to check.</p>`;
}
