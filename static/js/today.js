// The landing page: what's happening right now, pulled from every section.
// Read-mostly, but routines are checkable and cards completable inline so
// you rarely need to leave it.

async function renderToday() {
  const panel = el("panel");
  panel.innerHTML = `<div class="loading">Loading…</div>`;

  const wantFinance = (window.OPSDECK.settings?.enabled_modules || []).includes("finance");
  const [data, pendingNotes, healthSummary, finSummary] = await Promise.all([
    API.get(`/today?date=${todayISO()}`),
    API.get("/notes/quick?status=pending").catch(() => []),
    // Optional: the profile may have Health switched off, or have no
    // readings yet. Either way Today should still render.
    API.get("/health/summary?days=7").catch(() => ({})),
    // Same deal for Finance - absent or erroring, Today still renders.
    wantFinance ? API.get("/finance/summary").catch(() => null) : Promise.resolve(null),
  ]);

  const eventsHtml = data.events.length
    ? data.events.map((e) => `
        <div class="today-row">
          <span class="today-time">${e.all_day ? "All day" : fmtTime(e.start_at)}</span>
          <span class="dot dot-${esc(e.color)}"></span>
          <span class="today-text">${esc(e.title)}</span>
          ${e.location ? `<span class="today-meta">${esc(e.location)}</span>` : ""}
          ${e.rrule ? `<span class="repeat-badge">repeats</span>` : ""}
        </div>`).join("")
    : `<p class="empty-state">Nothing scheduled.</p>`;

  const groups = ["morning", "afternoon", "evening", "anytime"];
  const byGroup = {};
  data.routines.forEach((r) => (byGroup[r.time_group] ||= []).push(r));

  const routinesHtml = data.routines.length
    ? groups.filter((g) => byGroup[g]).map((g) => `
        <div class="routine-group">
          <div class="phase-label">${esc(g)}</div>
          ${byGroup[g].map((r) => `
            <div class="routine-row ${r.done_today ? "done" : ""}" data-routine="${r.id}">
              <span class="check"></span>
              <span class="routine-name">${esc(r.name)}</span>
              ${r.streak > 0 ? `<span class="streak">${r.streak}d</span>` : ""}
            </div>`).join("")}
        </div>`).join("")
    : `<p class="empty-state">No routines yet.</p>`;

  const cardRow = (c) => `
    <div class="today-row card-row" data-card="${c.id}">
      <span class="check small"></span>
      <span class="today-text">${esc(c.title)}</span>
      <span class="today-meta">${esc(c.board_title)} / ${esc(c.list_title)}</span>
      <span class="due-pill ${dueClass(c.due_at)}">${fmtDate(c.due_at)}</span>
    </div>`;

  const overdueHtml = data.cards_overdue.length
    ? `<div class="today-block overdue-block">
         <h2 class="block-title">Overdue <span class="count">${data.cards_overdue.length}</span></h2>
         ${data.cards_overdue.map(cardRow).join("")}
       </div>`
    : "";

  const dueHtml = data.cards_due.length
    ? data.cards_due.map(cardRow).join("")
    : `<p class="empty-state">Nothing due today.</p>`;

  const doneCount = data.routines.filter((r) => r.done_today).length;

  // Where a suggestion says it'd land, in words, so filing is one glance
  // and one tap rather than a form.
  const destOf = (s) => {
    if (!s || !s.kind) return "a card";
    if (s.kind === "event") return `calendar${s.due ? " · " + fmtDate(s.due) : ""}`;
    if (s.kind === "doc") return "Docs · Quick notes";
    if (s.kind === "routine") return "Routines";
    if (s.kind === "done") return "already done — dismiss?";
    const b = s.board;
    return (b ? `${b.board_title} / ${b.list_title}` : "a card")
      + (s.due ? " · due " + fmtDate(s.due) : "");
  };

  const notesHtml = pendingNotes.length
    ? `<div class="today-block qn-pending">
         <h2 class="block-title">Unfiled notes <span class="count">${pendingNotes.length}</span></h2>
         ${pendingNotes.map((n) => `
           <div class="qn-row" data-note="${n.id}">
             <span class="qn-body">${esc(n.body)}</span>
             <span class="qn-dest">→ ${esc(destOf(n.suggestion))}</span>
             <button class="btn tiny qn-file">File</button>
             <button class="btn tiny qn-drop">Dismiss</button>
           </div>`).join("")}
       </div>`
    : "";

  // ---- health strip ----
  // Last night plus whatever the watch has logged so far today. Deliberately
  // one line: this is a glance, and the Health tab is where you actually
  // look at anything. Sleep sits first because under wake-date bucketing
  // today's sleep figure *is* last night's, which is the number you want
  // before you've done anything else.
  const stripOrder = ["sleep_minutes", "steps", "active_minutes",
                      "exercise_minutes", "calories", "weight_kg"];
  const stripItems = stripOrder
    .filter((k) => healthSummary[k] && healthSummary[k].today != null)
    .slice(0, 5)
    .map((k) => {
      const s = healthSummary[k];
      let cmp = "";
      if (s.avg) {
        const pct = Math.round(((s.today - s.avg) / s.avg) * 100);
        cmp = Math.abs(pct) >= 5
          ? `<span class="hs-delta ${pct > 0 ? "up" : "down"}">${pct > 0 ? "▲" : "▼"}${Math.abs(pct)}%</span>`
          : `<span class="hs-delta flat">≈</span>`;
      }
      return `
        <div class="hs-item">
          <span class="hs-label">${k === "sleep_minutes" ? "Last night" : esc(s.label)}</span>
          <span class="hs-value">${fmtHealthStrip(k, s.today)}</span>
          ${cmp}
        </div>`;
    }).join("");

  const healthStrip = stripItems
    ? `<button class="health-strip" id="health-strip" title="Open Health">
         ${stripItems}<span class="hs-more">›</span>
       </button>`
    : "";

  // ---- finance glance ----
  // Month spend, the envelopes closest to their limits, and how many
  // transactions still need filing. One tap through to the entry form.
  let financeStrip = "";
  if (finSummary && finSummary.balances.length) {
    const nearest = finSummary.categories
      .filter((c) => c.limit_cents != null && c.effective_limit_cents > 0)
      .sort((a, b) => (b.spent_cents / b.effective_limit_cents)
                    - (a.spent_cents / a.effective_limit_cents))
      .slice(0, 3);
    const money = (c) => "$" + (Math.abs(c) / 100).toLocaleString(undefined,
      { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    financeStrip = `
      <button class="health-strip" id="finance-strip" title="Open Finance">
        <div class="hs-item">
          <span class="hs-label">Spent this month</span>
          <span class="hs-value">${money(finSummary.spend_total_cents)}</span>
        </div>
        ${nearest.map((c) => {
          const pct = Math.round((c.spent_cents / c.effective_limit_cents) * 100);
          return `
            <div class="hs-item">
              <span class="hs-label">${esc(c.name)}</span>
              <span class="hs-value">${pct}%</span>
              ${c.remaining_cents < 0
                ? `<span class="hs-delta up">over</span>`
                : ""}
            </div>`;
        }).join("")}
        ${finSummary.uncategorized.count ? `
          <div class="hs-item">
            <span class="hs-label">Unfiled</span>
            <span class="hs-value">${finSummary.uncategorized.count}</span>
          </div>` : ""}
        <span class="hs-more">›</span>
      </button>`;
  }

  panel.innerHTML = `
    <h1 class="section-title">Today</h1>
    <p class="section-sub">${fmtDateLong(data.date)}</p>

    ${healthStrip}
    ${financeStrip}

    <div class="quick-note">
      <textarea id="qn-input" rows="1" placeholder="Quick note — anything, file it later…"></textarea>
      <div class="qn-actions">
        <button class="btn" id="qn-save">Capture</button>
        <button class="btn primary" id="qn-file-now">Capture &amp; file</button>
      </div>
    </div>

    ${notesHtml}

    ${overdueHtml}

    <div class="today-grid">
      <div class="today-block">
        <h2 class="block-title">Schedule</h2>
        ${eventsHtml}
      </div>

      <div class="today-block">
        <h2 class="block-title">
          Routines <span class="count">${doneCount}/${data.routines.length}</span>
        </h2>
        ${routinesHtml}
      </div>
    </div>

    <div class="today-block">
      <h2 class="block-title">Due today</h2>
      ${dueHtml}
    </div>
  `;

  // ---- quick capture ----
  const input = el("qn-input");
  const capture = async (fileNow) => {
    const text = input.value.trim();
    if (!text) return;
    try {
      const note = await API.post("/notes/quick", { body: text, file_now: fileNow });
      input.value = "";
      if (note.status === "filed") toast(`Filed as ${note.filed_as}`);
      else if (fileNow) toast("Captured — not sure where it goes, left it below", "info", 5000);
      else toast("Note captured");
      renderToday();
    } catch (e) { /* API client already toasted the reason */ }
  };
  el("qn-save").addEventListener("click", () => capture(false));
  el("qn-file-now").addEventListener("click", () => capture(true));

  // Enter files it, Shift+Enter for a newline - capture should be one key.
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); capture(true); }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 160) + "px";
  });

  panel.querySelectorAll(".qn-row").forEach((row) => {
    const id = row.dataset.note;
    row.querySelector(".qn-file").addEventListener("click", async () => {
      try {
        const n = await API.post(`/notes/quick/${id}/file`);
        toast(`Filed as ${n.filed_as}`);
        renderToday();
      } catch (e) { /* toasted */ }
    });
    row.querySelector(".qn-drop").addEventListener("click", async () => {
      await API.del(`/notes/quick/${id}`);
      renderToday();
    });
  });

  el("health-strip")?.addEventListener("click", () => go("health"));
  el("finance-strip")?.addEventListener("click", () => go("finance"));

  panel.querySelectorAll(".routine-row").forEach((row) => {
    row.addEventListener("click", async () => {
      await API.post(`/routines/${row.dataset.routine}/toggle`, { date: todayISO() });
      renderToday();
    });
  });

  panel.querySelectorAll(".card-row").forEach((row) => {
    row.querySelector(".check").addEventListener("click", async (e) => {
      e.stopPropagation();
      await API.patch(`/cards/${row.dataset.card}`, { completed: 1 });
      toast("Card completed");
      renderToday();
    });
  });
}


// Compact formatter for the Today strip. Deliberately terser than the
// Health tab's - "7h 02m" and "8.4k" read at a glance where "8,432 steps"
// does not, on one line next to four other numbers.
function fmtHealthStrip(key, value) {
  if (value == null) return "—";
  if (key === "sleep_minutes" || key === "time_in_bed_minutes") {
    const h = Math.floor(value / 60), m = Math.round(value % 60);
    return `${h}h ${String(m).padStart(2, "0")}m`;
  }
  if (key === "steps") {
    return value >= 10000 ? `${(value / 1000).toFixed(1)}k` : Math.round(value).toLocaleString();
  }
  if (key === "calories") return `${Math.round(value)}`;
  if (key === "weight_kg") return `${Math.round(value * 10) / 10}kg`;
  return `${Math.round(value)}m`;
}
