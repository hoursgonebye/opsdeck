// Daily routines grouped by time of day, with streaks and a history strip.
//
// Nothing actually "resets" at midnight - completions are stored per local
// date, so a new day simply has no rows yet and history stays queryable.
// That's a deliberate choice over a destructive nightly job.

const TIME_GROUPS = ["morning", "afternoon", "evening", "anytime"];

async function renderRoutines() {
  const panel = el("panel");
  panel.innerHTML = `<div class="loading">Loading…</div>`;

  const [data, history] = await Promise.all([
    API.get(`/routines?date=${todayISO()}`),
    API.get("/routines/history?days=30"),
  ]);

  const byGroup = {};
  data.routines.forEach((r) => (byGroup[r.time_group] ||= []).push(r));

  const groupsHtml = TIME_GROUPS.filter((g) => byGroup[g]).map((g) => {
    const done = byGroup[g].filter((r) => r.done_today).length;
    return `
      <div class="routine-block">
        <div class="block-title-row">
          <h2 class="block-title">${esc(g)}</h2>
          <span class="count">${done}/${byGroup[g].length}</span>
        </div>
        ${byGroup[g].map((r) => `
          <div class="routine-row ${r.done_today ? "done" : ""}" data-routine="${r.id}">
            <span class="check"></span>
            <span class="routine-name">${esc(r.name)}</span>
            ${r.notes ? `<span class="today-meta">${esc(r.notes)}</span>` : ""}
            ${r.streak > 0 ? `<span class="streak" title="${r.streak} day streak">${r.streak}d</span>` : ""}
            <button class="icon-btn" data-action="edit-routine" data-routine="${r.id}">✎</button>
          </div>`).join("")}
      </div>`;
  }).join("");

  // History strip: one square per day, opacity by completion ratio.
  const histMap = {};
  history.days.forEach((d) => (histMap[d.local_date] = d.done));
  let strip = "";
  for (let i = 29; i >= 0; i--) {
    const date = addDaysISO(todayISO(), -i);
    const done = histMap[date] || 0;
    const ratio = history.total_routines ? done / history.total_routines : 0;
    const level = ratio === 0 ? 0 : ratio < 0.34 ? 1 : ratio < 0.67 ? 2 : ratio < 1 ? 3 : 4;
    strip += `<span class="heat lvl-${level}" title="${date}: ${done}/${history.total_routines}"></span>`;
  }

  panel.innerHTML = `
    <div class="section-head-row">
      <h1 class="section-title">Routines</h1>
      <div class="head-actions">
        <button class="btn primary" id="new-routine">+ Routine</button>
      </div>
    </div>
    <p class="section-sub">${fmtDateLong(data.date)} — resets each night at midnight (${window.OPSDECK.tz})</p>

    ${groupsHtml || '<p class="empty-state">No routines yet. Add one to get started.</p>'}

    <div class="routine-block">
      <h2 class="block-title">Last 30 days</h2>
      <div class="heat-strip">${strip}</div>
    </div>`;

  panel.querySelectorAll(".routine-row").forEach((row) => {
    row.addEventListener("click", async (e) => {
      if (e.target.closest("[data-action]")) return;
      await API.post(`/routines/${row.dataset.routine}/toggle`, { date: todayISO() });
      renderRoutines();
    });
  });

  panel.querySelectorAll('[data-action="edit-routine"]').forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const r = data.routines.find((x) => x.id === Number(btn.dataset.routine));
      openRoutineModal(r);
    });
  });

  el("new-routine").addEventListener("click", () => openRoutineModal(null));
}

function openRoutineModal(routine) {
  const r = routine || { name: "", time_group: "morning", notes: "" };

  openModal(`
    <div class="modal-head">
      <h2 class="modal-title">${routine ? "Edit routine" : "New routine"}</h2>
      <button class="icon-btn" onclick="closeModal()">×</button>
    </div>

    <label class="field-label">Name</label>
    <input type="text" id="rt-name" value="${escAttr(r.name)}" placeholder="e.g. 30 min of NCL practice">

    <label class="field-label">Time of day</label>
    <select id="rt-group">
      ${TIME_GROUPS.map((g) => `<option value="${g}" ${r.time_group === g ? "selected" : ""}>${g}</option>`).join("")}
    </select>

    <label class="field-label">Notes</label>
    <input type="text" id="rt-notes" value="${escAttr(r.notes)}" placeholder="optional">

    <div class="modal-actions">
      ${routine ? `<button class="btn danger" id="delete-routine">Delete</button>` : ""}
      <button class="btn primary" id="save-routine">Save</button>
    </div>
  `, () => {
    if (routine) {
      el("delete-routine").addEventListener("click", async () => {
        if (!confirm("Delete this routine and its history?")) return;
        await API.del(`/routines/${routine.id}`);
        closeModal();
        renderRoutines();
      });
    }
    el("save-routine").addEventListener("click", async () => {
      const payload = {
        name: el("rt-name").value || "Untitled",
        time_group: el("rt-group").value,
        notes: el("rt-notes").value,
      };
      if (routine) await API.patch(`/routines/${routine.id}`, payload);
      else await API.post("/routines", payload);
      closeModal();
      renderRoutines();
    });
  });
}
