// Homelab: what the estate is, what each box is for, and what to do next.
//
// Two views. Devices is the inventory, each card carrying its purpose, specs
// and its own recommendations. Roadmap is every recommendation across the lab
// sorted by severity, because "what should I do next" is the question this
// section actually exists to answer - an inventory alone is just a list.
//
// Reachability is probed server-side on load and never cached: a stored
// "online" flag is wrong the moment something is unplugged.

let hlView = "devices";        // devices | roadmap
let hlData = null;
let hlFilter = "all";          // all | open | high | done
let hlOpenDevice = null;       // expanded card

const HL_KIND_ICON = {
  server: "▣", guest: "▤", laptop: "▭", sbc: "▪", workstation: "◫",
  printer: "⎙", network: "⇄", iot: "◉", phone: "▯", other: "▫",
};
const HL_SEV_ORDER = { high: 0, medium: 1, low: 2 };
const HL_SEV_CLASS = { high: "c-red", medium: "c-amber", low: "c-gray" };
const HL_STATE_LABEL = {
  up: "up", down: "unreachable", "no-probe": "no probe",
  "expected-off": "off (expected)", unknown: "—",
};

function hlOpenUpgrades(list) {
  return (list || []).filter((u) => u.status !== "done" && u.status !== "declined");
}

async function renderHomelab() {
  const panel = el("panel");
  panel.innerHTML = `<div class="loading">Probing the lab…</div>`;

  try {
    hlData = await API.get("/homelab");
  } catch (e) {
    panel.innerHTML = `<p class="empty-state">Homelab module unavailable.</p>`;
    return;
  }
  const c = hlData.counts;

  panel.innerHTML = `
    <div class="section-head-row">
      <h1 class="section-title">Homelab</h1>
      <div class="head-actions">
        <button class="btn" id="hl-rescan">Re-probe</button>
        <button class="btn" id="hl-discover">Scan LAN</button>
        <button class="btn primary" id="hl-add">+ Device</button>
      </div>
    </div>

    <div class="h-tiles hl-tiles">
      <div class="joint-card ac-tile">
        <div class="h-label">Devices</div>
        <div class="h-value">${c.devices}</div>
        <div class="h-sub">${c.up} reachable · ${c.building} building</div>
      </div>
      <div class="joint-card ac-tile">
        <div class="h-label">Open actions</div>
        <div class="h-value">${c.upgrades_open}</div>
        <div class="h-sub">${c.upgrades_done} done</div>
      </div>
      <div class="joint-card ac-tile ${c.upgrades_high ? "hl-alarm" : ""}">
        <div class="h-label">High severity</div>
        <div class="h-value">${c.upgrades_high}</div>
        <div class="h-sub">${c.upgrades_high ? "worth doing first" : "all clear"}</div>
      </div>
    </div>

    <div class="h-controls">
      <div class="h-views">
        ${[["devices", "Devices"], ["roadmap", "Roadmap"]].map(([v, l]) =>
          `<button class="board-tab ${hlView === v ? "active" : ""}" data-hlview="${v}">${l}</button>`
        ).join("")}
      </div>
    </div>

    <div id="hl-body"></div>`;

  panel.querySelectorAll("[data-hlview]").forEach((b) =>
    b.addEventListener("click", () => { hlView = b.dataset.hlview; renderHomelab(); }));
  el("hl-rescan").addEventListener("click", renderHomelab);
  el("hl-add").addEventListener("click", () => deviceModal(null));
  el("hl-discover").addEventListener("click", runDiscover);

  if (hlView === "roadmap") paintRoadmap();
  else paintDevices();
}

// ---------------------------------------------------------------- devices

