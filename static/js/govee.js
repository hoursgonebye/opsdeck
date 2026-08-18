// Govee: lights, over the cloud API.
//
// Controls are optimistic - the slider moves under your thumb and the request
// goes after - because a round trip to Govee's servers is 200-600ms and a UI
// that waits for it feels broken. State is re-read after each change so the
// optimism is always corrected within a second.
//
// Brightness and colour changes are debounced. Dragging a slider fires an
// event per pixel, and Govee rate-limits per device; without this a single
// drag would spend a minute's quota and get itself throttled.

let gvConfig = null;
let gvDevices = [];
let gvActive = null;      // currently selected device id
let gvState = null;
let gvSendTimer = null;
let gvBusy = false;

const GV_PRESETS = [
  ["Warm", 2700], ["Soft", 3400], ["Neutral", 4200],
  ["Cool", 5600], ["Daylight", 6500],
];
const GV_COLORS = [
  ["#ff4d4d", 255, 77, 77], ["#ff9f43", 255, 159, 67], ["#ffe066", 255, 224, 102],
  ["#4ade80", 74, 222, 128], ["#38bdf8", 56, 189, 248], ["#a78bfa", 167, 139, 250],
  ["#f472b6", 244, 114, 182], ["#ffffff", 255, 255, 255],
];

function gvRgbHex(packed) {
  if (packed === null || packed === undefined) return null;
  const n = Number(packed);
  if (Number.isNaN(n)) return null;
  return "#" + n.toString(16).padStart(6, "0");
}

async function renderGovee() {
  const panel = el("panel");
  panel.innerHTML = `<div class="loading">Loading…</div>`;

  try {
    gvConfig = await API.get("/govee/config");
  } catch (e) {
    panel.innerHTML = `<p class="empty-state">Govee module unavailable.</p>`;
    return;
  }

  panel.innerHTML = `
    <div class="section-head-row">
      <h1 class="section-title">Lights</h1>
      <div class="head-actions">
        ${gvConfig.configured
          ? `<button class="btn" id="gv-refresh">Refresh</button>
             <button class="btn" id="gv-forget">Forget key</button>`
          : ""}
      </div>
    </div>
    <div id="gv-body"></div>`;

  el("gv-refresh")?.addEventListener("click", () => loadDevices(true));
  el("gv-forget")?.addEventListener("click", async () => {
    if (!confirm("Remove the saved Govee API key?")) return;
    await API.del("/govee/key");
    renderGovee();
  });

  if (!gvConfig.configured) { paintKeyPrompt(); return; }
  loadDevices(false);
}

// The section is useless without a key and the key can only come from a phone,
// so this is a first-class screen rather than a line in Settings.
function paintKeyPrompt(errorMsg) {
  el("gv-body").innerHTML = `
    <div class="joint-card gv-setup">
      <div class="block-title">Connect your Govee account</div>
      ${errorMsg ? `<div class="ac-warn">${esc(errorMsg)}</div>` : ""}
      <p class="settings-hint">Your light can't be reached from this network —
      it isn't on the LAN, it doesn't answer Govee's local API, and Bluetooth
      isn't usable from the server. The cloud API is the one route that works,
      and it needs a key from your phone.</p>
      <ol class="gv-steps">
        <li>Open the <strong>Govee Home</strong> app.</li>
        <li>Profile → <strong>About Us</strong> → <strong>Apply for API Key</strong>.</li>
        <li>Fill in the reason (“personal home automation” is fine). The key
            arrives by email, usually within a few minutes.</li>
        <li>Paste it below.</li>
      </ol>
      <div class="field-row-inline">
        <input type="password" id="gv-key" placeholder="Govee API key"
               autocomplete="off" spellcheck="false">
        <button class="btn primary" id="gv-save">Save &amp; verify</button>
      </div>
      <p class="settings-hint">It's stored on your server and never sent back to
      the browser. The key is checked against Govee before it's accepted, so a
      typo fails here rather than silently later.</p>
    </div>`;

  el("gv-save").addEventListener("click", async () => {
    const key = el("gv-key").value.trim();
    if (!key) { toast("Paste the key first", "error"); return; }
    const btn = el("gv-save");
    btn.disabled = true; btn.textContent = "Verifying…";
    try {
      const r = await API.request("PUT", "/govee/key", { api_key: key });
      toast(`Connected — ${r.device_count} device${r.device_count === 1 ? "" : "s"} found`,
            "info", 5000);
      renderGovee();
    } catch (e) {
      btn.disabled = false; btn.textContent = "Save & verify";
    }
  });
}

