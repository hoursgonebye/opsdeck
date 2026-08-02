// The skill tree: a pan/zoom SVG canvas of domain-clustered nodes.
//
// Deliberately not auto-laid-out. Node positions are stored coordinates you
// can drag, because a hand-arranged map reads as a place you know, and a
// force-directed graph rearranging itself every load does not. Branches are
// allowed to be disconnected.

let treeData = null;
let treeView = { x: 0, y: 0, scale: 1 };
let treePan = null;
let treeDragNode = null;
let treeFilter = null;

const DOMAIN_COLORS = {
  networking: "blue",
  linux: "amber",
  pentest: "red",
  defense: "teal",
  crypto: "purple",
  grc: "green",
  general: "gray",
};

async function renderTree() {
  const panel = el("panel");
  panel.innerHTML = `<div class="loading">Loading tree…</div>`;

  treeData = await API.get("/tree");
  const domains = [...new Set(treeData.nodes.map((n) => n.domain))].sort();
  const t = treeData.totals;

  panel.innerHTML = `
    <div class="section-head-row">
      <h1 class="section-title">Skill tree</h1>
      <div class="head-actions">
        <button class="btn" id="tree-fit">Fit</button>
        <button class="btn" id="tree-add">+ Node</button>
      </div>
    </div>
    <p class="section-sub">
      ${t.levels} of ${t.max_levels} levels across ${t.nodes} nodes ·
      drag a node to reposition · scroll to zoom
    </p>

    <div class="tree-filters">
      <button class="chip ${!treeFilter ? "on" : ""}" data-domain="">All</button>
      ${domains.map((d) => `
        <button class="chip ${treeFilter === d ? "on" : ""} c-${DOMAIN_COLORS[d] || "gray"}"
                data-domain="${escAttr(d)}">${esc(d)}</button>`).join("")}
    </div>

    <div class="tree-wrap" id="tree-wrap">
      <svg id="tree-svg" class="tree-svg"></svg>
      <div class="tree-hint">scroll or pinch to zoom · drag background to pan</div>
    </div>`;

  panel.querySelectorAll("[data-domain]").forEach((chip) => {
    chip.addEventListener("click", () => {
      treeFilter = chip.dataset.domain || null;
      renderTree();
    });
  });
  el("tree-add").addEventListener("click", () => openNodeEditor(null));
  el("tree-fit").addEventListener("click", fitTree);

  drawTree();
  fitTree();
  attachTreeInteraction();
}

