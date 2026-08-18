// Academics: the transcript, and the one number the transfer plan turns on.
//
// Four views behind one section. Overview answers "where am I and what do I
// need"; Terms is the transcript itself, editable in place; Import takes a
// pasted transcript; Scale is the grade table every number depends on.
//
// Nothing here computes a GPA. The server derives every figure from the
// course rows and the scale, so the UI can never disagree with the API - it
// re-fetches instead of patching numbers locally.

let acView = "overview";        // overview | terms | import | scale
let acData = null;              // last /academics payload
let acOpenTerms = new Set();    // which terms are expanded
let acPreview = null;           // last import preview
let acImportText = "";

const AC_STATUS_LABEL = {
  completed: "completed", in_progress: "in progress", planned: "planned",
};

// A GPA reads best at two decimals, but the difference between 3.395 and
// 3.400 is the whole ballgame next to a 3.4 floor - so the exact value the
// server computed is always one hover away.
function gpaText(v) {
  return v === null || v === undefined ? "—" : v.toFixed(2);
}
function gpaCell(v) {
  if (v === null || v === undefined) return `<span class="ac-gpa">—</span>`;
  return `<span class="ac-gpa" title="${v.toFixed(3)}">${v.toFixed(2)}</span>`;
}
function creditText(v) {
  if (v === null || v === undefined) return "—";
  return Number.isInteger(v) ? String(v) : String(Math.round(v * 100) / 100);
}

function acScale() { return acData?.scale || []; }

// Grades that can be assigned. The blank option is meaningful: it is what
// "no grade yet" looks like, and it is what makes a course count as
// remaining rather than finished.
function gradeOptions(selected, blankLabel = "—") {
  const opts = [`<option value=""${!selected ? " selected" : ""}>${blankLabel}</option>`];
  for (const g of acScale()) {
    const sel = g.grade === selected ? " selected" : "";
    opts.push(`<option value="${escAttr(g.grade)}"${sel}>${esc(g.grade)}</option>`);
  }
  // A grade already on a course but missing from the scale must stay
  // selectable, or opening the dropdown would silently erase it.
  if (selected && !acScale().some((g) => g.grade === selected)) {
    opts.push(`<option value="${escAttr(selected)}" selected>${esc(selected)} (unknown)</option>`);
  }
  return opts.join("");
}

// Every graded attempt, as candidates for "this course is a retake of…".
// Only graded courses are offered: an ungraded one has nothing to replace.
function replaceOptions(selected, selfId) {
  const opts = [`<option value=""${!selected ? " selected" : ""}>— not a retake —</option>`];
  for (const t of acData.terms || []) {
    for (const c of t.courses) {
      if (c.id === selfId || !c.grade) continue;
      const sel = c.id === selected ? " selected" : "";
      opts.push(`<option value="${c.id}"${sel}>${esc(c.code)} · ${esc(t.name)} · ${esc(c.grade)}</option>`);
    }
  }
  return opts.join("");
}

async function renderAcademics() {
  const panel = el("panel");
  panel.innerHTML = `<div class="loading">Loading…</div>`;

  try {
    acData = await API.get("/academics");
  } catch (e) {
    panel.innerHTML = `<p class="empty-state">Could not load academics.</p>`;
    return;
  }

  const views = [["overview", "Overview"], ["terms", "Transcript"],
                 ["import", "Import"], ["scale", "Grade scale"]];

  panel.innerHTML = `
    <div class="section-head-row">
      <h1 class="section-title">Academics</h1>
      <div class="head-actions">
        ${gpaCell(acData.cumulative.gpa)}
        <span class="ac-head-label">cumulative</span>
      </div>
    </div>

    <div class="h-controls">
      <div class="h-views">
        ${views.map(([v, l]) =>
          `<button class="board-tab ${acView === v ? "active" : ""}" data-acview="${v}">${l}</button>`
        ).join("")}
      </div>
    </div>

    ${scaleWarning()}
    <div id="ac-body"></div>`;

  panel.querySelectorAll("[data-acview]").forEach((b) =>
    b.addEventListener("click", () => { acView = b.dataset.acview; renderAcademics(); }));

  const bodyEl = el("ac-body");
  if (acView === "terms") acTerms(bodyEl);
  else if (acView === "import") acImport(bodyEl);
  else if (acView === "scale") acScaleView(bodyEl);
  else acOverview(bodyEl);
}

