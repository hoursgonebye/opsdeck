// Profiles: the tab bar that switches whose dashboard you're looking at,
// per-profile theming, and the Settings section.
//
// Switching a profile changes window.OPSDECK.activeProfile (which the API
// client sends as X-Profile-Id on every call), reloads that profile's
// settings, re-themes the page, and rebuilds the nav from the profile's
// enabled_modules. The whole app is one profile at a time.

const ALL_MODULES = [
  { key: "today", label: "Today", section: "today" },
  { key: "boards", label: "Boards", section: "board" },
  { key: "calendar", label: "Calendar", section: "calendar" },
  { key: "routines", label: "Routines", section: "routines" },
  { key: "docs", label: "Docs", section: "docs" },
  { key: "tree", label: "Skill tree", section: "tree" },
  { key: "thm", label: "TryHackMe", section: "thm" },
  { key: "growth", label: "Growth", section: "growth" },
  // "chat" is deliberately absent: the mentor is the floating dock in the
  // bottom-right corner now, present on every section rather than a tab.
  { key: "health", label: "Health", section: "health" },
  { key: "finance", label: "Finance", section: "finance" },
  { key: "academics", label: "Academics", section: "academics" },
  { key: "printer", label: "Printer", section: "printer" },
  { key: "govee", label: "Lights", section: "govee" },
  { key: "homelab", label: "Homelab", section: "homelab" },
  { key: "joint", label: "Us", section: "joint" },
];

// ---------- Device binding ----------
// A device claims a profile once ("whose device is this?") and from then on
// sees only that person plus Us - his phone shows the owner + Us, hers shows
// the partner + Us. Push registrations follow the claim, so a ping to her never
// buzzes his pocket. Stored per-device in localStorage; changeable in
// Settings. This is separation, not security: the household shares one API
// token by design (ARCHITECTURE §3) - the boundary is the tailnet.

function deviceProfile() {
  const v = localStorage.getItem("opsdeck-device-profile");
  return v && v !== "none" ? v : null;
}

function visibleProfiles() {
  const all = window.OPSDECK.profiles || [];
  const bound = deviceProfile();
  if (!bound || !all.some((p) => p.id === bound)) return all;
  return all.filter((p) => p.id === bound || p.type === "joint");
}

function maybeOfferDeviceClaim() {
  if (localStorage.getItem("opsdeck-device-profile")) return;
  const persons = (window.OPSDECK.profiles || []).filter((p) => p.type !== "joint");
  if (persons.length < 2) return;

  openModal(`
    <div class="modal-head">
      <h2 class="modal-title">Whose device is this?</h2>
    </div>
    <p class="settings-hint">Pick once and this device shows only that person
    plus Us — and notifications for them arrive here. Changeable any time in
    Settings.</p>
    <div class="claim-grid">
      ${persons.map((p) => `
        <button class="btn primary claim-btn" data-claim="${p.id}">
          <span class="profile-avatar type-${esc(p.type)}">${esc(initials(p.display_name))}</span>
          ${esc(p.display_name)}
        </button>`).join("")}
      <button class="btn" data-claim="none">Skip — show everyone</button>
    </div>
  `, (modal) => {
    modal.querySelectorAll("[data-claim]").forEach((b) =>
      b.addEventListener("click", async () => {
        const choice = b.dataset.claim;
        localStorage.setItem("opsdeck-device-profile", choice);
        closeModal();
        if (choice !== "none") {
          if (window.OPSDECK.activeProfile !== choice) {
            await switchProfile(choice);
          } else {
            renderProfileBar();
          }
          // Claim this device's push registration for that profile too.
          if (typeof ensurePushSubscription === "function"
              && Notification.permission === "granted") {
            ensurePushSubscription(true);
          }
          toast("This device now follows " +
            (window.OPSDECK.profiles.find((p) => p.id === choice)?.display_name || choice));
        }
      }));
  });
}

async function bootstrapProfiles() {
  const [profiles, themes] = await Promise.all([
    API.get("/profiles"),
    API.get("/themes"),
  ]);
  window.OPSDECK.profiles = profiles;
  window.OPSDECK.themes = themes;

  const bound = deviceProfile();
  const saved = localStorage.getItem("opsdeck-active-profile");
  const allowed = visibleProfiles();
  const active = allowed.find((p) => p.id === saved) ? saved
    : (bound && allowed.some((p) => p.id === bound)) ? bound : allowed[0]?.id || "primary";
  window.OPSDECK.activeProfile = active;

  await loadActiveSettings();
  renderProfileBar();
  maybeOfferDeviceClaim();
}

