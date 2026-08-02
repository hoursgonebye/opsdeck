// TryHackMe: completions in, direction out.
//
// The point of this section is not logging - it's that the mentor looks at
// the weakest and stalest parts of the tree and points at a specific next
// room. Completions carry no XP on their own: each one just offers a
// verification attempt on the nodes it's mapped to, and the strict mentor
// flow decides whether anything is earned.

let thmTreeNodes = null; // cached for the map-node selects

async function renderThm() {
  const panel = el("panel");
  panel.innerHTML = `<div class="loading">Loading…</div>`;

  const [data, tree] = await Promise.all([API.get("/thm"), API.get("/tree")]);
  thmTreeNodes = tree.nodes;

  const rec = data.recommendation;

  panel.innerHTML = `
    <div class="section-head-row">
      <h1 class="section-title">TryHackMe</h1>
      <div class="head-actions">
        <button class="btn" id="thm-sync" ${data.username ? "" : "disabled"}>Sync profile</button>
        <button class="btn primary" id="thm-log">+ Log completion</button>
      </div>
    </div>
    <p class="section-sub">
      Completions carry no XP by themselves — each one opens the door to a
      verification, and the mentor decides what's earned.
    </p>

    <div class="thm-settings">
      <label class="field-label" for="thm-username">Public profile username</label>
      <div class="thm-username-row">
        <input type="text" id="thm-username" placeholder="your-thm-username"
               value="${escAttr(data.username || "")}">
        <button class="btn" id="thm-save-user">Save</button>
      </div>
      <p class="thm-note">
        Sync scrapes the public profile via TryHackMe's unofficial endpoints —
        there's no official personal API, so it can break without notice.
        Manual logging always works.
      </p>
    </div>

    <div class="today-block">
      <div class="block-title-row">
        <h2 class="block-title">Next up</h2>
        <div>
          ${data.recommendation_stale ? `<span class="chip static c-amber">stale — tree changed</span>` : ""}
          <button class="btn small" id="thm-refresh-rec">Refresh</button>
        </div>
      </div>
      <div id="thm-rec">${renderRecommendation(rec, data.direct_mode)}</div>
    </div>

    <div class="today-block">
      <h2 class="block-title">Completed rooms</h2>
      <div id="thm-completions">
        ${data.completions.length
          ? data.completions.map(completionRow).join("")
          : `<p class="empty-state">Nothing logged yet. Finish a room, write it up in Docs, log it here.</p>`}
      </div>
    </div>`;

  el("thm-save-user").addEventListener("click", async () => {
    const username = el("thm-username").value.trim();
    await API.patch("/thm/settings", { username });
    toast(username ? `Username saved: ${username}` : "Username cleared", "info");
    renderThm();
  });

  el("thm-sync").addEventListener("click", async () => {
    el("thm-sync").disabled = true;
    el("thm-sync").textContent = "Syncing…";
    try {
      const res = await API.post("/thm/sync", {});
      if (!res.synced) toast(res.note, "error", 7000);
      else toast(res.added
        ? `Pulled ${res.added} new completion${res.added > 1 ? "s" : ""} (${res.total_on_profile} on profile)`
        : `Up to date — ${res.total_on_profile} rooms on profile`, "info", 5000);
    } catch (e) { /* toasted */ }
    renderThm();
  });

  el("thm-log").addEventListener("click", () => openLogCompletion());

  el("thm-refresh-rec").addEventListener("click", async () => {
    const box = el("thm-rec");
    box.innerHTML = `<p class="empty-state">Mentor is reading your tree…</p>`;
    try {
      await API.post("/thm/recommend", {});
      renderThm(); // full re-render also clears the stale chip
    } catch (e) {
      box.innerHTML = renderRecommendation(null, data.direct_mode);
    }
  });

  wireRecButtons();
  wireCompletionRows();
}