// The scale is the one input that can corrupt every number in the section
// while still looking plausible, so an unconfirmed value that is actually
// load-bearing gets said out loud rather than buried in a settings tab.
function scaleWarning() {
  const unknown = acData.unknown_grades_in_use || [];
  const unverified = acData.unverified_grades_in_use || [];
  if (!unknown.length && !unverified.length) return "";

  const parts = [];
  if (unknown.length) {
    parts.push(`<strong>${unknown.map(esc).join(", ")}</strong> ${unknown.length > 1 ? "are" : "is"}
      not in your grade scale, so ${unknown.length > 1 ? "those courses count" : "that course counts"}
      as attempted only — no points either way.`);
  }
  if (unverified.length) {
    parts.push(`<strong>${unverified.map(esc).join(", ")}</strong> ${unverified.length > 1 ? "are" : "is"}
      in use but unconfirmed. Check the value against the college catalog.`);
  }
  return `<div class="ac-warn">${parts.join(" ")}
    <button class="btn tiny" data-acview="scale" id="ac-warn-scale">Open grade scale</button></div>`;
}

// ------------------------------------------------------------- overview

function acOverview(body) {
  const c = acData.cumulative;
  const p = acData.projected;
  const hasProjection = p.gpa !== null && p.gpa !== c.gpa;

  const tiles = [
    ["Cumulative GPA", gpaText(c.gpa), `${creditText(c.gpa_units)} GPA credits`],
    ["Credits earned", creditText(c.earned), `${creditText(c.attempted)} attempted`],
    ["Quality points", creditText(c.points), "credits × grade points"],
    ["Projected GPA", gpaText(p.gpa),
     hasProjection ? "with your expected grades" : "same as current"],
  ].map(([label, value, sub]) => `
    <div class="joint-card ac-tile">
      <div class="h-label">${esc(label)}</div>
      <div class="h-value">${esc(value)}</div>
      <div class="h-sub">${esc(sub)}</div>
    </div>`).join("");

  body.innerHTML = `
    <div class="h-tiles">${tiles}</div>
    ${repeatsNote()}

    <div class="block-title-row"><h2 class="block-title">Targets</h2></div>
    <div class="ac-goals">${(acData.goals || []).map(goalCard).join("")
      || `<p class="empty-state">No targets yet.</p>`}</div>
    <div class="field-row-inline">
      <button class="btn" id="ac-add-goal">Add a target</button>
    </div>

    ${trendBlock()}

    <div class="joint-card ac-whatif">
      <div class="block-title">What if…</div>
      <p class="settings-hint">Ask the transcript a question directly: how a block
      of credits at some average would move the cumulative GPA, and what that
      block would have to average to clear a target.</p>
      <div class="field-row-inline">
        <label class="field-label">credits</label>
        <input type="number" id="ac-wi-credits" step="0.5" min="0"
               value="${acData.cumulative.attempted !== null ? defaultWhatIfCredits() : 15}">
        <label class="field-label">at GPA</label>
        <input type="number" id="ac-wi-at" step="0.1" min="0" max="4" value="3.5">
        <label class="field-label">target</label>
        <input type="number" id="ac-wi-target" step="0.05" min="0" max="4" value="3.4">
        <button class="btn primary" id="ac-wi-go">Calculate</button>
      </div>
      <div id="ac-wi-out" class="ac-wi-out"></div>
    </div>`;

  body.querySelectorAll(".ac-goal-edit").forEach((b) =>
    b.addEventListener("click", () => goalModal(Number(b.dataset.goal))));
  el("ac-add-goal")?.addEventListener("click", () => goalModal(null));
  el("ac-wi-go")?.addEventListener("click", runWhatIf);
}

// Once a repeat is in play the term GPAs above stop adding up to the
// cumulative below them, because a term keeps the average it actually had at
// the time. Say why, rather than letting it look like an arithmetic bug.
function repeatsNote() {
  const r = acData.repeats || {};
  const pending = r.pending || [];
  const applied = (r.applied || []).length;
  if (!pending.length && !applied) return "";

  const parts = [];
  if (pending.length) {
    parts.push(`A retake is scheduled for
      <strong>${pending.map((c) => `${esc(c.code)} (${esc(c.grade)})`).join(", ")}</strong>.
      Those grades still count today — the registrar has not replaced them yet — but
      the targets below already account for them dropping out once the retake is
      graded, because finishing it both adds credits and removes an old grade.`);
  }
  if (applied) {
    parts.push(`${applied} earlier attempt${applied > 1 ? "s have" : " has"} been
      retired by a completed retake. Term GPAs stay as they were at the time;
      only the cumulative reflects the replacement.`);
  }
  return `<div class="ac-note">${parts.join(" ")}</div>`;
}

// Default the what-if to the credits already scheduled but ungraded - the
// question is nearly always "this term", and pre-filling it means the common
// case needs one click, not three.
function defaultWhatIfCredits() {
  let remaining = 0;
  for (const t of acData.terms || []) {
    for (const c of t.courses) {
      if (!c.grade && !c.exclude_from_gpa) remaining += c.credits;
    }
  }
  return remaining || 15;
}

