# Frontend

~3,500 lines of plain JavaScript across 14 files. No framework, no build
step, no bundler. `templates/index.html` loads them in order as classic
`<script>` tags sharing one global scope.

---

## The contract every section follows

One file per section, each exporting a single `renderX()` that:

1. paints a loading state into `#panel`
2. fetches what it needs (`Promise.all` for parallel calls)
3. builds one HTML string and assigns it to `panel.innerHTML`
4. attaches event handlers to the freshly-created nodes

```js
async function renderRoutines() {
  const panel = el("panel");
  panel.innerHTML = `<div class="loading">Loading…</div>`;

  const data = await API.get(`/routines?date=${todayISO()}`);

  panel.innerHTML = `
    <h1 class="section-title">Routines</h1>
    ${data.routines.map(rowHtml).join("")}`;

  panel.querySelectorAll(".routine-row").forEach((row) =>
    row.addEventListener("click", async () => {
      await API.post(`/routines/${row.dataset.routine}/toggle`, {});
      renderRoutines();            // re-render the whole section
    }));
}
```

**Mutations re-render the entire section.** There is no diffing and no
reactive state. At this data size a full rebuild is sub-millisecond, and it
eliminates every stale-state bug — the DOM is always a pure function of the
last fetch.

---

## Load order

Order matters: `core.js` must be first (everything uses `API`, `el`, `esc`),
and `main.js` must be last (it boots the app).

```
core.js       API client, DOM helpers, modal, toasts, dates, theming
today.js      board.js  calendar.js  routines.js  docs.js
skilltree.js  mentor.js  thm.js  growth.js  chat.js
profiles.js   profile switcher, per-profile nav, Settings
joint.js      the "Us" tab
main.js       routing, search, notifications, boot()
```

---

## Boot sequence

```js
async function boot() {
  await bootstrapProfiles();   // GET /profiles + /themes, pick active,
                               // load its settings, apply theme, build nav
  go(startSection);            // render the first enabled section
  setupNotifications();
  refreshJointBadge();
}
```

Profiles load **first** because the API client needs an active profile to
scope every subsequent call, and the sidebar is generated from that
profile's `enabled_modules`. If the bootstrap fails, it logs a warning and
falls back to the static nav in `index.html` so the app still works.

---

## Routing

`main.js` holds a flat map of section → renderer:

```js
const SECTIONS = {
  today, board, calendar, routines, docs,
  tree, thm, growth, chat, joint, settings,
};

function go(section) {
  activeSection = section;
  document.querySelectorAll(".node").forEach((n) =>
    n.classList.toggle("active", n.dataset.section === section));
  location.hash = section;
  SECTIONS[section]();
}
```

Navigation is **event delegation on `#node-tree`**, not per-item listeners.
That matters: `buildNav()` replaces the nav's `innerHTML` whenever the
profile changes, and a delegated listener on the surviving parent keeps
working.

`hashchange` is also wired, so back/forward and deep links work.

---

## Profiles and theming

### Switching

```js
async function switchProfile(id) {
  window.OPSDECK.activeProfile = id;         // API client reads this
  localStorage.setItem("opsdeck-active-profile", id);
  renderProfileBar();
  await loadActiveSettings();                // re-theme + rebuild nav
  go(enabledSections()[0]);
}
```

`core.js` attaches the header on every request:

```js
if (window.OPSDECK.activeProfile) {
  opts.headers["X-Profile-Id"] = window.OPSDECK.activeProfile;
}
```

### Theming

Themes are colour sets applied to the CSS custom properties the stylesheet
already uses:

```js
const THEME_VAR_MAP = {
  bg: "--bg",            surface: "--panel",
  surface_alt: "--panel-alt", border: "--border",
  text: "--text",        text_muted: "--text-muted",
  accent: "--amber",     primary: "--blue",
};
```

`applyTheme()` sets them on `document.documentElement`. It also computes the
background's luminance and flips `color-scheme` plus `--on-accent`, so light
themes get dark text on accent-filled buttons and the browser renders its own
widgets (date pickers, select popups) to match.

