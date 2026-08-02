// Growth: weekly XP trend, the attribute radar, and the proposal inbox.
//
// Both charts are hand-drawn SVG rather than a charting library - two shapes
// don't justify a dependency, and it keeps the container free of CDN calls.

const XP_SOURCE_COLORS = {
  skills: "amber",
  cards: "blue",
  routines: "teal",
  checklist: "purple",
};

async function renderGrowth() {
  const panel = el("panel");
  panel.innerHTML = `<div class="loading">Loading…</div>`;

  const [xp, attrs, status] = await Promise.all([
    API.get("/xp?weeks=12"),
    API.get("/attributes?weeks=12"),
    API.get("/mentor/status"),
  ]);

  const cur = xp.current;
  const delta = cur?.delta_pct;
  const deltaHtml = delta === null || delta === undefined
    ? `<span class="card-meta">no baseline yet</span>`
    : `<span class="delta ${delta >= 0 ? "up" : "down"}">
         ${delta >= 0 ? "+" : ""}${delta}% vs 4-week average
       </span>`;

  panel.innerHTML = `
    <div class="section-head-row">
      <h1 class="section-title">Growth</h1>
      <div class="head-actions">
        <span class="chip static ${status.direct_mode ? "c-teal" : "c-gray"}">
          mentor: ${status.direct_mode ? "direct" : "queue"}
        </span>
      </div>
    </div>
    <p class="section-sub">Derived from what you actually did — nothing here is logged by hand.</p>

    <div class="growth-grid">
      <div class="today-block">
        <div class="block-title-row">
          <h2 class="block-title">This week</h2>
          ${deltaHtml}
        </div>
        <div class="xp-total">${cur ? cur.total.toLocaleString() : 0}<span>XP</span></div>
        <div class="xp-breakdown">
          ${Object.entries(cur?.sources || {}).filter(([, v]) => v > 0).map(([k, v]) => `
            <div class="xp-source">
              <span class="dot dot-${XP_SOURCE_COLORS[k]}"></span>
              <span class="xp-source-name">${esc(k)}</span>
              <span class="card-meta">${cur.counts[k]} × </span>
              <span class="xp-source-val">${v}</span>
            </div>`).join("") || '<span class="empty-state">Nothing logged yet this week.</span>'}
        </div>
      </div>

      <div class="today-block">
        <h2 class="block-title">Attribute shape</h2>
        ${radarChart(attrs.current, attrs.history)}
        <div class="radar-legend">
          ${attrs.current.map((a) => `
            <span class="legend-item">
              <span class="dot dot-${esc(a.color)}"></span>${esc(a.name)}
              <span class="card-meta">${a.value}</span>
            </span>`).join("")}
        </div>
      </div>
    </div>

    <div class="today-block">
      <h2 class="block-title">Last 12 weeks</h2>
      ${xpChart(xp.weeks)}
    </div>

    <div class="today-block">
      <div class="block-title-row">
        <h2 class="block-title">Mentor proposals</h2>
        <span class="count">${status.pending_proposals} pending</span>
      </div>
      <div id="proposal-list"><div class="loading">Loading…</div></div>
    </div>`;

  renderProposalsInto(el("proposal-list"));
}

// ---------- Weekly XP bars ----------
function xpChart(weeks) {
  const W = 720, H = 200, pad = { l: 34, r: 10, t: 12, b: 26 };
  const max = Math.max(...weeks.map((w) => w.total), 100);
  const bw = (W - pad.l - pad.r) / weeks.length;

  const bars = weeks.map((w, i) => {
    const x = pad.l + i * bw;
    let y = H - pad.b;
    // Stack sources so a tall week visibly shows what it was made of.
    const segs = ["routines", "checklist", "cards", "skills"].map((k) => {
      const v = w.sources[k] || 0;
      if (!v) return "";
      const h = (v / max) * (H - pad.t - pad.b);
      y -= h;
      return `<rect class="xp-bar ${XP_SOURCE_COLORS[k]}" x="${x + bw * 0.18}" y="${y}"
                width="${bw * 0.64}" height="${h}" rx="2">
                <title>${k}: ${v} XP</title></rect>`;
    }).join("");

    const label = w.week_start.slice(5).replace("-", "/");
    return `${segs}
      <text class="xp-tick ${w.is_current ? "now" : ""}" x="${x + bw / 2}" y="${H - 8}"
            text-anchor="middle">${label}</text>`;
  }).join("");

  // Trailing-average line so a single off week reads as variance, not decline.
  const pts = weeks.map((w, i) => {
    if (!w.trailing_avg) return null;
    const x = pad.l + i * bw + bw / 2;
    const y = H - pad.b - (w.trailing_avg / max) * (H - pad.t - pad.b);
    return `${x},${y}`;
  }).filter(Boolean);

  return `<svg viewBox="0 0 ${W} ${H}" class="chart">
    <line class="axis" x1="${pad.l}" y1="${H - pad.b}" x2="${W - pad.r}" y2="${H - pad.b}"/>
    <text class="xp-tick" x="4" y="${pad.t + 8}">${max}</text>
    ${bars}
    ${pts.length > 1 ? `<polyline class="avg-line" points="${pts.join(" ")}"/>` : ""}
  </svg>`;
}

// ---------- Attribute radar ----------
function radarChart(current, history) {
  const size = 260, cx = size / 2, cy = size / 2, R = 92;
  const n = current.length;
  if (!n) return `<p class="empty-state">No attributes defined.</p>`;

  const max = Math.max(...current.map((a) => a.value), 5);
  const angle = (i) => (Math.PI * 2 * i) / n - Math.PI / 2;
  const pt = (i, r) => [cx + Math.cos(angle(i)) * r, cy + Math.sin(angle(i)) * r];

  const rings = [0.25, 0.5, 0.75, 1].map((f) =>
    `<polygon class="radar-ring" points="${
      current.map((_, i) => pt(i, R * f).join(",")).join(" ")}"/>`).join("");

  const spokes = current.map((_, i) =>
    `<line class="radar-spoke" x1="${cx}" y1="${cy}" x2="${pt(i, R)[0]}" y2="${pt(i, R)[1]}"/>`).join("");

  const shape = (vals) =>
    current.map((a, i) => pt(i, (vals[a.key] || 0) / max * R).join(",")).join(" ");

  // Ghost of four weeks ago, so the shape's change over time is visible
  // rather than just its current state.
  const past = history.length > 4 ? history[history.length - 5].attributes : null;
  const ghost = past
    ? `<polygon class="radar-ghost" points="${shape(past)}"/>` : "";

  const nowVals = Object.fromEntries(current.map((a) => [a.key, a.value]));

  const labels = current.map((a, i) => {
    const [x, y] = pt(i, R + 20);
    const anchor = Math.abs(x - cx) < 12 ? "middle" : x > cx ? "start" : "end";
    return `<text class="radar-label" x="${x}" y="${y + 4}" text-anchor="${anchor}">${esc(a.name)}</text>`;
  }).join("");

  const dots = current.map((a, i) => {
    const [x, y] = pt(i, (a.value / max) * R);
    return `<circle class="radar-dot dot-${esc(a.color)}" cx="${x}" cy="${y}" r="3"/>`;
  }).join("");

  return `<svg viewBox="0 0 ${size} ${size}" class="radar">
    ${rings}${spokes}${ghost}
    <polygon class="radar-shape" points="${shape(nowVals)}"/>
    ${dots}${labels}
  </svg>
  ${past ? `<p class="card-meta radar-note">Faint outline is 4 weeks ago</p>` : ""}`;
}