function goalCard(g) {
  const met = g.met;
  // A goal whose scope contains nothing yet is not failing, it is empty.
  // Rendering it as "below target" would be the section's first lie.
  const empty = !g.gpa_units && !g.remaining_units;
  const state = empty ? "empty" : met ? "met" : g.projected_met ? "projected" : "short";
  const stateLabel = empty ? "not started"
    : met ? "on target"
    : g.projected_met ? "on target if projections hold" : "below target";

  // The sentence that matters. Everything else on the card is context for it.
  let verdict;
  if (empty) {
    verdict = g.scope_tag
      ? `Nothing is tagged <strong>${esc(g.scope_tag)}</strong> yet, so there is
         nothing to average. Tag the courses this target covers — edit a course
         and add the tag — and it starts tracking itself.`
      : `No graded coursework on record yet.`;
  } else if (met) {
    verdict = `Currently <strong>${gpaText(g.current_gpa)}</strong>, above the
      ${g.target_gpa} floor. Holding it is the job.`;
  } else if (g.needed_gpa === null) {
    verdict = `No ungraded credits left in this scope, so the ${g.target_gpa}
      is settled at <strong>${gpaText(g.current_gpa)}</strong>.`;
  } else if (g.reachable_in_scheduled) {
    verdict = `Needs a <strong>${gpaText(g.needed_gpa)}</strong> average across the
      ${creditText(g.remaining_units)} credits already scheduled.`;
  } else {
    const extra = g.extra_credits_needed;
    verdict = `Out of reach inside the ${creditText(g.remaining_units)} credits
      scheduled — that would take a ${gpaText(g.needed_gpa)} average, above the
      ${g.best_grade_points} your scale allows.` +
      (extra === null
        ? ` The target is at or above the highest grade available, so no amount of
            coursework reaches it.`
        : ` About <strong>${creditText(extra)} credits</strong> of straight
            ${topGrade()} would.`);
  }

  return `
    <div class="joint-card ac-goal ${state}">
      <div class="ac-goal-head">
        <span class="ac-goal-name">${esc(g.name)}</span>
        <span class="chip ${met ? "on c-green" : ""}">${esc(stateLabel)}</span>
        <button class="btn tiny ac-goal-edit" data-goal="${g.id}">Edit</button>
      </div>
      <div class="ac-goal-nums">
        <span>now ${gpaCell(g.current_gpa)}</span>
        <span>projected ${gpaCell(g.projected_gpa)}</span>
        <span>target <span class="ac-gpa">${g.target_gpa.toFixed(2)}</span></span>
        ${g.scope_tag ? `<span class="tag">${esc(g.scope_tag)} only</span>` : ""}
      </div>
      ${gpaBar(g)}
      <div class="ac-goal-verdict">${verdict}</div>
      ${g.note ? `<div class="ac-goal-note">${esc(g.note)}</div>` : ""}
    </div>`;
}

function topGrade() {
  const best = acScale().filter((g) => g.counts_gpa && g.points !== null)
    .sort((a, b) => b.points - a.points)[0];
  return best ? best.grade : "A";
}

// A bar scaled 2.0–4.0 rather than 0–4.0: nothing in this plan is decided
// below a 2.0, and the full range compresses the part that matters into a
// sliver where a 3.24 and a 3.40 look identical.
function gpaBar(g) {
  const lo = 2.0, hi = 4.0;
  const pct = (v) => v === null || v === undefined
    ? null : Math.max(0, Math.min(100, ((v - lo) / (hi - lo)) * 100));
  const cur = pct(g.current_gpa);
  const proj = pct(g.projected_gpa);
  const tgt = pct(g.target_gpa);
  return `
    <div class="ac-bar" title="scale ${lo.toFixed(1)}–${hi.toFixed(1)}">
      ${proj !== null ? `<div class="ac-bar-proj" style="width:${proj}%"></div>` : ""}
      ${cur !== null ? `<div class="ac-bar-fill" style="width:${cur}%"></div>` : ""}
      ${tgt !== null ? `<div class="ac-bar-target" style="left:${tgt}%"></div>` : ""}
    </div>`;
}

function trendBlock() {
  const terms = (acData.terms || []).filter((t) => t.totals.gpa !== null);
  if (terms.length < 2) return "";
  const rows = terms.map((t) => {
    const w = Math.max(2, Math.min(100, ((t.totals.gpa - 2.0) / 2.0) * 100));
    return `
      <div class="ac-trend-row">
        <span class="ac-trend-name">${esc(t.name)}</span>
        <span class="ac-trend-bar"><span style="width:${w}%"></span></span>
        <span class="ac-trend-val">${gpaText(t.totals.gpa)}</span>
        <span class="ac-trend-cum">cum ${gpaText(t.cumulative_gpa)}</span>
      </div>`;
  }).join("");
  return `
    <div class="block-title-row"><h2 class="block-title">Term by term</h2></div>
    <div class="joint-card ac-trend">${rows}
      <p class="settings-hint">Bars are term GPA on a 2.0–4.0 scale; the right-hand
      figure is the cumulative average as of the end of that term.</p></div>`;
}