function drawTree() {
  const svg = el("tree-svg");
  const nodes = treeData.nodes;
  const byId = Object.fromEntries(nodes.map((n) => [n.id, n]));

  const dim = (n) => treeFilter && n.domain !== treeFilter;

  const edges = treeData.edges.map((e) => {
    const a = byId[e.from_id], b = byId[e.to_id];
    if (!a || !b) return "";
    // An edge counts as "flowing" once its parent has any level - that's
    // what makes progress visible as a path rather than scattered dots.
    const live = a.level > 0 && b.level > 0;
    const faded = dim(a) || dim(b);
    return `<line class="tree-edge ${live ? "live" : ""} ${faded ? "faded" : ""}"
              x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}"
              data-edge="${e.from_id}-${e.to_id}"/>`;
  }).join("");

  const nodeEls = nodes.map((n) => {
    const color = DOMAIN_COLORS[n.domain] || "gray";
    const pct = n.max_level ? n.level / n.max_level : 0;
    const r = 22 + (n.tier - 1) * 3;
    const circ = 2 * Math.PI * (r + 5);

    return `
      <g class="tree-node c-${color} ${n.level > 0 ? "started" : ""} ${n.locked ? "locked" : ""}
                ${n.pending_attempt ? "pending" : ""} ${dim(n) ? "faded" : ""}"
         data-node="${n.id}" transform="translate(${n.x},${n.y})">
        <circle class="node-ring-bg" r="${r + 5}"/>
        <circle class="node-ring" r="${r + 5}"
                stroke-dasharray="${circ * pct} ${circ}"
                transform="rotate(-90)"/>
        <circle class="node-body" r="${r}"/>
        ${n.locked ? `<path class="node-lock" d="M-4,-1 h8 v6 h-8 z M-2.5,-1 v-2.5 a2.5,2.5 0 0,1 5,0 v2.5"/>` : ""}
        ${n.level > 0 && !n.locked ? `<text class="node-level" y="5">${n.level}</text>` : ""}
        <text class="node-label" y="${r + 20}">${esc(truncate(n.title, 22))}</text>
      </g>`;
  }).join("");

  svg.innerHTML = `
    <defs>
      <filter id="node-glow" x="-70%" y="-70%" width="240%" height="240%">
        <feGaussianBlur stdDeviation="6" result="b"/>
        <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
      </filter>
    </defs>
    <g id="tree-viewport">
      <g id="tree-edges">${edges}</g>
      <g id="tree-nodes">${nodeEls}</g>
    </g>`;

  applyTransform();

  svg.querySelectorAll(".tree-node").forEach((g) => {
    // Pointer events rather than mouse events: one code path covers mouse,
    // touch, and pen, so dragging a node works the same on a phone.
    g.addEventListener("pointerdown", (e) => {
      if (!e.isPrimary) return;
      e.stopPropagation();
      const id = Number(g.dataset.node);
      treeDragNode = {
        id, el: g, moved: false,
        startX: e.clientX, startY: e.clientY,
        origX: treeData.nodes.find((n) => n.id === id).x,
        origY: treeData.nodes.find((n) => n.id === id).y,
      };
    });
  });
}