async function loadDevices(force) {
  const body = el("gv-body");
  body.innerHTML = `<div class="loading">Talking to Govee…</div>`;
  let r;
  try {
    r = await API.get(`/govee/devices${force ? "?refresh=1" : ""}`);
  } catch (e) {
    paintKeyPrompt("Govee refused that key, or the service is unreachable.");
    return;
  }
  gvDevices = r.devices || [];
  if (!gvDevices.length) {
    body.innerHTML = `<p class="empty-state">The key works, but this Govee
      account has no devices on it.</p>`;
    return;
  }
  gvActive = gvActive && gvDevices.some((d) => d.device === gvActive)
    ? gvActive
    : (r.primary && gvDevices.some((d) => d.device === r.primary)
        ? r.primary : gvDevices[0].device);
  paintDevices();
  loadState();
}

function paintDevices() {
  const chips = gvDevices.map((d) => `
    <button class="chip ${d.device === gvActive ? "on c-amber" : ""}" data-gv="${escAttr(d.device)}">
      ${esc(d.name)}
    </button>`).join("");

  el("gv-body").innerHTML = `
    ${gvDevices.length > 1 ? `<div class="gv-picker">${chips}</div>` : ""}
    <div id="gv-panel"><div class="loading">Reading state…</div></div>`;

  el("gv-body").querySelectorAll("[data-gv]").forEach((b) =>
    b.addEventListener("click", () => {
      gvActive = b.dataset.gv;
      API.request("PUT", "/govee/primary", { device: gvActive }).catch(() => {});
      paintDevices();
      loadState();
    }));
}

async function loadState() {
  try {
    gvState = await API.get(`/govee/state?device=${encodeURIComponent(gvActive)}`);
  } catch (e) {
    gvState = null;
  }
  paintPanel();
}

function paintPanel() {
  const box = el("gv-panel");
  if (!box) return;
  const dev = gvDevices.find((d) => d.device === gvActive);
  if (!dev) { box.innerHTML = `<p class="empty-state">Device not found.</p>`; return; }

  if (!gvState) {
    box.innerHTML = `
      <div class="joint-card">
        <div class="block-title">${esc(dev.name)}</div>
        <p class="settings-hint">Couldn't read this light's state. It may be
        powered off at the wall — controls still work if it's merely idle.</p>
        ${controlsHtml(dev, {})}
      </div>`;
    wireControls(dev);
    return;
  }

  const on = gvState.power === 1 || gvState.power === true;
  const hex = gvRgbHex(gvState.color);
  // Govee still reports a last-known power/colour for a bulb it cannot reach,
  // so without this the panel would show a confident "On" for a lamp that is
  // dark, and every control would fail with a bare 400.
  const offline = gvState.online === false;

  box.innerHTML = `
    <div class="joint-card gv-card ${offline ? "offline" : ""}">
      <div class="gv-head">
        <span class="gv-bulb ${on && !offline ? "on" : ""}"
              style="${on && !offline && hex ? `--bulb:${esc(hex)}` : ""}"></span>
        <div>
          <div class="gv-name">${esc(gvState.name || dev.name)}</div>
          <div class="gv-meta mono">${esc(dev.sku)} · ${esc(dev.device)}</div>
        </div>
        <button class="btn ${on && !offline ? "primary" : ""} gv-power" id="gv-power"
                ${offline ? "disabled" : ""}>
          ${offline ? "—" : on ? "On" : "Off"}
        </button>
      </div>
      ${offline ? `
        <div class="ac-warn gv-offline">
          <span><strong>Offline.</strong> Govee can't reach this bulb — it's
          switched off at the wall or has dropped off Wi-Fi. The values below are
          the last state Govee saw, and controls stay disabled until it's back.</span>
        </div>` : ""}
      <div class="${offline ? "gv-disabled" : ""}">${controlsHtml(dev, gvState)}</div>
    </div>`;
  if (!offline) wireControls(dev);
}