async function runWhatIf() {
  const credits = Number(el("ac-wi-credits").value) || 0;
  const at = Number(el("ac-wi-at").value);
  const target = Number(el("ac-wi-target").value);
  const out = el("ac-wi-out");
  out.innerHTML = `<div class="loading">Calculating…</div>`;

  let r;
  try {
    r = await API.get(`/academics/forecast?credits=${credits}&at=${at}&target=${target}`);
  } catch (e) { out.innerHTML = ""; return; }

  const reachable = r.needed_gpa !== null && r.needed_gpa <= r.best_grade_points;
  out.innerHTML = `
    <div class="ac-wi-line">
      ${creditText(r.credits)} more credits at a ${gpaText(at)} average lands you at
      <strong>${gpaText(r.resulting_gpa ?? null)}</strong>
      (from ${gpaText(r.current_gpa)}).
    </div>
    <div class="ac-wi-line">
      To reach <strong>${gpaText(r.target)}</strong> over those credits you would need a
      <strong>${gpaText(r.needed_gpa)}</strong> average —
      ${reachable ? "possible" : `impossible, your scale tops out at ${r.best_grade_points}`}.
    </div>
    <div class="ac-wi-line muted">
      Best case over ${creditText(r.credits)} credits: ${gpaText(r.max_possible_gpa)}.
      ${r.extra_credits_needed === null
        ? ""
        : `Reaching ${gpaText(r.target)} at straight ${topGrade()} takes about
           ${creditText(r.extra_credits_needed)} credits.`}
    </div>`;
}

// ---------------------------------------------------------------- terms

function acTerms(body) {
  const terms = acData.terms || [];
  if (!terms.length) {
    body.innerHTML = `
      <p class="empty-state">No terms yet. Paste your transcript under Import,
      or add a term by hand.</p>
      <div class="field-row-inline"><button class="btn primary" id="ac-add-term">Add a term</button></div>`;
    el("ac-add-term")?.addEventListener("click", () => termModal(null));
    return;
  }

  body.innerHTML = `
    <div class="field-row-inline">
      <button class="btn" id="ac-add-term">Add a term</button>
      <span class="settings-hint">Grades save as you change them; every GPA
      re-derives from the server.</span>
    </div>
    <div class="ac-terms">${terms.map(termBlock).join("")}</div>`;

  el("ac-add-term")?.addEventListener("click", () => termModal(null));

  body.querySelectorAll(".ac-term-head").forEach((h) =>
    h.addEventListener("click", (e) => {
      if (e.target.closest("button")) return;
      const id = Number(h.dataset.term);
      acOpenTerms.has(id) ? acOpenTerms.delete(id) : acOpenTerms.add(id);
      acTerms(body);
    }));

  body.querySelectorAll(".ac-term-edit").forEach((b) =>
    b.addEventListener("click", () => termModal(Number(b.dataset.term))));
  body.querySelectorAll(".ac-term-del").forEach((b) =>
    b.addEventListener("click", () => deleteTerm(Number(b.dataset.term))));
  body.querySelectorAll(".ac-add-course").forEach((b) =>
    b.addEventListener("click", () => courseModal(Number(b.dataset.term), null)));
  body.querySelectorAll(".ac-course-edit").forEach((b) =>
    b.addEventListener("click", () => courseModal(null, Number(b.dataset.course))));
  body.querySelectorAll(".ac-course-del").forEach((b) =>
    b.addEventListener("click", () => deleteCourse(Number(b.dataset.course))));

  // Inline grade edits are the whole point of this view: entering a final
  // grade is the single most common action, so it costs one click here
  // rather than opening a dialog.
  body.querySelectorAll("[data-grade-for]").forEach((sel) =>
    sel.addEventListener("change", async () => {
      const field = sel.dataset.field;
      await API.patch(`/academics/courses/${sel.dataset.gradeFor}`, { [field]: sel.value });
      await renderAcademics();
    }));
}

