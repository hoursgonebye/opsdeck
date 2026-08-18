// Month-grid calendar. Pulls expanded event occurrences from the server
// (the RRULE math lives in Python, not here) and overlays card due dates so
// one view answers "what's on this month".

let calYear = new Date().getFullYear();
let calMonth = new Date().getMonth(); // 0-indexed

// The day a tap opened, and everything bucketed for the visible grid. A month
// cell can only ever show three or four items before it runs out of room -
// on a phone it is closer to one - so the grid is a map, not a reader, and the
// day panel is where you actually read a day.
let calSelected = null;
let calByDate = {};

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
      feed_id: o.feed_id || null,
      // Carried through so the day panel can render a full entry without a
      // second round trip per event.
      start_at: o.start_at, end_at: o.end_at, all_day: o.all_day,
      location: o.location || "", description: o.description || "",
      startDay, endDay,
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
      board_title: b.title, list_title: l.title,
    });
  })));

  // Multi-day bars sort to the top of every cell so a run stays on the same
  // visual line as it crosses the week.
  Object.values(byDate).forEach((items) =>
    items.sort((a, b) => (b.span ? 1 : 0) - (a.span ? 1 : 0)));
  calByDate = byDate;

  const today = todayISO();
  // Opening the month with a day already selected: today if it's in view,
  // otherwise nothing - guessing a different day would be noise.
  if (calSelected && !byDate[calSelected]
      && calSelected.slice(0, 7) !== `${calYear}-${pad(calMonth + 1)}`) {
    calSelected = null;
  }
  let cells = "";
  let cursor = gridStart;
  while (cursor <= gridEnd) {
    const inMonth = new Date(cursor + "T00:00:00").getMonth() === calMonth;
    const items = byDate[cursor] || [];
    cells += `
      <div class="cal-cell ${inMonth ? "" : "outside"} ${cursor === today ? "today" : ""} ${cursor === calSelected ? "selected" : ""}" data-date="${cursor}">
        <div class="cal-daynum">${Number(cursor.slice(8, 10))}${
          items.length ? `<span class="cal-count">${items.length}</span>` : ""}</div>
        ${items.slice(0, CAL_MAX_IN_CELL).map((i) => `
          <div class="cal-item ${i.kind} ${i.span ? `span ${i.spanClass}` : ""}"
               title="${escAttr(i.title)}"
               ${i.kind === "event" ? `data-event="${i.event_id}" data-occ="${i.occurrence}"${i.feed_id ? ` data-feed="${i.feed_id}"` : ""}` : `data-card="${i.card_id}"`}>
            <span class="dot dot-${esc(i.color)}"></span>
            ${i.time ? `<span class="cal-time">${i.time}</span>` : ""}
            <span class="cal-item-text">${esc(i.title)}</span>
          </div>`).join("")}
        ${items.length > CAL_MAX_IN_CELL
          ? `<div class="cal-more">+${items.length - CAL_MAX_IN_CELL} more</div>` : ""}
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
        <button class="btn" id="cal-feeds">Feeds</button>
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
    <div id="cal-day"></div>
    <p class="cal-legend">Tap a day to read everything on it. Grey dots are card
    due dates; bars span multi-day events.</p>`;

  el("cal-prev").addEventListener("click", () => { shiftMonth(-1); });
  el("cal-next").addEventListener("click", () => { shiftMonth(1); });
  el("cal-today").addEventListener("click", () => {
    calYear = new Date().getFullYear();
    calMonth = new Date().getMonth();
    renderCalendar();
  });
  el("new-event").addEventListener("click", () => openEventModal(null));
  el("cal-feeds").addEventListener("click", openFeedsModal);

  // A single tap anywhere in a cell opens that day. This replaces the old
  // double-click-to-create: a phone has no double-click, so on mobile the
  // grid used to be entirely inert - you could see that a day had something
  // on it and had no way to find out what.
  panel.querySelectorAll(".cal-cell").forEach((cell) => {
    cell.addEventListener("click", () => selectDay(cell.dataset.date));
    cell.addEventListener("dblclick", (e) => {
      e.preventDefault();
      openEventModal(null, cell.dataset.date);
    });
  });
  // Tapping an item still jumps straight to that one event on desktop, but it
  // no longer swallows the tap on mobile, where the pill is a few pixels tall.
  panel.querySelectorAll(".cal-item.event").forEach((item) => {
    item.addEventListener("click", (e) => {
      if (window.matchMedia("(max-width: 820px)").matches) return;  // let the day open
      e.stopPropagation();
      if (item.dataset.feed) {
        toast("From a subscribed calendar — edit it at the source", "info", 4500);
        return;
      }
      openEventModal(Number(item.dataset.event), null, item.dataset.occ);
    });
  });

  renderDayPanel();
}