function paintDevices() {
  const body = el("hl-body");
  const groups = {};
  (hlData.devices || []).forEach((d) => (groups[d.kind] ||= []).push(d));

  const order = ["server", "guest", "workstation", "laptop", "sbc", "printer",
                 "network", "iot", "phone", "other"];
  const html = order.filter((k) => groups[k]).map((k) => `
    <div class="block-title-row"><h2 class="block-title">${esc(k)}</h2></div>
    <div class="hl-grid">${groups[k].map(deviceCard).join("")}</div>`).join("");

  body.innerHTML = html || `<p class="empty-state">No devices yet.</p>`;

  body.querySelectorAll("[data-hl-toggle]").forEach((b) =>
    b.addEventListener("click", (e) => {
      if (e.target.closest("button.btn")) return;
      const id = Number(b.dataset.hlToggle);
      hlOpenDevice = hlOpenDevice === id ? null : id;
      paintDevices();
    }));
  body.querySelectorAll("[data-hl-edit]").forEach((b) =>
    b.addEventListener("click", () => deviceModal(Number(b.dataset.hlEdit))));
  body.querySelectorAll("[data-hl-addup]").forEach((b) =>
    b.addEventListener("click", () => upgradeModal(null, Number(b.dataset.hlAddup))));
  wireUpgradeRows(body);
}

function deviceCard(d) {
  const open = hlOpenDevice === d.id;
  const openUps = hlOpenUpgrades(d.upgrades);
  const high = openUps.filter((u) => u.severity === "high").length;

  return `
    <div class="joint-card hl-card state-${esc(d.state)} ${open ? "open" : ""}"
         data-hl-toggle="${d.id}">
      <div class="hl-head">
        <span class="hl-icon">${HL_KIND_ICON[d.kind] || "▫"}</span>
        <span class="hl-name">${esc(d.name)}</span>
        <span class="hl-state" title="${escAttr(
          d.probe_port ? `TCP ${d.probe_host || d.lan_ip}:${d.probe_port}` : "nothing to probe")}">
          <span class="hl-dot"></span>${esc(HL_STATE_LABEL[d.state] || d.state)}
        </span>
      </div>
      <div class="hl-meta">
        <span class="chip static">${esc(d.status)}</span>
        ${d.lan_ip ? `<span class="mono">${esc(d.lan_ip)}</span>` : ""}
        ${d.tailscale_ip ? `<span class="mono ac-dim">ts ${esc(d.tailscale_ip)}</span>` : ""}
        ${high ? `<span class="chip static c-red">${high} high</span>`
               : openUps.length ? `<span class="chip static">${openUps.length} open</span>` : ""}
      </div>
      <div class="hl-purpose">${esc(d.purpose)}</div>

      ${open ? `
        ${d.specs ? `<div class="hl-specs">${d.specs.split("\n").map((l) =>
          `<div>${esc(l)}</div>`).join("")}</div>` : ""}
        ${d.notes ? `<div class="hl-notes">${esc(d.notes)}</div>` : ""}
        ${d.upgrades.length ? `
          <div class="hl-sub-title">Recommendations</div>
          ${d.upgrades.slice().sort(upSort).map(upgradeRow).join("")}` : ""}
        <div class="field-row-inline hl-card-actions">
          <button class="btn tiny" data-hl-edit="${d.id}">Edit</button>
          <button class="btn tiny" data-hl-addup="${d.id}">+ Recommendation</button>
        </div>` : ""}
    </div>`;
}

// ---------------------------------------------------------------- roadmap

function upSort(a, b) {
  const doneRank = (u) => (u.status === "done" || u.status === "declined" ? 1 : 0);
  if (doneRank(a) !== doneRank(b)) return doneRank(a) - doneRank(b);
  return (HL_SEV_ORDER[a.severity] ?? 3) - (HL_SEV_ORDER[b.severity] ?? 3);
}

