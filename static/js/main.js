// Entry point: section routing, global search, notification polling, clock.

const SECTIONS = {
  today: renderToday,
  board: renderBoard,
  calendar: renderCalendar,
  routines: renderRoutines,
  docs: renderDocs,
  tree: renderTree,
  thm: renderThm,
  growth: renderGrowth,
  joint: renderJoint,
  health: renderHealth,
  finance: renderFinance,
  settings: renderSettings,
};

let activeSection = "today";

function go(section) {
  activeSection = section;
  document.querySelectorAll(".node").forEach((n) =>
    n.classList.toggle("active", n.dataset.section === section));
  location.hash = section;
  // Navigating always closes the mobile drawer - leaving it open over the
  // page you just asked for is the classic off-canvas annoyance.
  closeDrawer();
  syncMobileTitle(section);
  SECTIONS[section]();
}

// ---------- Mobile drawer ----------
function openDrawer() {
  document.body.classList.add("drawer-open");
  el("mb-menu")?.setAttribute("aria-expanded", "true");
}
function closeDrawer() {
  document.body.classList.remove("drawer-open");
  el("mb-menu")?.setAttribute("aria-expanded", "false");
}
function toggleDrawer() {
  document.body.classList.contains("drawer-open") ? closeDrawer() : openDrawer();
}

function syncMobileTitle(section) {
  const t = el("mb-title");
  if (!t) return;
  const labels = {
    today: "Today", board: "Boards", calendar: "Calendar", routines: "Routines",
    docs: "Docs", tree: "Skill tree", thm: "TryHackMe", growth: "Growth",
    chat: "Mentor", joint: "Us", health: "Health", settings: "Settings",
  };
  t.textContent = labels[section] || section;
}

el("mb-menu")?.addEventListener("click", toggleDrawer);
el("drawer-backdrop")?.addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeDrawer();
});

el("node-tree").addEventListener("click", (e) => {
  const node = e.target.closest(".node");
  if (node) go(node.dataset.section);
});

window.addEventListener("hashchange", () => {
  const s = location.hash.slice(1);
  if (SECTIONS[s] && s !== activeSection) go(s);
});

// ---------- Global search ----------
let searchTimer = null;
el("global-search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  const q = e.target.value.trim();
  const box = el("search-results");
  if (q.length < 2) { box.classList.remove("open"); return; }

  searchTimer = setTimeout(async () => {
    const r = await API.get(`/search?q=${encodeURIComponent(q)}`);
    const sections = [];
    if (r.cards.length) sections.push(group("Cards", r.cards.map((c) =>
      `<div class="sr-item" data-go="board">${esc(c.title)}
         <span class="today-meta">${esc(c.board_title)}</span></div>`)));
    if (r.events.length) sections.push(group("Events", r.events.map((ev) =>
      `<div class="sr-item" data-go="calendar">${esc(ev.title)}
         <span class="today-meta">${fmtDate(ev.start_at.slice(0, 10))}</span></div>`)));
    if (r.docs.length) sections.push(group("Docs", r.docs.map((d) =>
      `<div class="sr-item" data-go="docs" data-doc="${d.id}">${esc(d.title)}</div>`)));

    box.innerHTML = sections.join("") || `<div class="sr-empty">No matches</div>`;
    box.classList.add("open");

    box.querySelectorAll(".sr-item").forEach((item) => {
      item.addEventListener("click", () => {
        box.classList.remove("open");
        el("global-search").value = "";
        if (item.dataset.doc) { go("docs"); openDocViewer(Number(item.dataset.doc)); }
        else go(item.dataset.go);
      });
    });
  }, 220);
});

function group(title, items) {
  return `<div class="sr-group"><div class="sr-title">${title}</div>${items.join("")}</div>`;
}

document.addEventListener("click", (e) => {
  if (!e.target.closest(".sidebar-search")) el("search-results").classList.remove("open");
});

// ---------- Notifications ----------
// Browser notifications fire while a tab is open. True background push
// (browser closed) needs Web Push with VAPID keys - see README.
const fired = new Set();