function termBlock(t) {
  const open = acOpenTerms.has(t.id);
  const hasProjection = t.projected_totals.gpa !== null
    && t.projected_totals.gpa !== t.totals.gpa;

  const rows = t.courses.map((c) => `
    <tr class="${c.superseded ? "ac-replaced" : ""}">
      <td class="mono">${esc(c.code)}</td>
      <td>${esc(c.title)}${c.exclude_from_gpa
        ? ` <span class="tag">excluded</span>` : ""}${
        c.superseded ? ` <span class="tag t-red">replaced by retake</span>` : ""}${
        c.superseded_pending ? ` <span class="tag t-amber">retake scheduled</span>` : ""}${
        c.replaces ? ` <span class="tag t-blue">retake of ${esc(c.replaces.grade)}</span>` : ""}${
        (c.tags || []).map((tag) => ` <span class="tag">${esc(tag)}</span>`).join("")}</td>
      <td class="ac-num">${creditText(c.credits)}</td>
      <td>
        <select data-grade-for="${c.id}" data-field="grade" class="ac-grade-sel">
          ${gradeOptions(c.grade)}
        </select>
      </td>
      <td>
        ${c.grade ? `<span class="ac-dim">—</span>` : `
          <select data-grade-for="${c.id}" data-field="projected_grade" class="ac-grade-sel proj">
            ${gradeOptions(c.projected_grade, "expect?")}
          </select>`}
      </td>
      <td class="ac-row-actions">
        <button class="btn tiny ac-course-edit" data-course="${c.id}">Edit</button>
        <button class="btn tiny ac-course-del" data-course="${c.id}">✕</button>
      </td>
    </tr>`).join("");

  return `
    <div class="joint-card ac-term">
      <div class="ac-term-head" data-term="${t.id}">
        <span class="ac-term-toggle">${open ? "▾" : "▸"}</span>
        <span class="ac-term-name">${esc(t.name)}</span>
        <span class="chip">${esc(AC_STATUS_LABEL[t.status] || t.status)}</span>
        <span class="ac-term-gpa">
          term ${gpaCell(t.totals.gpa)}
          ${hasProjection ? `<span class="ac-dim">→ ${gpaText(t.projected_totals.gpa)} projected</span>` : ""}
        </span>
        <span class="ac-term-meta">${creditText(t.totals.earned)}/${creditText(t.totals.attempted)} cr
          · cum ${gpaText(t.cumulative_gpa)}</span>
        <span class="ac-term-btns">
          <button class="btn tiny ac-term-edit" data-term="${t.id}">Edit</button>
          <button class="btn tiny ac-term-del" data-term="${t.id}">Delete</button>
        </span>
      </div>
      ${open ? `
        <table class="ac-table">
          <thead><tr>
            <th>Code</th><th>Title</th><th class="ac-num">Cr</th>
            <th>Grade</th><th>Expected</th><th></th>
          </tr></thead>
          <tbody>${rows || `<tr><td colspan="6" class="ac-dim">No courses.</td></tr>`}</tbody>
        </table>
        <div class="field-row-inline">
          <button class="btn tiny ac-add-course" data-term="${t.id}">Add course</button>
        </div>` : ""}
    </div>`;
}

async function deleteTerm(id) {
  const t = (acData.terms || []).find((x) => x.id === id);
  if (!confirm(`Delete ${t ? t.name : "this term"} and its ${t ? t.courses.length : 0} courses?`)) return;
  await API.del(`/academics/terms/${id}`);
  toast("Term deleted", "info");
  renderAcademics();
}

async function deleteCourse(id) {
  if (!confirm("Delete this course?")) return;
  await API.del(`/academics/courses/${id}`);
  renderAcademics();
}

function termModal(id) {
  const t = id ? (acData.terms || []).find((x) => x.id === id) : null;
  openModal(`
    <div class="modal-head"><h2 class="modal-title">${t ? "Edit term" : "New term"}</h2></div>
    <div class="field-row">
      <label class="field-label">Name</label>
      <input type="text" id="ac-t-name" placeholder="Fall 2026"
             value="${escAttr(t?.name || "")}">
    </div>
    <div class="field-row">
      <label class="field-label">Status</label>
      <select id="ac-t-status">
        ${["completed", "in_progress", "planned"].map((s) =>
          `<option value="${s}"${t?.status === s ? " selected" : ""}>${AC_STATUS_LABEL[s]}</option>`
        ).join("")}
      </select>
    </div>
    <div class="field-row">
      <label class="field-label">Institution</label>
      <input type="text" id="ac-t-inst" value="${escAttr(t?.institution || "")}"
             placeholder="Your college">
    </div>
    <p class="settings-hint">Season and year are read from the name, so
    “Spring 2027” sorts itself into the right place.</p>
    <div class="modal-actions">
      <button class="btn" id="ac-t-cancel">Cancel</button>
      <button class="btn primary" id="ac-t-save">Save</button>
    </div>`, () => {
    el("ac-t-cancel").addEventListener("click", closeModal);
    el("ac-t-save").addEventListener("click", async () => {
      const payload = {
        name: el("ac-t-name").value.trim(),
        status: el("ac-t-status").value,
        institution: el("ac-t-inst").value.trim(),
      };
      if (!payload.name) { toast("Name required", "error"); return; }
      try {
        if (t) await API.patch(`/academics/terms/${t.id}`, payload);
        else {
          const created = await API.post("/academics/terms", payload);
          acOpenTerms.add(created.id);
        }
      } catch (e) { return; }
      closeModal();
      renderAcademics();
    });
  });
}