async function loadActiveSettings() {
  window.OPSDECK.settings = await API.get(`/profiles/${window.OPSDECK.activeProfile}/settings`);
  applyProfileTheme();
  buildNav();
}

function renderProfileBar() {
  const active = window.OPSDECK.activeProfile;
  const profiles = visibleProfiles();

  const bar = el("profile-bar");
  if (bar) {
    bar.innerHTML = profiles.map((p) => `
      <button class="profile-tab ${p.id === active ? "active" : ""}" data-profile="${p.id}">
        <span class="profile-avatar type-${esc(p.type)}">${esc(initials(p.display_name))}</span>
        <span class="profile-name">${esc(p.display_name)}</span>
      </button>`).join("");
    bar.querySelectorAll(".profile-tab").forEach((btn) =>
      btn.addEventListener("click", () => switchProfile(btn.dataset.profile)));
  }

  // Mobile top bar: avatars only, no names - the bar has to stay one line.
  const mb = el("mb-profiles");
  if (mb) {
    mb.innerHTML = profiles.map((p) => `
      <span class="profile-avatar type-${esc(p.type)} ${p.id === active ? "active" : ""}"
            data-profile="${p.id}" role="button" tabindex="0"
            title="${escAttr(p.display_name)}">${esc(initials(p.display_name))}</span>`).join("");
    mb.querySelectorAll("[data-profile]").forEach((a) => {
      const go = () => switchProfile(a.dataset.profile);
      a.addEventListener("click", go);
      a.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); }
      });
    });
  }
}

function initials(name) {
  return (name || "?").trim().slice(0, 2).toUpperCase();
}

async function switchProfile(id) {
  if (id === window.OPSDECK.activeProfile) return;
  // A bound device only switches between its person and Us.
  if (!visibleProfiles().some((p) => p.id === id)) return;
  window.OPSDECK.activeProfile = id;
  localStorage.setItem("opsdeck-active-profile", id);
  renderProfileBar();
  await loadActiveSettings();
  // Land on the profile's first enabled section (joint profiles open on Us).
  const first = enabledSections()[0] || "today";
  go(first);
}

function enabledModules() {
  const s = window.OPSDECK.settings || {};
  return s.enabled_modules || ["today", "boards", "calendar", "routines", "docs"];
}

function enabledSections() {
  const set = new Set(enabledModules());
  return ALL_MODULES.filter((m) => set.has(m.key)).map((m) => m.section);
}

// Rebuild the sidebar nav to show only this profile's enabled modules,
// always plus Settings at the foot.
function buildNav() {
  const tree = el("node-tree");
  if (!tree) return;
  const set = new Set(enabledModules());
  const items = ALL_MODULES.filter((m) => set.has(m.key));

  tree.innerHTML = items.map((m, i) => {
    const last = i === items.length - 1;
    const badge = m.section === "growth"
      ? `<span class="nav-badge" id="nav-badge" hidden></span>` : "";
    const notif = m.section === "joint"
      ? `<span class="nav-badge" id="joint-badge" hidden></span>` : "";
    return `<li data-section="${m.section}" class="node">
      <span class="node-bullet">${last ? "└─" : "├─"}</span> ${m.label}${badge}${notif}</li>`;
  }).join("") +
    `<li class="node-divider"></li>
     <li data-section="settings" class="node"><span class="node-bullet">⚙</span> Settings</li>`;

  // Re-mark active (buildNav wipes the active class).
  tree.querySelectorAll(".node").forEach((n) =>
    n.classList.toggle("active", n.dataset.section === activeSection));
}

