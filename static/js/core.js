// Shared plumbing every section uses: the API client, modal, toasts, and
// small date/HTML helpers. Loaded first; everything else assumes it exists.

const API = {
  async request(method, path, body, isForm) {
    const opts = {
      method,
      headers: { "X-API-Token": window.OPSDECK.token },
    };
    // Every request carries the active profile. The server scopes content
    // resources (boards, calendar, routines, docs, notes) by this header,
    // so switching tabs is just switching which id we send - no per-call
    // path juggling. Joint endpoints ignore it; they're household-wide.
    if (window.OPSDECK.activeProfile) {
      opts.headers["X-Profile-Id"] = window.OPSDECK.activeProfile;
    }
    if (body && isForm) {
      opts.body = body; // FormData sets its own Content-Type boundary
    } else if (body) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch("/api" + path, opts);
    if (!res.ok) {
      let msg = `${res.status} ${res.statusText}`;
      try { msg = (await res.json()).error || msg; } catch (e) {}
      toast(msg, "error");
      throw new Error(msg);
    }
    if (res.status === 204) return null;
    const ct = res.headers.get("content-type") || "";
    return ct.includes("json") ? res.json() : res.text();
  },
  get(p) { return API.request("GET", p); },
  post(p, b) { return API.request("POST", p, b); },
  patch(p, b) { return API.request("PATCH", p, b); },
  del(p) { return API.request("DELETE", p); },
  upload(p, formData) { return API.request("POST", p, formData, true); },
};

// ---------- DOM helpers ----------
function esc(str) {
  const d = document.createElement("div");
  d.textContent = str ?? "";
  return d.innerHTML;
}
function escAttr(str) {
  return esc(str).replace(/"/g, "&quot;");
}
function el(id) { return document.getElementById(id); }

// ---------- Toasts ----------
function toast(message, kind = "info", ms = 3200) {
  const stack = el("toast-stack");
  const node = document.createElement("div");
  node.className = `toast ${kind}`;
  node.textContent = message;
  stack.appendChild(node);
  setTimeout(() => {
    node.classList.add("leaving");
    setTimeout(() => node.remove(), 250);
  }, ms);
}

// ---------- Modal ----------
function openModal(html, onMount) {
  const backdrop = el("modal-backdrop");
  const modal = el("modal");
  modal.innerHTML = html;
  backdrop.classList.add("open");
  if (onMount) onMount(modal);
  const first = modal.querySelector("input, textarea, select");
  if (first) first.focus();
}
function closeModal() {
  el("modal-backdrop").classList.remove("open");
  el("modal").innerHTML = "";
}
document.addEventListener("click", (e) => {
  if (e.target.id === "modal-backdrop") closeModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeModal();
});

// ---------- Dates ----------
function todayISO() {
  // Local date in the browser's zone. The server anchors to OPSDECK_TZ;
  // for a single user in one place these agree.
  const d = new Date();
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
function pad(n) { return String(n).padStart(2, "0"); }

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}
function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso.length <= 10 ? iso + "T00:00:00" : iso);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
function fmtDateLong(iso) {
  const d = new Date(iso.length <= 10 ? iso + "T00:00:00" : iso);
  return d.toLocaleDateString(undefined, {
    weekday: "long", month: "long", day: "numeric", year: "numeric",
  });
}
function daysBetween(aISO, bISO) {
  const a = new Date(aISO + "T00:00:00");
  const b = new Date(bISO + "T00:00:00");
  return Math.round((b - a) / 86400000);
}
function addDaysISO(iso, n) {
  const d = new Date(iso + "T00:00:00");
  d.setDate(d.getDate() + n);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// Overdue / due-soon classification used by both Today and the board.
function dueClass(dueISO) {
  if (!dueISO) return "";
  const today = todayISO();
  const due = dueISO.slice(0, 10);
  if (due < today) return "overdue";
  if (due === today) return "due-today";
  if (daysBetween(today, due) <= 2) return "due-soon";
  return "";
}

const LABEL_COLORS = ["gray", "blue", "teal", "green", "amber", "red", "purple", "pink"];

// ---------- Theming ----------
// A theme's colors map onto the CSS custom properties the stylesheet already
// uses. Applied to :root because the SPA shows one profile at a time (the
// switcher swaps the active profile and re-renders) rather than mounting all
// three panes at once - so a single root scope is correct and simplest.
const THEME_VAR_MAP = {
  bg: "--bg",
  surface: "--panel",
  surface_alt: "--panel-alt",
  border: "--border",
  text: "--text",
  text_muted: "--text-muted",
  accent: "--amber",       // the stylesheet's pervasive highlight colour
  primary: "--blue",       // secondary accent
};

function applyTheme(colors, accentOverride) {
  if (!colors) return;
  const root = document.documentElement;
  for (const [key, cssVar] of Object.entries(THEME_VAR_MAP)) {
    if (colors[key]) root.style.setProperty(cssVar, colors[key]);
  }
  if (accentOverride) root.style.setProperty("--amber", accentOverride);
  // Light themes need dark text on the accent-filled chips/buttons; decide
  // from the background's luminance.
  root.style.setProperty("--on-accent", isLight(colors.bg) ? "#ffffff" : "#10141b");
  root.style.setProperty("color-scheme", isLight(colors.bg) ? "light" : "dark");
}

function isLight(hex) {
  if (!hex) return false;
  const c = hex.replace("#", "");
  if (c.length < 6) return false;
  const r = parseInt(c.slice(0, 2), 16), g = parseInt(c.slice(2, 4), 16), b = parseInt(c.slice(4, 6), 16);
  return (0.299 * r + 0.587 * g + 0.114 * b) > 150;
}

// Resolve and apply the active profile's theme from the loaded theme list.
function applyProfileTheme() {
  const s = window.OPSDECK.settings || {};
  const themes = window.OPSDECK.themes || [];
  const theme = themes.find((t) => t.id === s.theme_id) || themes.find((t) => t.id === "midnight");
  if (theme) applyTheme(theme.colors, s.accent_override);
}