function renderRecommendation(rec, directMode) {
  if (!rec) {
    return directMode
      ? `<p class="empty-state">No recommendation yet — hit Refresh and the mentor
         will target your weakest branch.</p>`
      : `<p class="empty-state">Queue mode: an external agent can
         <code>GET /api/thm/recommend</code> for context and POST one back.</p>`;
  }
  return `
    ${rec.summary ? `<p class="thm-rec-summary">${esc(rec.summary)}</p>` : ""}
    ${(rec.recommendations || []).map((r) => `
      <div class="thm-rec">
        <div class="thm-rec-head">
          <a href="https://tryhackme.com/room/${encodeURIComponent(r.room_code)}"
             target="_blank" rel="noopener" class="thm-rec-title">${esc(r.room_title || r.room_code)}</a>
          <span class="card-meta mono">${esc(r.room_code)}</span>
        </div>
        <p class="thm-rec-reason">${esc(r.reason || "")}</p>
        <div class="thm-rec-foot">
          <span class="card-meta">${nodeNames(r.node_ids)}</span>
          <button class="btn small" data-log-room="${escAttr(r.room_code)}"
                  data-log-title="${escAttr(r.room_title || "")}">Done it — log</button>
        </div>
      </div>`).join("")}
    ${rec.generated_at ? `<p class="card-meta">Generated ${fmtDate(rec.generated_at.slice(0, 10))} · ${esc(rec.source || "")}</p>` : ""}`;
}

function nodeNames(ids) {
  if (!ids || !ids.length || !thmTreeNodes) return "";
  const names = ids
    .map((id) => thmTreeNodes.find((n) => n.id === id)?.title)
    .filter(Boolean);
  return names.length ? `feeds: ${names.map(esc).join(", ")}` : "";
}

// The tree is a few hundred nodes deep now, so a flat select is unusable.
// Group by domain, sort inside each, and drop anything already mapped to
// this room.
function nodeOptions(alreadyMapped) {
  const taken = new Set((alreadyMapped || []).map((n) => n.id));
  const byDomain = {};
  thmTreeNodes
    .filter((n) => !taken.has(n.id))
    .forEach((n) => (byDomain[n.domain] ||= []).push(n));

  return Object.keys(byDomain).sort().map((domain) => `
    <optgroup label="${escAttr(domain)}">
      ${byDomain[domain]
        .sort((a, b) => a.tier - b.tier || a.title.localeCompare(b.title))
        .map((n) => `<option value="${n.id}">${esc(n.title)} · t${n.tier} · lv ${n.level}/${n.max_level}</option>`)
        .join("")}
    </optgroup>`).join("");
}

// TryHackMe difficulty strings are freeform-ish; map the known ones onto a
// colour and leave anything else neutral.
function difficultyClass(d) {
  const k = (d || "").toLowerCase();
  return ["easy", "medium", "hard", "insane"].includes(k) ? `d-${k}` : "c-gray";
}

function completionRow(c) {
  const nodes = (c.nodes || []).map((n) => {
    let action;
    if (n.verified) {
      action = `<span class="chip static c-teal">verified</span>`;
    } else if (n.pending_attempt) {
      action = `<button class="btn small" data-resume="${n.pending_attempt}">Resume</button>`;
    } else if (n.level >= n.max_level) {
      action = `<span class="chip static c-gray">maxed</span>`;
    } else {
      action = `<button class="btn small primary" data-verify="${n.id}"
                        data-room="${escAttr(c.room_code)}">Prove it</button>`;
    }
    return `<div class="thm-node-row">
      <span class="thm-node-name">${esc(n.title)}
        <span class="card-meta">lv ${n.level}/${n.max_level}</span></span>
      <span class="thm-node-actions">
        ${action}
        <button class="icon-btn subtle" data-unmap-room="${escAttr(c.room_code)}"
                data-unmap-node="${n.id}" title="Unmap">×</button>
      </span>
    </div>`;
  }).join("");

  return `
    <div class="thm-completion" data-code="${escAttr(c.room_code)}">
      <div class="thm-completion-head">
        <div>
          <a href="https://tryhackme.com/room/${encodeURIComponent(c.room_code)}"
             target="_blank" rel="noopener" class="thm-room-title">${esc(c.title || c.room_code)}</a>
          <span class="card-meta mono">${esc(c.room_code)}</span>
          ${c.difficulty ? `<span class="chip static ${difficultyClass(c.difficulty)}">${esc(c.difficulty)}</span>` : ""}
          <span class="chip static ${c.source === "sync" ? "c-blue" : "c-gray"}">${esc(c.source)}</span>
        </div>
        <div>
          <span class="card-meta">${fmtDate(c.local_date)}</span>
          <button class="icon-btn subtle" data-del-completion="${c.id}" title="Remove">×</button>
        </div>
      </div>
      ${(c.tags || []).length ? `<div class="thm-tags">
        ${c.tags.slice(0, 8).map((t) => `<span class="chip static c-gray">${esc(t)}</span>`).join("")}
      </div>` : ""}
      <div class="thm-nodes">
        ${nodes || `<p class="card-meta">Not mapped to any node yet — map it so the room can count for something.</p>`}
        <div class="thm-map-row">
          <select data-map-select="${escAttr(c.room_code)}">
            <option value="">Map to node…</option>
            ${nodeOptions(c.nodes)}
          </select>
          <button class="btn small" data-map-room="${escAttr(c.room_code)}">Map</button>
        </div>
      </div>
    </div>`;
}