function paintRoadmap() {
  const body = el("hl-body");
  const byId = {};
  (hlData.devices || []).forEach((d) => (byId[d.id] = d.name));

  let all = [...(hlData.lab_upgrades || [])];
  (hlData.devices || []).forEach((d) => all.push(...d.upgrades));

  if (hlFilter === "open") all = all.filter((u) => u.status !== "done" && u.status !== "declined");
  if (hlFilter === "high") all = all.filter((u) => u.severity === "high");
  if (hlFilter === "done") all = all.filter((u) => u.status === "done");
  all.sort(upSort);

  body.innerHTML = `
    <div class="tree-filters">
      ${[["all", "Everything"], ["open", "Open"], ["high", "High severity"], ["done", "Done"]]
        .map(([f, l]) => `<button class="chip ${hlFilter === f ? "on c-amber" : ""}"
          data-hlfilter="${f}">${l}</button>`).join("")}
      <button class="btn tiny" id="hl-add-lab-up">+ Lab-wide recommendation</button>
    </div>
    <p class="settings-hint">Severity is rated for <em>this</em> environment — a
    single-node lab on a trusted LAN with remote access already behind Tailscale
    — not a generic checklist.</p>
    <div class="hl-roadmap">${all.map((u) => upgradeRow(u, byId[u.device_id])).join("")
      || `<p class="empty-state">Nothing here.</p>`}</div>`;

  body.querySelectorAll("[data-hlfilter]").forEach((b) =>
    b.addEventListener("click", () => { hlFilter = b.dataset.hlfilter; paintRoadmap(); }));
  el("hl-add-lab-up").addEventListener("click", () => upgradeModal(null, null));
  wireUpgradeRows(body);
}

function upgradeRow(u, deviceName) {
  const done = u.status === "done" || u.status === "declined";
  return `
    <div class="hl-up ${done ? "done" : ""} sev-${esc(u.severity)}" data-up="${u.id}">
      <div class="hl-up-head">
        <span class="chip static ${HL_SEV_CLASS[u.severity] || ""}">${esc(u.severity)}</span>
        <span class="tag">${esc(u.category)}</span>
        <span class="hl-up-title">${esc(u.title)}</span>
        ${deviceName ? `<span class="tag t-blue">${esc(deviceName)}</span>` : ""}
        ${u.cost ? `<span class="hl-up-cost mono">${esc(u.cost)}</span>` : ""}
      </div>
      ${u.detail ? `<div class="hl-up-detail">${esc(u.detail)}</div>` : ""}
      <div class="hl-up-actions">
        <select class="hl-up-status" data-up-status="${u.id}">
          ${["idea", "planned", "doing", "done", "declined"].map((s) =>
            `<option value="${s}"${u.status === s ? " selected" : ""}>${s}</option>`).join("")}
        </select>
        <button class="btn tiny" data-up-edit="${u.id}">Edit</button>
        <button class="btn tiny" data-up-del="${u.id}">✕</button>
      </div>
    </div>`;
}

function wireUpgradeRows(scope) {
  scope.querySelectorAll("[data-up-status]").forEach((sel) =>
    sel.addEventListener("change", async (e) => {
      e.stopPropagation();
      await API.patch(`/homelab/upgrades/${sel.dataset.upStatus}`, { status: sel.value });
      renderHomelab();
    }));
  scope.querySelectorAll("[data-up-status]").forEach((sel) =>
    sel.addEventListener("click", (e) => e.stopPropagation()));
  scope.querySelectorAll("[data-up-edit]").forEach((b) =>
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      upgradeModal(Number(b.dataset.upEdit));
    }));
  scope.querySelectorAll("[data-up-del]").forEach((b) =>
    b.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm("Delete this recommendation?")) return;
      await API.del(`/homelab/upgrades/${b.dataset.upDel}`);
      renderHomelab();
    }));
}

// ----------------------------------------------------------------- modals

function findUpgrade(id) {
  for (const u of hlData.lab_upgrades || []) if (u.id === id) return u;
  for (const d of hlData.devices || []) for (const u of d.upgrades) if (u.id === id) return u;
  return null;
}

