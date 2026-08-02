// Month-grid calendar. Pulls expanded event occurrences from the server
// (the RRULE math lives in Python, not here) and overlays card due dates so
// one view answers "what's on this month".

let calYear = new Date().getFullYear();
let calMonth = new Date().getMonth(); // 0-indexed

async function renderCalendar() {
  const panel = el("panel");
  panel.innerHTML = `<div class="loading">Loading…</div>`;

  const first = new Date(calYear, calMonth, 1);
  const last = new Date(calYear, calMonth + 1, 0);
  const gridStart = addDaysISO(isoOf(first), -first.getDay());
  const gridEnd = addDaysISO(isoOf(last), 6 - last.getDay());

  const [occurrences, boardsData] = await Promise.all([
    API.get(`/events?start=${gridStart}&end=${gridEnd}`),
    API.get("/boards"),
  ]);

  // Bucket everything by date so each cell is a simple lookup. An event that
  // spans days is pushed onto every day it covers, tagged so the CSS can
  // draw one continuous bar instead of a lone dot on the start date.
  const byDate = {};
  occurrences.forEach((o) => {
    const startDay = o.start_at.slice(0, 10);
    const endDay = (o.end_at || o.start_at).slice(0, 10);
    const base = {
      kind: "event", title: o.title, color: o.color,
      event_id: o.event_id, occurrence: o.occurrence, rrule: o.rrule,
    };

    if (endDay <= startDay) {
      (byDate[startDay] ||= []).push({
        ...base, time: o.all_day ? "" : fmtTime(o.start_at), span: false,
      });
      return;
    }

    for (let day = startDay; day <= endDay; day = addDaysISO(day, 1)) {
      const isStart = day === startDay;
      const isEnd = day === endDay;
      (byDate[day] ||= []).push({
        ...base,
        span: true,
        spanClass: isStart ? "span-start" : isEnd ? "span-end" : "span-mid",
        // Only the first day carries the clock time; the rest are continuation.
        time: isStart && !o.all_day ? fmtTime(o.start_at) : "",
      });
    }
  });
  boardsData.forEach((b) => b.lists.forEach((l) => l.cards.forEach((c) => {
    if (!c.due_at || c.completed) return;
    (byDate[c.due_at.slice(0, 10)] ||= []).push({
      kind: "card", title: c.title, color: "gray", time: "", card_id: c.id,
    });
  })));

  // Multi-day bars sort to the top of every cell so a run stays on the same
  // visual line as it crosses the week.
  Object.values(byDate).forEach((items) =>
    items.sort((a, b) => (b.span ? 1 : 0) - (a.span ? 1 : 0)));

  const today = todayISO();
  let cells = "";
  let cursor = gridStart;
  while (cursor <= gridEnd) {
    const inMonth = new Date(cursor + "T00:00:00").getMonth() === calMonth;
    const items = byDate[cursor] || [];
    cells += `
      <div class="cal-cell ${inMonth ? "" : "outside"} ${cursor === today ? "today" : ""}" data-date="${cursor}">
        <div class="cal-daynum">${Number(cursor.slice(8, 10))}</div>
        ${items.slice(0, 4).map((i) => `
          <div class="cal-item ${i.kind} ${i.span ? `span ${i.spanClass}` : ""}"
               title="${escAttr(i.title)}"
               ${i.kind === "event" ? `data-event="${i.event_id}" data-occ="${i.occurrence}"` : `data-card="${i.card_id}"`}>
            <span class="dot dot-${esc(i.color)}"></span>
            ${i.time ? `<span class="cal-time">${i.time}</span>` : ""}
            <span class="cal-item-text">${esc(i.title)}</span>
          </div>`).join("")}
        ${items.length > 4 ? `<div class="cal-more">+${items.length - 4} more</div>` : ""}
      </div>`;
    cursor = addDaysISO(cursor, 1);
  }

  const monthName = first.toLocaleDateString(undefined, { month: "long", year: "numeric" });

  panel.innerHTML = `
    <div class="section-head-row">
      <h1 class="section-title">Calendar</h1>
      <div class="head-actions">
        <button class="btn" id="cal-prev">‹</button>
        <button class="btn" id="cal-today">Today</button>
        <button class="btn" id="cal-next">›</button>
        <button class="btn primary" id="new-event">+ Event</button>
      </div>
    </div>
    <p class="section-sub">${monthName}</p>
    <div class="cal-scroll">
      <div class="cal-grid">
        ${["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].map((d) => `<div class="cal-head">${d}</div>`).join("")}
        ${cells}
      </div>
    </div>
    <p class="cal-legend">Grey dots are card due dates. Bars span multi-day events. Click any event to edit it.</p>`;

  el("cal-prev").addEventListener("click", () => { shiftMonth(-1); });
  el("cal-next").addEventListener("click", () => { shiftMonth(1); });
  el("cal-today").addEventListener("click", () => {
    calYear = new Date().getFullYear();
    calMonth = new Date().getMonth();
    renderCalendar();
  });
  el("new-event").addEventListener("click", () => openEventModal(null));

  panel.querySelectorAll(".cal-cell").forEach((cell) => {
    cell.addEventListener("dblclick", () => openEventModal(null, cell.dataset.date));
  });
  panel.querySelectorAll(".cal-item.event").forEach((item) => {
    item.addEventListener("click", (e) => {
      e.stopPropagation();
      openEventModal(Number(item.dataset.event), null, item.dataset.occ);
    });
  });
}