function truncate(s, n) {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function applyTransform() {
  const vp = el("tree-viewport");
  if (vp) vp.setAttribute("transform",
    `translate(${treeView.x},${treeView.y}) scale(${treeView.scale})`);
}

function fitTree() {
  const wrap = el("tree-wrap");
  if (!wrap || !treeData.nodes.length) return;
  const xs = treeData.nodes.map((n) => n.x), ys = treeData.nodes.map((n) => n.y);
  const pad = 90;
  const minX = Math.min(...xs) - pad, maxX = Math.max(...xs) + pad;
  const minY = Math.min(...ys) - pad, maxY = Math.max(...ys) + pad;
  const w = wrap.clientWidth, h = wrap.clientHeight;
  treeView.scale = Math.min(w / (maxX - minX), h / (maxY - minY), 1.1);
  treeView.x = (w - (maxX - minX) * treeView.scale) / 2 - minX * treeView.scale;
  treeView.y = (h - (maxY - minY) * treeView.scale) / 2 - minY * treeView.scale;
  applyTransform();
}

// Live pointers on the canvas, keyed by pointerId. Two at once means pinch.
const treePointers = new Map();
let treePinch = null;

function zoomAbout(nextScale, cx, cy) {
  const next = Math.max(0.15, Math.min(2.5, nextScale));
  treeView.x = cx - (cx - treeView.x) * (next / treeView.scale);
  treeView.y = cy - (cy - treeView.y) * (next / treeView.scale);
  treeView.scale = next;
  applyTransform();
}

function attachTreeInteraction() {
  const wrap = el("tree-wrap");

  wrap.addEventListener("pointerdown", (e) => {
    treePointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

    // Second finger down: start a pinch and abandon any pan/node drag so the
    // gesture doesn't fight itself.
    if (treePointers.size === 2) {
      const [a, b] = [...treePointers.values()];
      const rect = wrap.getBoundingClientRect();
      treePinch = {
        dist: Math.hypot(a.x - b.x, a.y - b.y),
        scale: treeView.scale,
        cx: (a.x + b.x) / 2 - rect.left,
        cy: (a.y + b.y) / 2 - rect.top,
      };
      treePan = null;
      treeDragNode = null;
      wrap.classList.remove("panning");
      return;
    }

    if (e.target.closest(".tree-node")) return;   // node handler already ran
    if (!e.isPrimary) return;
    wrap.setPointerCapture?.(e.pointerId);
    treePan = { x: e.clientX - treeView.x, y: e.clientY - treeView.y };
    wrap.classList.add("panning");
  });

  wrap.addEventListener("pointermove", (e) => {
    if (!treePointers.has(e.pointerId)) return;
    treePointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

    if (treePinch && treePointers.size === 2) {
      const [a, b] = [...treePointers.values()];
      const dist = Math.hypot(a.x - b.x, a.y - b.y);
      if (treePinch.dist > 0) {
        zoomAbout(treePinch.scale * (dist / treePinch.dist), treePinch.cx, treePinch.cy);
      }
    }
  });

  const endPointer = (e) => {
    treePointers.delete(e.pointerId);
    if (treePointers.size < 2) treePinch = null;
    wrap.releasePointerCapture?.(e.pointerId);
  };
  wrap.addEventListener("pointerup", endPointer);
  wrap.addEventListener("pointercancel", endPointer);

  window.addEventListener("pointermove", onTreeMove);
  window.addEventListener("pointerup", onTreeUp);
  window.addEventListener("pointercancel", onTreeUp);

  wrap.addEventListener("wheel", (e) => {
    e.preventDefault();
    const rect = wrap.getBoundingClientRect();
    const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
    // Zoom toward the cursor rather than the origin.
    zoomAbout(treeView.scale * factor, e.clientX - rect.left, e.clientY - rect.top);
  }, { passive: false });
}

function onTreeMove(e) {
  if (treePinch) return;   // two fingers down: zooming, not dragging
  if (treeDragNode) {
    const dx = (e.clientX - treeDragNode.startX) / treeView.scale;
    const dy = (e.clientY - treeDragNode.startY) / treeView.scale;
    if (Math.abs(dx) > 2 || Math.abs(dy) > 2) treeDragNode.moved = true;
    const nx = treeDragNode.origX + dx, ny = treeDragNode.origY + dy;
    treeDragNode.el.setAttribute("transform", `translate(${nx},${ny})`);
    const node = treeData.nodes.find((n) => n.id === treeDragNode.id);
    node.x = nx; node.y = ny;
    redrawEdgesFor(treeDragNode.id);
    return;
  }
  if (treePan) {
    treeView.x = e.clientX - treePan.x;
    treeView.y = e.clientY - treePan.y;
    applyTransform();
  }
}

function onTreeUp() {
  el("tree-wrap")?.classList.remove("panning");
  treePan = null;
  if (treeDragNode) {
    const { id, moved } = treeDragNode;
    const node = treeData.nodes.find((n) => n.id === id);
    treeDragNode = null;
    if (moved) {
      API.patch(`/tree/nodes/${id}`, { x: Math.round(node.x), y: Math.round(node.y) });
    } else {
      openNodeDetail(id);
    }
  }
}

function redrawEdgesFor(nodeId) {
  const byId = Object.fromEntries(treeData.nodes.map((n) => [n.id, n]));
  treeData.edges.forEach((e) => {
    if (e.from_id !== nodeId && e.to_id !== nodeId) return;
    const line = document.querySelector(`[data-edge="${e.from_id}-${e.to_id}"]`);
    if (!line) return;
    const a = byId[e.from_id], b = byId[e.to_id];
    line.setAttribute("x1", a.x); line.setAttribute("y1", a.y);
    line.setAttribute("x2", b.x); line.setAttribute("y2", b.y);
  });
}

// ---------- Node detail ----------
function openNodeDetail(nodeId) {
  const n = treeData.nodes.find((x) => x.id === nodeId);
  if (!n) return;

  const attrNames = Object.fromEntries(treeData.attributes.map((a) => [a.key, a.name]));
  const weights = n.weights.map((w) => `
    <div class="weight-row">
      <span>${esc(attrNames[w.attribute_key] || w.attribute_key)}</span>
      <div class="weight-bar"><div style="width:${Math.min(w.weight, 1) * 100}%"></div></div>
      <span class="card-meta">${w.weight.toFixed(1)}</span>
    </div>`).join("") || '<span class="empty-state">No attributes mapped.</span>';

  const pips = Array.from({ length: n.max_level }, (_, i) =>
    `<span class="pip ${i < n.level ? "on" : ""}"></span>`).join("");

  let action;
  if (n.locked) {
    action = `<div class="lock-note">Locked until ${esc(n.unlock_attr)} reaches ${n.unlock_value}</div>`;
  } else if (n.level >= n.max_level) {
    action = `<div class="lock-note maxed">Mastered — level ${n.level}/${n.max_level}</div>`;
  } else if (n.pending_attempt) {
    action = `<button class="btn primary" id="resume-attempt">Resume verification</button>`;
  } else {
    action = `<button class="btn primary" id="start-levelup">Level up — prove it</button>`;
  }

  openModal(`
    <div class="modal-head">
      <div>
        <h2 class="modal-title">${esc(n.title)}</h2>
        <span class="chip c-${DOMAIN_COLORS[n.domain] || "gray"} static">${esc(n.domain)}</span>
        <span class="card-meta">tier ${n.tier}</span>
      </div>
      <button class="icon-btn" onclick="closeModal()">×</button>
    </div>

    <div class="level-pips">${pips}<span class="card-meta">${n.level}/${n.max_level}</span>
      ${n.rejected_attempts ? `<span class="chip static c-red">${n.rejected_attempts} failed</span>` : ""}
    </div>

    ${n.description ? `<p class="node-desc">${esc(n.description)}</p>` : ""}

    <label class="field-label">Feeds attributes</label>
    ${weights}

    ${n.xp_next ? `<p class="card-meta" style="margin-top:14px">Next level is worth ${n.xp_next} XP</p>` : ""}

    <div class="modal-actions">
      <button class="btn" id="edit-node">Edit</button>
      ${action}
    </div>
  `, () => {
    el("edit-node").addEventListener("click", () => openNodeEditor(n));
    el("start-levelup")?.addEventListener("click", () => startLevelUp(n.id));
    el("resume-attempt")?.addEventListener("click", () => openAttempt(n.pending_attempt));
  });
}

// ---------- Node editor ----------
function openNodeEditor(node) {
  const n = node || {
    title: "", description: "", domain: "general", tier: 1,
    max_level: 5, weights: [], unlock_attr: "", unlock_value: "",
    x: Math.round(-treeView.x / treeView.scale + 300),
    y: Math.round(-treeView.y / treeView.scale + 200),
  };
  const attrs = treeData.attributes;
  const wMap = Object.fromEntries((n.weights || []).map((w) => [w.attribute_key, w.weight]));

  openModal(`
    <div class="modal-head">
      <h2 class="modal-title">${node ? "Edit node" : "New node"}</h2>
      <button class="icon-btn" onclick="closeModal()">×</button>
    </div>

    <label class="field-label">Title</label>
    <input type="text" id="nd-title" value="${escAttr(n.title)}">

    <label class="field-label">Description</label>
    <textarea id="nd-desc" class="modal-textarea" rows="2">${esc(n.description)}</textarea>

    <div class="field-row">
      <div>
        <label class="field-label">Domain</label>
        <select id="nd-domain">
          ${Object.keys(DOMAIN_COLORS).map((d) =>
            `<option value="${d}" ${n.domain === d ? "selected" : ""}>${d}</option>`).join("")}
        </select>
      </div>
      <div>
        <label class="field-label">Tier (1–5, drives difficulty)</label>
        <input type="number" id="nd-tier" min="1" max="5" value="${n.tier}">
      </div>
    </div>

    <label class="field-label">Attribute weights</label>
    <div class="weight-editor">
      ${attrs.map((a) => `
        <div class="weight-edit-row">
          <span>${esc(a.name)}</span>
          <input type="range" min="0" max="1" step="0.1"
                 data-attr="${a.key}" value="${wMap[a.key] ?? 0}">
          <span class="weight-val">${(wMap[a.key] ?? 0).toFixed(1)}</span>
        </div>`).join("")}
    </div>

    <div class="field-row">
      <div>
        <label class="field-label">Unlock gated by</label>
        <select id="nd-unlock-attr">
          <option value="">Not gated</option>
          ${attrs.map((a) => `
            <option value="${a.key}" ${n.unlock_attr === a.key ? "selected" : ""}>${esc(a.name)}</option>`).join("")}
        </select>
      </div>
      <div>
        <label class="field-label">Threshold</label>
        <input type="number" id="nd-unlock-val" step="0.5" value="${n.unlock_value ?? ""}">
      </div>
    </div>

    <div class="modal-actions">
      ${node ? `<button class="btn danger" id="nd-delete">Delete</button>` : ""}
      <button class="btn primary" id="nd-save">Save</button>
    </div>
  `, (modal) => {
    modal.querySelectorAll('input[type="range"]').forEach((r) => {
      r.addEventListener("input", () => {
        r.nextElementSibling.textContent = Number(r.value).toFixed(1);
      });
    });

    if (node) {
      el("nd-delete").addEventListener("click", async () => {
        if (!confirm(`Delete "${node.title}"? Its level history goes too.`)) return;
        await API.del(`/tree/nodes/${node.id}`);
        closeModal();
        renderTree();
      });
    }

    el("nd-save").addEventListener("click", async () => {
      const weights = [...modal.querySelectorAll('input[type="range"]')]
        .filter((r) => Number(r.value) > 0)
        .map((r) => ({ attribute_key: r.dataset.attr, weight: Number(r.value) }));

      const payload = {
        title: el("nd-title").value || "Untitled",
        description: el("nd-desc").value,
        domain: el("nd-domain").value,
        tier: Number(el("nd-tier").value),
        unlock_attr: el("nd-unlock-attr").value || null,
        unlock_value: el("nd-unlock-val").value ? Number(el("nd-unlock-val").value) : null,
        weights,
      };
      if (node) await API.patch(`/tree/nodes/${node.id}`, payload);
      else await API.post("/tree/nodes", { ...payload, x: n.x, y: n.y });
      closeModal();
      toast("Saved");
      renderTree();
    });
  });
}

// ---------- Level-up animation ----------
// One brief pulse on the node and a light travelling its parent edges.
// Skipped entirely under prefers-reduced-motion.
function celebrateLevelUp(nodeId) {
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduced) return;

  const g = document.querySelector(`.tree-node[data-node="${nodeId}"]`);
  if (g) {
    g.classList.add("levelled");
    setTimeout(() => g.classList.remove("levelled"), 1400);
  }

  treeData.edges.filter((e) => e.to_id === nodeId).forEach((e) => {
    const line = document.querySelector(`[data-edge="${e.from_id}-${e.to_id}"]`);
    if (!line) return;
    const trail = line.cloneNode();
    trail.classList.add("edge-trail");
    trail.classList.remove("tree-edge", "faded");
    line.parentNode.appendChild(trail);
    const len = Math.hypot(
      line.x2.baseVal.value - line.x1.baseVal.value,
      line.y2.baseVal.value - line.y1.baseVal.value
    );
    trail.style.strokeDasharray = `${len * 0.25} ${len}`;
    trail.style.strokeDashoffset = len * 1.25;
    requestAnimationFrame(() => {
      trail.style.transition = "stroke-dashoffset 0.85s cubic-bezier(.4,0,.2,1), opacity .3s ease-out .55s";
      trail.style.strokeDashoffset = "0";
      trail.style.opacity = "0";
    });
    setTimeout(() => trail.remove(), 1200);
  });
}