// ---------- Settings section ----------
async function renderSettings() {
  const panel = el("panel");
  panel.innerHTML = `<div class="loading">Loading…</div>`;

  const pid = window.OPSDECK.activeProfile;
  const [settings, profile, themes] = await Promise.all([
    API.get(`/profiles/${pid}/settings`),
    Promise.resolve(window.OPSDECK.profiles.find((p) => p.id === pid)),
    API.get("/themes"),
  ]);
  window.OPSDECK.themes = themes;

  const moduleSet = new Set(settings.enabled_modules || []);
  const toggleable = ALL_MODULES.filter((m) => m.key !== "today");

  panel.innerHTML = `
    <h1 class="section-title">Settings</h1>
    <p class="section-sub">${esc(profile.display_name)} · ${esc(profile.type)} profile</p>

    <div class="settings-block">
      <h2 class="block-title">Display name</h2>
      <div class="field-row-inline">
        <input type="text" id="set-name" value="${escAttr(profile.display_name)}">
        <button class="btn" id="set-name-save">Save</button>
      </div>
    </div>

    <div class="settings-block">
      <h2 class="block-title">Theme</h2>
      <div class="theme-grid" id="theme-grid">
        ${themes.map((t) => `
          <button class="theme-swatch ${t.id === settings.theme_id ? "active" : ""}" data-theme="${escAttr(t.id)}"
                  title="${escAttr(t.name)}">
            <span class="sw sw-bg" style="background:${esc(t.colors.bg)}"></span>
            <span class="sw sw-surface" style="background:${esc(t.colors.surface)}"></span>
            <span class="sw sw-accent" style="background:${esc(t.colors.accent)}"></span>
            <span class="theme-name">${esc(t.name)}</span>
          </button>`).join("")}
      </div>
    </div>

    <div class="settings-block">
      <h2 class="block-title">Modules on this tab</h2>
      <p class="settings-hint">Turn a whole section off for this profile. Today is always on.</p>
      <div class="module-toggles">
        ${toggleable.map((m) => `
          <label class="toggle-row">
            <input type="checkbox" data-module="${m.key}" ${moduleSet.has(m.key) ? "checked" : ""}>
            <span>${esc(m.label)}</span>
          </label>`).join("")}
      </div>
    </div>

    <div class="settings-block">
      <h2 class="block-title">Week starts on</h2>
      <select id="set-weekstart">
        <option value="monday" ${settings.week_start === "monday" ? "selected" : ""}>Monday</option>
        <option value="sunday" ${settings.week_start === "sunday" ? "selected" : ""}>Sunday</option>
      </select>
    </div>

    <div class="settings-block">
      <h2 class="block-title">This device</h2>
      <p class="settings-hint">A bound device shows only that person plus Us,
      and their notifications arrive here. Separation for convenience, not a
      lock — the household shares one instance.</p>
      <select id="set-device">
        <option value="none" ${!deviceProfile() ? "selected" : ""}>Not bound — show everyone</option>
        ${window.OPSDECK.profiles.filter((p) => p.type !== "joint").map((p) => `
          <option value="${p.id}" ${deviceProfile() === p.id ? "selected" : ""}>
            ${esc(p.display_name)}'s device</option>`).join("")}
      </select>
    </div>`;

  el("set-name-save").addEventListener("click", async () => {
    await API.patch(`/profiles/${pid}`, { display_name: el("set-name").value.trim() || profile.display_name });
    window.OPSDECK.profiles = await API.get("/profiles");
    renderProfileBar();
    toast("Name saved");
  });

  panel.querySelectorAll("[data-theme]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const themeId = btn.dataset.theme;
      const merged = await API.patch(`/profiles/${pid}/settings`, { theme_id: themeId });
      window.OPSDECK.settings = merged;
      applyProfileTheme();
      panel.querySelectorAll(".theme-swatch").forEach((s) =>
        s.classList.toggle("active", s.dataset.theme === themeId));
    });
  });

  panel.querySelectorAll("[data-module]").forEach((cb) => {
    cb.addEventListener("change", async () => {
      const on = new Set(settings.enabled_modules);
      cb.checked ? on.add(cb.dataset.module) : on.delete(cb.dataset.module);
      if (!on.has("today")) on.add("today");
      const list = ALL_MODULES.map((m) => m.key).filter((k) => on.has(k));
      settings.enabled_modules = list;
      const merged = await API.patch(`/profiles/${pid}/settings`, { enabled_modules: list });
      window.OPSDECK.settings = merged;
      buildNav();
      toast("Modules updated");
    });
  });

  el("set-weekstart").addEventListener("change", async (e) => {
    await API.patch(`/profiles/${pid}/settings`, { week_start: e.target.value });
    toast("Saved");
  });

  el("set-device").addEventListener("change", async (e) => {
    localStorage.setItem("opsdeck-device-profile", e.target.value);
    if (e.target.value !== "none" && Notification.permission === "granted"
        && typeof ensurePushSubscription === "function") {
      await ensurePushSubscription(true);   // move this device's push too
    }
    toast("Device binding saved — reloading");
    setTimeout(() => location.reload(), 600);
  });
}