function selectDay(date) {
  calSelected = calSelected === date ? null : date;
  document.querySelectorAll(".cal-cell").forEach((c) =>
    c.classList.toggle("selected", c.dataset.date === calSelected));
  renderDayPanel();
  if (calSelected) {
    // On a phone the panel is below the fold; without this the tap appears to
    // do nothing at all.
    el("cal-day")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

const CAL_MAX_IN_CELL = 4;

// One day, read properly: every event and due card, with times, location,
// notes and the actions that apply. This is the fix for "all the stuff is
// unviewable" - the grid stays a map and this is the page.
function renderDayPanel() {
  const box = el("cal-day");
  if (!box) return;
  if (!calSelected) { box.innerHTML = ""; return; }

  const items = (calByDate[calSelected] || []).slice().sort(calDaySort);
  const isToday = calSelected === todayISO();

  const rows = items.map((i) => {
    if (i.kind === "card") {
      return `
        <div class="cal-day-row card" data-card="${i.card_id}">
          <span class="cal-day-when">due</span>
          <span class="dot dot-${esc(i.color)}"></span>
          <span class="cal-day-main">
            <span class="cal-day-title">${esc(i.title)}</span>
            <span class="cal-day-sub">${esc(i.board_title || "")}${
              i.list_title ? " · " + esc(i.list_title) : ""}</span>
          </span>
        </div>`;
    }
    return `
      <div class="cal-day-row event" data-event="${i.event_id}" data-occ="${i.occurrence}"
           ${i.feed_id ? `data-feed="${i.feed_id}"` : ""}>
        <span class="cal-day-when">${esc(calWhen(i))}</span>
        <span class="dot dot-${esc(i.color)}"></span>
        <span class="cal-day-main">
          <span class="cal-day-title">${esc(i.title)}</span>
          ${i.location ? `<span class="cal-day-sub">${calLinkify(i.location)}</span>` : ""}
          ${i.description ? `<span class="cal-day-desc">${esc(i.description)}</span>` : ""}
          ${i.span ? `<span class="cal-day-sub">${esc(fmtDate(i.startDay))} – ${esc(fmtDate(i.endDay))}</span>` : ""}
          ${i.feed_id ? `<span class="tag">subscribed</span>` : ""}
          ${i.rrule ? `<span class="tag">repeats</span>` : ""}
        </span>
      </div>`;
  }).join("");

  box.innerHTML = `
    <div class="joint-card cal-day-panel">
      <div class="cal-day-head">
        <span class="cal-day-date">${esc(fmtDateLong(calSelected))}${isToday ? " · today" : ""}</span>
        <span class="cal-day-n">${items.length} item${items.length === 1 ? "" : "s"}</span>
        <button class="btn tiny" id="cal-day-add">+ Event</button>
        <button class="icon-btn" id="cal-day-close" aria-label="Close">×</button>
      </div>
      ${rows || `<p class="empty-state small">Nothing on this day.</p>`}
    </div>`;

  el("cal-day-close").addEventListener("click", () => selectDay(calSelected));
  el("cal-day-add").addEventListener("click", () => openEventModal(null, calSelected));

  box.querySelectorAll(".cal-day-row.event").forEach((row) =>
    row.addEventListener("click", (e) => {
      if (e.target.tagName === "A") return;      // let a location link open
      if (row.dataset.feed) {
        toast("From a subscribed calendar — edit it at the source", "info", 4500);
        return;
      }
      openEventModal(Number(row.dataset.event), null, row.dataset.occ);
    }));
  box.querySelectorAll(".cal-day-row.card").forEach((row) =>
    row.addEventListener("click", () => { go("board"); }));
}

// All-day and multi-day runs first, then by clock time.
function calDaySort(a, b) {
  const rank = (i) => (i.kind === "card" ? 2 : (i.span || i.all_day) ? 0 : 1);
  if (rank(a) !== rank(b)) return rank(a) - rank(b);
  return (a.start_at || "").localeCompare(b.start_at || "");
}

function calWhen(i) {
  if (i.all_day) return "all day";
  if (i.span && i.startDay !== calSelected) return "ongoing";
  if (!i.start_at) return "";
  const from = fmtTime(i.start_at);
  const to = i.end_at && i.endDay === calSelected ? fmtTime(i.end_at) : "";
  return to && to !== from ? `${from}–${to}` : from;
}

// A location is very often a URL in practice (a meeting link, a challenge
// platform), and a day panel you cannot click through from is half a feature.
function calLinkify(text) {
  const s = esc(text);
  return /^https?:\/\//i.test(text)
    ? `<a href="${escAttr(text)}" target="_blank" rel="noopener">${s}</a>`
    : s;
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


// ---------- subscribed feeds ----------
// A read-only .ics subscription: a work roster, a class timetable. Its
// events land in the normal events table, so they appear on Today, the
// month grid and the merged Us view without those knowing feeds exist.

// last_synced_at and next_sync_at come from SQLite's datetime('now'), which
// is UTC, while every other timestamp in the UI is local wall-clock. Tag it
// as UTC so the browser converts, rather than showing a sync from four hours
// ago as though it had just happened.
function feedTime(s) {
  if (!s) return "";
  const d = new Date(s.replace(" ", "T") + "Z");
  return isNaN(d) ? s.slice(0, 16)
    : d.toLocaleString([], { month: "short", day: "numeric",
                             hour: "numeric", minute: "2-digit" });
}

async function openFeedsModal() {
  const feeds = await API.get("/calendar/feeds");
  const autoMin = feeds.length ? feeds[0].auto_sync_minutes : 0;

  openModal(`
    <div class="modal-head">
      <h2 class="modal-title">Subscribed calendars</h2>
      <button class="icon-btn" onclick="closeModal()">×</button>
    </div>

    <div class="feed-list">
      ${feeds.map((f) => `
        <div class="feed-row" data-feed="${f.id}">
          <span class="dot dot-${esc(f.color)}"></span>
          <span class="feed-main">
            <span class="feed-name">${esc(f.name)}</span>
            <span class="card-meta">${esc(f.url_host)} · ${f.event_count} events${
              f.last_synced_at ? ` · synced ${esc(feedTime(f.last_synced_at))}` : ""}${
              f.next_sync_at ? ` · next ${esc(feedTime(f.next_sync_at))}` : ""}</span>
            ${f.last_status && f.last_status !== "ok"
              ? `<span class="feed-err">${esc(f.last_status)}</span>` : ""}
          </span>
          <button class="btn tiny" data-sync="${f.id}">Sync</button>
          <button class="btn tiny danger" data-drop="${f.id}">Remove</button>
        </div>`).join("") || `<p class="empty-state small">No subscriptions yet.</p>`}
    </div>

    <label class="field-label">Add a feed</label>
    <input type="text" id="feed-name" placeholder="Name (e.g. Work)">
    <input type="text" id="feed-url" placeholder="https://… .ics  or  webcal://…">
    <div class="field-row">
      <div>
        <label class="field-label">Colour</label>
        <select id="feed-color">
          ${LABEL_COLORS.map((c) => `<option value="${c}" ${c === "red" ? "selected" : ""}>${c}</option>`).join("")}
        </select>
      </div>
    </div>
    <p class="notes-gate-hint">
      Read-only. Events refresh on each sync, so edits belong at the source.
      The URL is stored server-side and never sent back to the browser.
      ${!feeds.length ? "" : autoMin > 0
        ? `Feeds re-sync on their own every ${autoMin} minutes; “Sync all” just forces it now.`
        : `Auto-sync is off (OPSDECK_FEED_SYNC_MINUTES=0), so these only refresh when you sync them.`}
    </p>

    <div class="modal-actions">
      <button class="btn" id="feed-sync-all">Sync all</button>
      <button class="btn primary" id="feed-add">Subscribe</button>
    </div>
  `, (modal) => {
    el("feed-add").addEventListener("click", async () => {
      const url = el("feed-url").value.trim();
      if (!url) { toast("Paste the feed URL", "error"); return; }
      const btn = el("feed-add");
      btn.disabled = true; btn.textContent = "Fetching…";
      try {
        const r = await API.post("/calendar/feeds", {
          name: el("feed-name").value.trim() || "Subscribed calendar",
          url, color: el("feed-color").value,
        });
        toast(`Imported ${r.imported} events`);
        closeModal(); renderCalendar();
      } catch (e) {
        btn.disabled = false; btn.textContent = "Subscribe";
      }
    });

    el("feed-sync-all").addEventListener("click", async () => {
      const r = await API.post("/calendar/feeds/sync-all", {});
      const okd = r.filter((x) => x.ok).reduce((n, x) => n + (x.imported || 0), 0);
      const bad = r.filter((x) => !x.ok);
      toast(bad.length ? `${okd} imported, ${bad.length} failed` : `Synced ${okd} events`,
            bad.length ? "error" : "info", 5000);
      closeModal(); renderCalendar();
    });

    modal.querySelectorAll("[data-sync]").forEach((b) =>
      b.addEventListener("click", async () => {
        b.disabled = true; b.textContent = "…";
        try {
          const r = await API.post(`/calendar/feeds/${b.dataset.sync}/sync`, {});
          toast(`Synced ${r.imported} events`);
        } catch (e) { /* toasted */ }
        closeModal(); renderCalendar();
      }));

    modal.querySelectorAll("[data-drop]").forEach((b) =>
      b.addEventListener("click", async () => {
        if (!confirm("Unsubscribe? Its imported events are removed too.")) return;
        const r = await API.del(`/calendar/feeds/${b.dataset.drop}`);
        toast(`Removed ${r.events_removed} events`);
        closeModal(); renderCalendar();
      }));
  });
}