function wireRecButtons() {
  document.querySelectorAll("[data-log-room]").forEach((btn) => {
    btn.addEventListener("click", () =>
      openLogCompletion(btn.dataset.logRoom, btn.dataset.logTitle));
  });
}

function wireCompletionRows() {
  document.querySelectorAll("[data-verify]").forEach((btn) => {
    btn.addEventListener("click", () =>
      startLevelUp(Number(btn.dataset.verify), { roomCode: btn.dataset.room }));
  });
  document.querySelectorAll("[data-resume]").forEach((btn) => {
    btn.addEventListener("click", () => openAttempt(Number(btn.dataset.resume)));
  });
  document.querySelectorAll("[data-del-completion]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await API.del(`/thm/completions/${btn.dataset.delCompletion}`);
      renderThm();
    });
  });
  document.querySelectorAll("[data-map-room]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const code = btn.dataset.mapRoom;
      const select = document.querySelector(`[data-map-select="${CSS.escape(code)}"]`);
      if (!select.value) { toast("Pick a node", "error"); return; }
      await API.post(`/thm/rooms/${encodeURIComponent(code)}/nodes`,
        { node_id: Number(select.value) });
      renderThm();
    });
  });
  document.querySelectorAll("[data-unmap-room]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await API.request("DELETE",
        `/thm/rooms/${encodeURIComponent(btn.dataset.unmapRoom)}/nodes`,
        { node_id: Number(btn.dataset.unmapNode) });
      renderThm();
    });
  });
}

function openLogCompletion(code = "", title = "") {
  openModal(`
    <div class="modal-head">
      <h2 class="modal-title">Log a completed room</h2>
      <button class="icon-btn" onclick="closeModal()">×</button>
    </div>

    <label class="field-label">Room code or URL</label>
    <input type="text" id="thm-code" placeholder="vulnversity or https://tryhackme.com/room/vulnversity"
           value="${escAttr(code)}">

    <label class="field-label">Title (optional — fetched if the endpoints cooperate)</label>
    <input type="text" id="thm-title" value="${escAttr(title)}">

    <label class="field-label">Completed on</label>
    <input type="date" id="thm-date" value="${todayISO()}">

    <p class="notes-gate-hint">
      Logging is step one of three: write the room up in Docs, then map it to
      a node and prove it. The completion itself grants nothing.
    </p>

    <div class="modal-actions">
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn primary" id="thm-submit">Log it</button>
    </div>
  `, () => {
    el("thm-submit").addEventListener("click", async () => {
      const roomCode = el("thm-code").value.trim();
      if (!roomCode) { toast("Room code is required", "error"); return; }
      try {
        await API.post("/thm/completions", {
          room_code: roomCode,
          title: el("thm-title").value.trim(),
          date: el("thm-date").value,
        });
      } catch (e) { return; }
      closeModal();
      renderThm();
    });
  });
}