function courseModal(termId, courseId) {
  let course = null, term = null;
  for (const t of acData.terms || []) {
    if (t.id === termId) term = t;
    const hit = t.courses.find((c) => c.id === courseId);
    if (hit) { course = hit; term = t; }
  }
  const tags = (course?.tags || []).join(", ");

  openModal(`
    <div class="modal-head">
      <h2 class="modal-title">${course ? "Edit course" : "New course"}</h2>
    </div>
    <div class="field-row">
      <label class="field-label">Code</label>
      <input type="text" id="ac-c-code" placeholder="MTH 141" value="${escAttr(course?.code || "")}">
    </div>
    <div class="field-row">
      <label class="field-label">Title</label>
      <input type="text" id="ac-c-title" value="${escAttr(course?.title || "")}">
    </div>
    <div class="field-row-inline">
      <label class="field-label">Credits</label>
      <input type="number" id="ac-c-credits" step="0.5" min="0" max="24"
             value="${course?.credits ?? 3}">
      <label class="field-label">Grade</label>
      <select id="ac-c-grade">${gradeOptions(course?.grade || "")}</select>
      <label class="field-label">Expected</label>
      <select id="ac-c-proj">${gradeOptions(course?.projected_grade || "", "—")}</select>
    </div>
    <div class="field-row">
      <label class="field-label">Tags</label>
      <input type="text" id="ac-c-tags" value="${escAttr(tags)}"
             placeholder="ub-core, major">
    </div>
    <p class="settings-hint">Tags scope a target — “ub-core” on the four UB
    transfer courses makes a core-GPA goal work off exactly those.</p>
    <div class="field-row">
      <label class="field-label">Retake of</label>
      <select id="ac-c-replaces">
        ${replaceOptions(course?.replaces_course_id || null, course?.id ?? null)}
      </select>
    </div>
    <p class="settings-hint">Link a retake to the attempt it replaces and the
    old grade retires itself <em>the moment this one is graded</em> — not
    before, so today's GPA stays the one the registrar would give you. Only
    link them if your college replaces the original grade rather than
    averaging the two attempts.</p>
    <label class="toggle-row">
      <input type="checkbox" id="ac-c-excl" ${course?.exclude_from_gpa ? "checked" : ""}>
      Exclude from GPA (repeat replaced by a later attempt, etc.)
    </label>
    <div class="modal-actions">
      <button class="btn" id="ac-c-cancel">Cancel</button>
      <button class="btn primary" id="ac-c-save">Save</button>
    </div>`, () => {
    el("ac-c-cancel").addEventListener("click", closeModal);
    el("ac-c-save").addEventListener("click", async () => {
      const payload = {
        code: el("ac-c-code").value.trim(),
        title: el("ac-c-title").value.trim(),
        credits: Number(el("ac-c-credits").value),
        grade: el("ac-c-grade").value,
        projected_grade: el("ac-c-proj").value,
        tags: el("ac-c-tags").value.split(",").map((s) => s.trim()).filter(Boolean),
        exclude_from_gpa: el("ac-c-excl").checked,
        replaces_course_id: el("ac-c-replaces").value
          ? Number(el("ac-c-replaces").value) : null,
      };
      try {
        if (course) await API.patch(`/academics/courses/${course.id}`, payload);
        else await API.post("/academics/courses", { ...payload, term_id: term.id });
      } catch (e) { return; }
      closeModal();
      renderAcademics();
    });
  });
}

function goalModal(id) {
  const g = id ? (acData.goals || []).find((x) => x.id === id) : null;
  openModal(`
    <div class="modal-head"><h2 class="modal-title">${g ? "Edit target" : "New target"}</h2></div>
    <div class="field-row">
      <label class="field-label">Name</label>
      <input type="text" id="ac-g-name" value="${escAttr(g?.name || "")}" placeholder="Scholarship floor">
    </div>
    <div class="field-row-inline">
      <label class="field-label">Target GPA</label>
      <input type="number" id="ac-g-target" step="0.05" min="0" max="4"
             value="${g?.target_gpa ?? 3.4}">
      <label class="field-label">Scope tag</label>
      <input type="text" id="ac-g-tag" value="${escAttr(g?.scope_tag || "")}"
             placeholder="(blank = all courses)">
    </div>
    <div class="field-row">
      <label class="field-label">Note</label>
      <textarea id="ac-g-note" rows="2">${esc(g?.note || "")}</textarea>
    </div>
    <div class="modal-actions">
      ${g ? `<button class="btn" id="ac-g-del">Delete</button>` : ""}
      <button class="btn" id="ac-g-cancel">Cancel</button>
      <button class="btn primary" id="ac-g-save">Save</button>
    </div>`, () => {
    el("ac-g-cancel").addEventListener("click", closeModal);
    el("ac-g-del")?.addEventListener("click", async () => {
      if (!confirm(`Delete the “${g.name}” target?`)) return;
      await API.del(`/academics/goals/${g.id}`);
      closeModal();
      renderAcademics();
    });
    el("ac-g-save").addEventListener("click", async () => {
      const payload = {
        name: el("ac-g-name").value.trim(),
        target_gpa: Number(el("ac-g-target").value),
        scope_tag: el("ac-g-tag").value.trim(),
        note: el("ac-g-note").value.trim(),
      };
      if (!payload.name) { toast("Name required", "error"); return; }
      try {
        if (g) await API.patch(`/academics/goals/${g.id}`, payload);
        else await API.post("/academics/goals", payload);
      } catch (e) { return; }
      closeModal();
      renderAcademics();
    });
  });
}