function deviceModal(id) {
  const d = id ? (hlData.devices || []).find((x) => x.id === id) : null;
  openModal(`
    <div class="modal-head">
      <h2 class="modal-title">${d ? "Edit device" : "New device"}</h2>
      <button class="icon-btn" onclick="closeModal()">×</button>
    </div>
    <label class="field-label">Name</label>
    <input type="text" id="hl-name" value="${escAttr(d?.name || "")}">
    <div class="field-row">
      <div>
        <label class="field-label">Kind</label>
        <select id="hl-kind">${(hlData.kinds || []).map((k) =>
          `<option value="${k}"${d?.kind === k ? " selected" : ""}>${k}</option>`).join("")}</select>
      </div>
      <div>
        <label class="field-label">Status</label>
        <select id="hl-status">${["active", "building", "planned", "retired"].map((s) =>
          `<option value="${s}"${d?.status === s ? " selected" : ""}>${s}</option>`).join("")}</select>
      </div>
    </div>
    <label class="field-label">Purpose</label>
    <textarea id="hl-purpose" class="modal-textarea" rows="2">${esc(d?.purpose || "")}</textarea>
    <label class="field-label">Specs (one per line)</label>
    <textarea id="hl-specs" class="modal-textarea" rows="5">${esc(d?.specs || "")}</textarea>
    <div class="field-row">
      <div><label class="field-label">LAN IP</label>
        <input type="text" id="hl-lan" value="${escAttr(d?.lan_ip || "")}"></div>
      <div><label class="field-label">Tailscale IP</label>
        <input type="text" id="hl-ts" value="${escAttr(d?.tailscale_ip || "")}"></div>
    </div>
    <div class="field-row">
      <div><label class="field-label">Probe host</label>
        <input type="text" id="hl-phost" value="${escAttr(d?.probe_host || "")}"
               placeholder="defaults to the LAN IP"></div>
      <div><label class="field-label">Probe port (0 = none)</label>
        <input type="number" id="hl-pport" value="${d?.probe_port ?? 0}"></div>
    </div>
    <label class="field-label">Notes</label>
    <textarea id="hl-notes" class="modal-textarea" rows="3">${esc(d?.notes || "")}</textarea>
    <div class="modal-actions">
      ${d ? `<button class="btn danger" id="hl-del">Delete</button>` : ""}
      <button class="btn primary" id="hl-save">Save</button>
    </div>`, () => {
    el("hl-del")?.addEventListener("click", async () => {
      if (!confirm(`Delete ${d.name} and its recommendations?`)) return;
      await API.del(`/homelab/devices/${d.id}`);
      closeModal(); renderHomelab();
    });
    el("hl-save").addEventListener("click", async () => {
      const payload = {
        name: el("hl-name").value.trim(), kind: el("hl-kind").value,
        status: el("hl-status").value, purpose: el("hl-purpose").value.trim(),
        specs: el("hl-specs").value, lan_ip: el("hl-lan").value.trim(),
        tailscale_ip: el("hl-ts").value.trim(),
        probe_host: el("hl-phost").value.trim(),
        probe_port: Number(el("hl-pport").value) || 0,
        notes: el("hl-notes").value.trim(),
      };
      if (!payload.name) { toast("Name required", "error"); return; }
      try {
        if (d) await API.patch(`/homelab/devices/${d.id}`, payload);
        else await API.post("/homelab/devices", payload);
      } catch (e) { return; }
      closeModal(); renderHomelab();
    });
  });
}