> The original spec suggested scoping variables to a per-tab wrapper element.
> This SPA shows **one profile at a time** — switching re-renders rather than
> revealing a hidden pane — so a single `:root` scope is both correct and
> simpler. If all three tabs were ever mounted simultaneously, this is the
> one place that would need to change.

### enabled_modules

`buildNav()` renders the sidebar from `ALL_MODULES` filtered by the
profile's `enabled_modules`, then appends Settings. Turning a section off is
a settings edit, not a code branch.

Adding a section means three edits: a new `renderX()` file, an entry in
`SECTIONS` (main.js), and an entry in `ALL_MODULES` (profiles.js).

---

## Shared helpers (core.js)

| Helper | Purpose |
|---|---|
| `API.get/post/patch/del/upload` | Fetch wrapper; injects both headers, toasts errors, throws |
| `el(id)` | `getElementById` |
| `esc(str)` | HTML-escape via `textContent` — **use on every interpolated value** |
| `escAttr(str)` | `esc` plus quote-escaping, for attribute positions |
| `toast(msg, kind, ms)` | Transient notification |
| `openModal(html, onMount)` / `closeModal()` | Modal; `onMount` receives the element to wire handlers |
| `todayISO()`, `addDaysISO()`, `daysBetween()` | Local-date maths |
| `fmtDate`, `fmtDateLong`, `fmtTime` | Locale display |
| `dueClass(iso)` | `overdue` / `due-today` / `due-soon` |
| `applyTheme`, `applyProfileTheme` | Theming |

### Escaping

Everything is built by string interpolation, so **every value from the API
must pass through `esc()` or `escAttr()`**. `esc()` works by round-tripping
through `textContent`, so it can't be bypassed by clever input.

```js
`<div title="${escAttr(c.title)}">${esc(c.title)}</div>`
```

The markdown renderer in `docs.js` escapes *first*, then applies its own
formatting, so no raw HTML from a doc reaches the page. Uploaded HTML docs
are the deliberate exception and are isolated in a sandboxed iframe — see
[ARCHITECTURE §9](ARCHITECTURE.md#the-docs-iframe-sandbox).

---

## Notable implementation details

**Skill tree (`skilltree.js`)** uses Pointer Events, not mouse events, so one
code path covers mouse, touch and pen. Two live pointers switch it into
pinch-zoom. `.tree-wrap` sets `touch-action: none` because the canvas owns
its own gestures — without it, dragging the tree scrolls the page instead.
With ~450 nodes, `fitTree()` matters on first paint.

**Calendar (`calendar.js`)** expands multi-day events across every day they
cover, tagging each cell `span-start` / `span-mid` / `span-end` so CSS can
draw one continuous bar. `.cal-cell { min-width: 0 }` is load-bearing: grid
children default to `min-width: auto`, so one long unbreakable title would
otherwise stretch its column and warp the whole month.

**Chat (`chat.js`)** keeps the transcript in memory and the session id in
`localStorage`, so switching sections and back doesn't lose the conversation.
Errors are pushed into the transcript rather than only toasted, so a failed
turn is visible in context.

**Today (`today.js`)** renders unfiled quick notes with the destination the
heuristic *would* have chosen, so filing is one glance and one tap.

---

## CSS

One file, ~1,050 lines, organised: tokens → form controls → shell → sections
→ responsive. All colour goes through custom properties in `:root` (that's
what makes theming work).

Deliberate choices worth knowing:

- **Form controls are styled globally**, not per-context. They used to be
  styled only inside modals and the sidebar, which left inputs elsewhere
  rendering as white boxes on a dark page.
- **`color-scheme: dark`** makes the browser render its *own* widgets dark.
- **Autofill override** uses a 1000px inset box-shadow — Chrome hardcodes a
  light autofill background that `background` cannot touch.
- **`overflow-x: hidden` + `overscroll-behavior: none`** on `html, body`
  stop the whole page sliding around on touch. Wide content (the month grid,
  code blocks) scrolls inside its own container instead.