// --------------------------------------------------------------- import

function acImport(body) {
  body.innerHTML = `
    <div class="joint-card">
      <div class="block-title">Paste a transcript</div>
      <p class="settings-hint">Copy the text out of your unofficial transcript
      (portal or PDF) and paste it whole — headers and totals included. Nothing
      is written until you commit, and the preview checks its own arithmetic
      against the GPAs the registrar printed.</p>
      <textarea id="ac-import-text" class="modal-textarea ac-import-text" rows="10"
        placeholder="Fall 2025&#10;Course Description Attempted Earned Grade Points&#10;ENG  101 Writing and Research 3.000 3.000 B 9.000&#10;…">${esc(acImportText)}</textarea>
      <div class="field-row-inline">
        <button class="btn primary" id="ac-preview">Preview</button>
        <button class="btn" id="ac-clear">Clear</button>
      </div>
    </div>
    <div id="ac-preview-out">${acPreview ? previewHtml(acPreview) : ""}</div>`;

  el("ac-clear").addEventListener("click", () => {
    acImportText = ""; acPreview = null; acImport(body);
  });
  el("ac-preview").addEventListener("click", async () => {
    acImportText = el("ac-import-text").value;
    if (!acImportText.trim()) { toast("Paste something first", "error"); return; }
    el("ac-preview-out").innerHTML = `<div class="loading">Parsing…</div>`;
    try {
      acPreview = await API.post("/academics/import/preview", { text: acImportText });
    } catch (e) { el("ac-preview-out").innerHTML = ""; return; }
    el("ac-preview-out").innerHTML = previewHtml(acPreview);
    wirePreview(body);
  });
  if (acPreview) wirePreview(body);
}