async function setupNotifications() {
  const btn = el("notify-btn");
  if (!btn) return;

  if (!("Notification" in window)) {
    btn.textContent = "Notifications unsupported";
    btn.disabled = true;
    btn.title = "This browser has no Notification API.";
    return;
  }

  // Browsers only expose notifications and service workers on a secure
  // context: https, or localhost. Over plain http on a LAN/tailnet IP the
  // permission prompt silently never appears - which is exactly the "button
  // does nothing" symptom. Say so instead of failing quietly.
  if (!window.isSecureContext) {
    btn.textContent = "Notifications need HTTPS";
    btn.disabled = true;
    btn.title =
      "Browsers only allow notifications on https:// or localhost. " +
      "This page is plain http, so the permission prompt can never open. " +
      "Reach the app over Tailscale HTTPS (tailscale serve) or via " +
      "http://localhost:5000 on the host itself.";
    btn.addEventListener("click", () => toast(btn.title, "error", 8000));
    return;
  }

  const sync = () => {
    const p = Notification.permission;
    btn.classList.toggle("on", p === "granted");
    btn.disabled = p === "denied";
    if (p === "granted") {
      btn.textContent = "Notifications on";
      btn.title = "Reminders will fire while this tab is open.";
    } else if (p === "denied") {
      btn.textContent = "Notifications blocked";
      btn.title =
        "You (or the browser) blocked notifications for this site. " +
        "Re-allow them in the site settings - a page can't re-prompt once denied.";
    } else {
      btn.textContent = "Enable notifications";
      btn.title = "";
    }
  };
  sync();

  btn.addEventListener("click", async () => {
    if (Notification.permission === "granted") {
      // Already on - prove it works rather than looking inert.
      new Notification("Ops Deck", { body: "Notifications are working." });
      return;
    }
    let result;
    try {
      result = await Notification.requestPermission();
    } catch (e) {
      toast("Could not request notification permission: " + e.message, "error");
      return;
    }
    sync();
    if (result === "granted") toast("Notifications enabled");
    else if (result === "denied") toast("Notifications blocked in browser settings", "error", 6000);
    else toast("Notification prompt dismissed", "info");
  });

  if ("serviceWorker" in navigator) {
    try { await navigator.serviceWorker.register("/sw.js"); } catch (e) { /* non-fatal */ }
  }

  setInterval(pollReminders, 60000);
  pollReminders();
}

async function pollReminders() {
  if (Notification.permission !== "granted") return;
  let due;
  try { due = await API.get("/reminders/upcoming?minutes=2"); } catch (e) { return; }

  due.forEach((r) => {
    if (fired.has(r.key)) return;
    fired.add(r.key);
    new Notification(r.title, {
      body: `Starts at ${fmtTime(r.start_at)}`,
      tag: r.key,
    });
    toast(`${r.title} — ${fmtTime(r.start_at)}`, "info", 6000);
  });
}

// ---------- Clock ----------
function tickClock() {
  el("clock").textContent = new Date().toLocaleString(undefined, {
    weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}
setInterval(tickClock, 30000);
tickClock();

// ---------- Boot ----------
// Profiles must load first: the API client needs an active profile to scope
// every subsequent call, and the nav is built from that profile's modules.
async function boot() {
  try {
    await bootstrapProfiles();
  } catch (e) {
    // If profiles can't load (older backend, etc.), fall back to the static
    // nav already in the DOM so the app still works.
    console.warn("profile bootstrap failed, running unscoped", e);
  }
  const initial = location.hash.slice(1);
  const start = SECTIONS[initial] ? initial : (enabledSectionsSafe()[0] || "today");
  go(start);
  setupNotifications();
  refreshJointBadge();
  setInterval(refreshJointBadge, 45000);
}

function enabledSectionsSafe() {
  try { return enabledSections(); } catch (e) { return ["today"]; }
}

// Joint activity shows as an unread-notification count on the Us nav item.
async function refreshJointBadge() {
  const badge = el("joint-badge");
  if (!badge) return;
  try {
    const notifs = await API.get("/notifications?unseen=1");
    badge.hidden = !notifs.length;
    badge.textContent = notifs.length;
  } catch (e) { /* stay quiet */ }
}

boot();

// ---------- Mentor badge ----------
// A quiet count on the Growth nav item so pending proposals and in-flight
// verifications are visible without polling the user with notifications.
async function refreshMentorBadge() {
  try {
    const s = await API.get("/mentor/status");
    const n = s.pending_proposals + s.pending_attempts;
    const badge = el("nav-badge");
    if (!badge) return;
    badge.hidden = n === 0;
    badge.textContent = n;
  } catch (e) { /* offline or unauthorised - stay quiet */ }
}
setInterval(refreshMentorBadge, 45000);
refreshMentorBadge();