function controlsHtml(dev, state) {
  const bright = state.brightness ?? 50;
  const temp = state.color_temp || 4000;
  return `
    ${dev.supports.brightness ? `
      <div class="gv-ctl">
        <div class="h-label">Brightness <span id="gv-bright-val">${bright}%</span></div>
        <input type="range" id="gv-bright" min="1" max="100" value="${bright}">
      </div>` : ""}

    ${dev.supports.color ? `
      <div class="gv-ctl">
        <div class="h-label">Colour</div>
        <div class="gv-swatches">
          ${GV_COLORS.map(([hex, r, g, b]) =>
            `<button class="gv-sw" style="background:${hex}" title="${hex}"
                     data-r="${r}" data-g="${g}" data-b="${b}"></button>`).join("")}
          <input type="color" id="gv-color" value="${escAttr(gvRgbHex(state.color) || "#ffffff")}"
                 title="Custom colour">
        </div>
      </div>` : ""}

    ${dev.supports.color_temp ? `
      <div class="gv-ctl">
        <div class="h-label">White <span id="gv-temp-val">${temp}K</span></div>
        <input type="range" id="gv-temp" min="2000" max="9000" step="100" value="${temp}">
        <div class="gv-presets">
          ${GV_PRESETS.map(([label, k]) =>
            `<button class="chip" data-k="${k}">${label}</button>`).join("")}
        </div>
      </div>` : ""}`;
}

function wireControls(dev) {
  el("gv-power")?.addEventListener("click", async () => {
    const on = gvState && (gvState.power === 1 || gvState.power === true);
    await send("power", on ? 0 : 1, true);
  });

  const bright = el("gv-bright");
  bright?.addEventListener("input", () => {
    el("gv-bright-val").textContent = `${bright.value}%`;
    queue("brightness", Number(bright.value));
  });

  const temp = el("gv-temp");
  temp?.addEventListener("input", () => {
    el("gv-temp-val").textContent = `${temp.value}K`;
    queue("color_temp", Number(temp.value));
  });

  document.querySelectorAll("[data-k]").forEach((b) =>
    b.addEventListener("click", () => {
      if (temp) { temp.value = b.dataset.k; el("gv-temp-val").textContent = `${b.dataset.k}K`; }
      send("color_temp", Number(b.dataset.k), true);
    }));

  document.querySelectorAll(".gv-sw").forEach((b) =>
    b.addEventListener("click", () => send("color", {
      r: Number(b.dataset.r), g: Number(b.dataset.g), b: Number(b.dataset.b),
    }, true)));

  const picker = el("gv-color");
  picker?.addEventListener("change", () => {
    const h = picker.value.replace("#", "");
    queue("color", {
      r: parseInt(h.slice(0, 2), 16),
      g: parseInt(h.slice(2, 4), 16),
      b: parseInt(h.slice(4, 6), 16),
    });
  });
}

// Coalesce a drag into one request, sent once the control settles.
function queue(action, value) {
  clearTimeout(gvSendTimer);
  gvSendTimer = setTimeout(() => send(action, value, false), 320);
}

async function send(action, value, refresh) {
  if (gvBusy) return;
  gvBusy = true;
  try {
    await API.post("/govee/control", { device: gvActive, action, value });
  } catch (e) {
    gvBusy = false;
    return;
  }
  gvBusy = false;
  // Power and presets change enough of the picture to be worth re-reading;
  // a slider drag does not, and re-rendering mid-drag would fight the thumb.
  if (refresh) loadState();
}