function isoOf(d) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
function shiftMonth(delta) {
  calMonth += delta;
  if (calMonth < 0) { calMonth = 11; calYear--; }
  if (calMonth > 11) { calMonth = 0; calYear++; }
  renderCalendar();
}

// ---------- Event modal with RRULE builder ----------
async function openEventModal(eventId, presetDate, occurrence) {
  let ev = {
    title: "", description: "", location: "", color: "blue",
    start_at: (presetDate || todayISO()) + "T09:00:00",
    end_at: "", all_day: 0, rrule: "", remind_min: null,
  };
  if (eventId) {
    const all = await API.get("/events");
    ev = all.find((e) => e.id === eventId) || ev;
  }

  const r = parseRRule(ev.rrule || "");

  openModal(`
    <div class="modal-head">
      <h2 class="modal-title">${eventId ? "Edit event" : "New event"}</h2>
      <button class="icon-btn" onclick="closeModal()">×</button>
    </div>

    <label class="field-label">Title</label>
    <input type="text" id="ev-title" value="${escAttr(ev.title)}" placeholder="Event title">

    <div class="field-row">
      <div>
        <label class="field-label">Starts</label>
        <input type="datetime-local" id="ev-start" value="${(ev.start_at || "").slice(0, 16)}">
      </div>
      <div>
        <label class="field-label">Ends</label>
        <input type="datetime-local" id="ev-end" value="${(ev.end_at || "").slice(0, 16)}">
      </div>
    </div>

    <div class="field-row">
      <div>
        <label class="field-label">Color</label>
        <select id="ev-color">
          ${LABEL_COLORS.map((c) => `<option value="${c}" ${ev.color === c ? "selected" : ""}>${c}</option>`).join("")}
        </select>
      </div>
      <div>
        <label class="field-label">Remind me</label>
        <select id="ev-remind">
          <option value="">No reminder</option>
          ${[5, 10, 15, 30, 60, 120, 1440].map((m) => `
            <option value="${m}" ${ev.remind_min == m ? "selected" : ""}>
              ${m >= 1440 ? "1 day" : m >= 60 ? m / 60 + " hr" : m + " min"} before
            </option>`).join("")}
        </select>
      </div>
    </div>

    <label class="field-label">
      <input type="checkbox" id="ev-allday" ${ev.all_day ? "checked" : ""}> All day
    </label>

    <label class="field-label">Repeat</label>
    <div class="field-row">
      <select id="rr-freq">
        <option value="" ${!r.freq ? "selected" : ""}>Does not repeat</option>
        <option value="DAILY" ${r.freq === "DAILY" ? "selected" : ""}>Daily</option>
        <option value="WEEKLY" ${r.freq === "WEEKLY" ? "selected" : ""}>Weekly</option>
        <option value="MONTHLY" ${r.freq === "MONTHLY" ? "selected" : ""}>Monthly</option>
        <option value="YEARLY" ${r.freq === "YEARLY" ? "selected" : ""}>Yearly</option>
      </select>
      <input type="number" id="rr-interval" min="1" value="${r.interval || 1}" title="Every N">
    </div>

    <div id="rr-weekdays" class="weekday-picker" style="${r.freq === "WEEKLY" ? "" : "display:none"}">
      ${["SU", "MO", "TU", "WE", "TH", "FR", "SA"].map((d, i) => `
        <button type="button" class="wd ${r.byday.includes(d) ? "on" : ""}" data-day="${d}">
          ${["S", "M", "T", "W", "T", "F", "S"][i]}
        </button>`).join("")}
    </div>

    <div class="field-row" id="rr-end-row" style="${r.freq ? "" : "display:none"}">
      <div>
        <label class="field-label">Ends after (count)</label>
        <input type="number" id="rr-count" min="1" value="${r.count || ""}" placeholder="never">
      </div>
      <div>
        <label class="field-label">Or on date</label>
        <input type="date" id="rr-until" value="${r.until || ""}">
      </div>
    </div>

    <label class="field-label">Location</label>
    <input type="text" id="ev-loc" value="${escAttr(ev.location)}">

    <label class="field-label">Notes</label>
    <textarea id="ev-desc" class="modal-textarea" rows="3">${esc(ev.description)}</textarea>

    <div class="modal-actions">
      ${eventId && occurrence && ev.rrule
        ? `<button class="btn" id="skip-occ">Skip this one</button>` : ""}
      ${eventId ? `<button class="btn danger" id="delete-event">Delete series</button>` : ""}
      <button class="btn primary" id="save-event">Save</button>
    </div>
  `, (modal) => {
    const days = new Set(r.byday);

    el("rr-freq").addEventListener("change", (e) => {
      el("rr-weekdays").style.display = e.target.value === "WEEKLY" ? "" : "none";
      el("rr-end-row").style.display = e.target.value ? "" : "none";
    });

    modal.querySelectorAll(".wd").forEach((btn) => {
      btn.addEventListener("click", () => {
        const d = btn.dataset.day;
        days.has(d) ? days.delete(d) : days.add(d);
        btn.classList.toggle("on");
      });
    });

    if (eventId && occurrence && ev.rrule) {
      el("skip-occ").addEventListener("click", async () => {
        await API.post(`/events/${eventId}/occurrences/${occurrence}`, { action: "skip" });
        closeModal();
        toast("Occurrence skipped");
        renderCalendar();
      });
    }

    if (eventId) {
      el("delete-event").addEventListener("click", async () => {
        if (!confirm("Delete the whole series?")) return;
        await API.del(`/events/${eventId}`);
        closeModal();
        renderCalendar();
      });
    }

    el("save-event").addEventListener("click", async () => {
      const payload = {
        title: el("ev-title").value || "Untitled",
        description: el("ev-desc").value,
        location: el("ev-loc").value,
        color: el("ev-color").value,
        start_at: el("ev-start").value + ":00",
        end_at: el("ev-end").value ? el("ev-end").value + ":00" : null,
        all_day: el("ev-allday").checked ? 1 : 0,
        rrule: buildRRule(days),
        remind_min: el("ev-remind").value ? Number(el("ev-remind").value) : null,
      };
      if (eventId) await API.patch(`/events/${eventId}`, payload);
      else await API.post("/events", payload);
      closeModal();
      toast("Saved");
      renderCalendar();
    });
  });
}