function previewHtml(p) {
  const verdict = p.scale_looks_right
    ? `<div class="ac-ok">Every term's arithmetic matches the GPA printed on the
       transcript — your grade scale is correct.</div>`
    : `<div class="ac-warn">
        ${p.mismatched_terms ? `<strong>${p.mismatched_terms}</strong> term${p.mismatched_terms > 1 ? "s" : ""}
          disagree with the printed totals. ` : ""}
        ${p.unknown_grades.length ? `Unrecognised grade${p.unknown_grades.length > 1 ? "s" : ""}:
          <strong>${p.unknown_grades.map(esc).join(", ")}</strong>. ` : ""}
        Fix the grade scale before importing, or these numbers will be wrong in a
        way that still looks plausible.</div>`;

  const terms = p.terms.map((t) => `
    <div class="joint-card ac-prev-term ${t.matches_transcript === false ? "bad" : ""}">
      <div class="ac-prev-head">
        <strong>${esc(t.name)}</strong>
        <span class="chip">${esc(AC_STATUS_LABEL[t.status] || t.status)}</span>
        <span>${t.courses.length} courses · ${creditText(t.computed.attempted)} cr</span>
        <span>GPA ${gpaText(t.computed.gpa)}${t.stated
          ? ` <span class="ac-dim">(transcript: ${t.stated.gpa.toFixed(2)})</span>` : ""}</span>
        ${t.duplicate ? `<span class="chip on c-amber">already imported</span>` : ""}
      </div>
      ${t.discrepancies.length
        ? `<ul class="ac-disc">${t.discrepancies.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>`
        : ""}
      <div class="ac-prev-courses">${t.courses.map((c) =>
        `<span class="ac-prev-course"><span class="mono">${esc(c.code)}</span>
         ${esc(c.title)} · ${creditText(c.credits)}cr
         ${c.grade ? esc(c.grade) : "<span class='ac-dim'>no grade</span>"}</span>`).join("")}</div>
    </div>`).join("");

  return `
    ${verdict}
    <div class="field-row-inline ac-commit-row">
      <span>${p.new_terms} new term${p.new_terms === 1 ? "" : "s"},
        ${p.duplicate_terms} already present</span>
      <label class="toggle-row">
        <input type="checkbox" id="ac-replace"> Replace terms that already exist
      </label>
      <button class="btn primary" id="ac-commit">Import</button>
    </div>
    ${terms}
    ${p.unparsed.length ? `
      <div class="joint-card">
        <div class="block-title">Lines not understood (${p.unparsed.length})</div>
        <p class="settings-hint">Shown so nothing disappears silently. Header and
        boilerplate rows are expected here; a course row is not.</p>
        <ul class="ac-disc">${p.unparsed.slice(0, 30).map((l) =>
          `<li class="mono">${esc(l)}</li>`).join("")}</ul>
      </div>` : ""}`;
}

function wirePreview(body) {
  el("ac-commit")?.addEventListener("click", async () => {
    const replace = el("ac-replace")?.checked;
    if (!acPreview.scale_looks_right &&
        !confirm("The parsed numbers disagree with the transcript's own totals. Import anyway?")) return;
    let r;
    try {
      r = await API.post("/academics/import/commit", { text: acImportText, replace });
    } catch (e) { return; }
    toast(`Imported ${r.added_terms} terms, ${r.added_courses} courses` +
          (r.replaced_terms ? `, replaced ${r.replaced_terms}` : "") +
          (r.skipped_terms ? `, skipped ${r.skipped_terms} duplicate` : ""), "info", 6000);
    acPreview = null; acImportText = "";
    acView = "terms";
    renderAcademics();
  });
}

// ---------------------------------------------------------------- scale

function acScaleView(body) {
  const rows = acScale().map((g) => `
    <tr data-grade="${escAttr(g.grade)}">
      <td class="mono">${esc(g.grade)}</td>
      <td><input type="number" step="0.1" min="0" max="10" class="ac-sc-points"
                 value="${g.points === null ? "" : g.points}" placeholder="none"></td>
      <td><input type="checkbox" class="ac-sc-gpa" ${g.counts_gpa ? "checked" : ""}></td>
      <td><input type="checkbox" class="ac-sc-credit" ${g.earns_credit ? "checked" : ""}></td>
      <td><input type="checkbox" class="ac-sc-verified" ${g.verified ? "checked" : ""}></td>
      <td><button class="btn tiny ac-sc-del">✕</button></td>
    </tr>`).join("");

  body.innerHTML = `
    <div class="joint-card">
      <div class="block-title">Grade scale</div>
      <p class="settings-hint">Every GPA in this section is derived from this
      table, so it is the one thing worth getting right. <strong>A, B+, B, D and
      W are confirmed</strong> — the arithmetic on your existing transcript only
      works with those values. The rest are inferred from the same half-step
      pattern; check them against the WCC catalog and tick “confirmed” once
      you have.</p>
      <p class="settings-hint">Leave points blank for grades that carry none at
      all (W, I, P). That is different from 0, which is a real F.</p>
      <table class="ac-table ac-scale-table">
        <thead><tr>
          <th>Grade</th><th>Points</th><th>In GPA</th><th>Earns credit</th>
          <th>Confirmed</th><th></th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <div class="field-row-inline">
        <input type="text" id="ac-sc-new" placeholder="new grade (A-)" maxlength="3">
        <input type="number" id="ac-sc-new-pts" step="0.1" min="0" max="10" placeholder="points">
        <button class="btn" id="ac-sc-add">Add</button>
        <button class="btn primary" id="ac-sc-save">Save scale</button>
      </div>
    </div>`;

  el("ac-sc-save").addEventListener("click", async () => {
    const grades = [...body.querySelectorAll("tr[data-grade]")].map((tr) => {
      const pts = tr.querySelector(".ac-sc-points").value.trim();
      return {
        grade: tr.dataset.grade,
        points: pts === "" ? null : Number(pts),
        counts_gpa: tr.querySelector(".ac-sc-gpa").checked,
        earns_credit: tr.querySelector(".ac-sc-credit").checked,
        verified: tr.querySelector(".ac-sc-verified").checked,
      };
    });
    try { await API.request("PUT", "/academics/scale", { grades }); } catch (e) { return; }
    toast("Scale saved — every GPA re-derived", "info");
    renderAcademics();
  });

  el("ac-sc-add").addEventListener("click", async () => {
    const grade = el("ac-sc-new").value.trim().toUpperCase();
    if (!grade) return;
    const pts = el("ac-sc-new-pts").value.trim();
    try {
      await API.request("PUT", "/academics/scale", {
        grades: [{ grade, points: pts === "" ? null : Number(pts),
                   counts_gpa: pts !== "", earns_credit: true, verified: true }],
      });
    } catch (e) { return; }
    renderAcademics();
  });

  body.querySelectorAll(".ac-sc-del").forEach((b) =>
    b.addEventListener("click", async () => {
      const grade = b.closest("tr").dataset.grade;
      if (!confirm(`Remove ${grade} from the scale? Courses with it become attempted-only.`)) return;
      await API.del(`/academics/scale/${encodeURIComponent(grade)}`);
      renderAcademics();
    }));
}

// The scale-warning banner sits above the view switcher, so its button has to
// reach the same routing the tabs use.
document.addEventListener("click", (e) => {
  if (e.target.id === "ac-warn-scale") { acView = "scale"; renderAcademics(); }
});