function upgradeModal(id, deviceId) {
  const u = id ? findUpgrade(id) : null;
  const target = u ? u.device_id : deviceId;
  openModal(`
    <div class="modal-head">
      <h2 class="modal-title">${u ? "Edit recommendation" : "New recommendation"}</h2>
      <button class="icon-btn" onclick="closeModal()">×</button>
    </div>
    <label class="field-label">Title</label>
    <input type="text" id="hu-title" value="${escAttr(u?.title || "")}">
    <label class="field-label">Detail</label>
    <textarea id="hu-detail" class="modal-textarea" rows="5">${esc(u?.detail || "")}</textarea>
    <div class="field-row">
      <div><label class="field-label">Category</label>
        <select id="hu-cat">${(hlData.categories || []).map((c) =>
          `<option value="${c}"${u?.category === c ? " selected" : ""}>${c}</option>`).join("")}</select></div>
      <div><label class="field-label">Severity</label>
        <select id="hu-sev">${["high", "medium", "low"].map((s) =>
          `<option value="${s}"${u?.severity === s ? " selected" : ""}>${s}</option>`).join("")}</select></div>
    </div>
    <div class="field-row">
      <div><label class="field-label">Cost</label>
        <input type="text" id="hu-cost" value="${escAttr(u?.cost || "")}" placeholder="free, ~$30, $400+"></div>
      <div><label class="field-label">Applies to</label>
        <select id="hu-dev">
          <option value="">the lab as a whole</option>
          ${(hlData.devices || []).map((d) =>
            `<option value="${d.id}"${target === d.id ? " selected" : ""}>${esc(d.name)}</option>`).join("")}
        </select></div>
    </div>
    <div class="modal-actions">
      <button class="btn primary" id="hu-save">Save</button>
    </div>`, () => {
    el("hu-save").addEventListener("click", async () => {
      const payload = {
        title: el("hu-title").value.trim(), detail: el("hu-detail").value.trim(),
        category: el("hu-cat").value, severity: el("hu-sev").value,
        cost: el("hu-cost").value.trim(),
        device_id: el("hu-dev").value ? Number(el("hu-dev").value) : null,
      };
      if (!payload.title) { toast("Title required", "error"); return; }
      try {
        if (u) await API.patch(`/homelab/upgrades/${u.id}`, payload);
        else await API.post("/homelab/upgrades", payload);
      } catch (e) { return; }
      closeModal(); renderHomelab();
    });
  });
}

// ---------------------------------------------------------------- discover

async function runDiscover() {
  const btn = el("hl-discover");
  btn.disabled = true; btn.textContent = "Scanning…";
  let r;
  try {
    r = await API.post("/homelab/discover", { subnet: "192.168.1" });
  } catch (e) {
    btn.disabled = false; btn.textContent = "Scan LAN";
    return;
  }
  btn.disabled = false; btn.textContent = "Scan LAN";

  openModal(`
    <div class="modal-head">
      <h2 class="modal-title">LAN scan — ${r.subnet}.0/24</h2>
      <button class="icon-btn" onclick="closeModal()">×</button>
    </div>
    <p class="settings-hint">TCP knock on ports ${r.ports_tried.join(", ")}.
    Hosts that answer nothing on those ports stay invisible — this finds
    services, not every device. ${r.new_count} address${r.new_count === 1 ? "" : "es"}
    not in the inventory.</p>
    <div class="hl-scan">
      ${r.found.map((f) => `
        <div class="hl-scan-row ${f.new ? "new" : ""}">
          <span class="mono">${esc(f.ip)}</span>
          <span class="ac-dim">:${f.port}</span>
          <span>${f.known_as ? esc(f.known_as) : `<em>not in inventory</em>`}</span>
          ${f.new ? `<button class="btn tiny" data-adopt="${escAttr(f.ip)}">Add</button>` : ""}
        </div>`).join("") || `<p class="empty-state small">Nothing answered.</p>`}
    </div>`, (modal) => {
    modal.querySelectorAll("[data-adopt]").forEach((b) =>
      b.addEventListener("click", async () => {
        await API.post("/homelab/devices", {
          name: `Unidentified ${b.dataset.adopt}`, kind: "other", status: "active",
          lan_ip: b.dataset.adopt, probe_host: b.dataset.adopt, probe_port: 80,
          purpose: "Found by a LAN scan — identify and describe.",
        });
        closeModal(); renderHomelab();
      }));
  });
}