// Assemble an RFC 5545 RRULE from the form controls.
function buildRRule(days) {
  const freq = el("rr-freq").value;
  if (!freq) return "";
  const parts = [`FREQ=${freq}`];
  const interval = Number(el("rr-interval").value || 1);
  if (interval > 1) parts.push(`INTERVAL=${interval}`);
  if (freq === "WEEKLY" && days.size) parts.push(`BYDAY=${[...days].join(",")}`);
  const count = el("rr-count").value;
  const until = el("rr-until").value;
  if (count) parts.push(`COUNT=${count}`);
  else if (until) parts.push(`UNTIL=${until.replace(/-/g, "")}T235959Z`);
  return "RRULE:" + parts.join(";");
}

function parseRRule(str) {
  const out = { freq: "", interval: 1, byday: [], count: "", until: "" };
  if (!str) return out;
  str.replace(/^RRULE:/i, "").split(";").forEach((p) => {
    const [k, v] = p.split("=");
    if (!v) return;
    if (k === "FREQ") out.freq = v;
    if (k === "INTERVAL") out.interval = Number(v);
    if (k === "BYDAY") out.byday = v.split(",");
    if (k === "COUNT") out.count = v;
    if (k === "UNTIL") out.until = `${v.slice(0, 4)}-${v.slice(4, 6)}-${v.slice(6, 8)}`;
  });
  return out;
}
