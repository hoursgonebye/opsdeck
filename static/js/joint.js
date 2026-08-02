// The "Us" tab - the shared/relationship layer. Everything here talks to
// /api/joint/* and is household-wide, not scoped to the active profile.
//
// One section with an internal sub-nav (Home / Wall / Mailbox / Plans /
// Q&A) so all the fun-layer features live together without a dozen sidebar
// entries.

let jointTab = "home";

async function renderJoint() {
  const panel = el("panel");
  panel.innerHTML = `
    <div class="section-head-row">
      <h1 class="section-title">Us</h1>
      <div class="joint-subnav" id="joint-subnav">
        ${["home", "wall", "mailbox", "plans", "q&a"].map((t) =>
          `<button class="board-tab ${t === jointTab ? "active" : ""}" data-jtab="${t}">${t}</button>`).join("")}
      </div>
    </div>
    <div id="joint-body"><div class="loading">Loading…</div></div>`;

  panel.querySelectorAll("[data-jtab]").forEach((b) =>
    b.addEventListener("click", () => { jointTab = b.dataset.jtab; renderJoint(); }));

  const body = el("joint-body");
  try {
    if (jointTab === "home") await jointHome(body);
    else if (jointTab === "wall") await jointWall(body);
    else if (jointTab === "mailbox") await jointMailbox(body);
    else if (jointTab === "plans") await jointPlans(body);
    else await jointQA(body);
  } catch (e) {
    body.innerHTML = `<p class="empty-state">Couldn't load this — ${esc(e.message || "error")}.</p>`;
  }
}

function otherProfile() {
  // Who a ping/message goes to: the other individual, not joint.
  const me = window.OPSDECK.activeProfile === "partner" ? "partner" : "primary";
  return me === "primary" ? "partner" : "primary";
}
function meProfile() {
  return window.OPSDECK.activeProfile === "partner" ? "partner" : "primary";
}

const STAGE_ART = ["🌱", "🌿", "🪴", "🌳", "🌸", "🌺", "🌟"];

async function jointHome(body) {
  const [home, xp, mile, fb] = await Promise.all([
    API.get("/joint/home"),
    API.get("/joint/relationship-xp"),
    API.get("/joint/milestones/upcoming"),
    API.get("/joint/flashback"),
  ]);

  const pct = xp.level_span ? Math.round((xp.into_level / xp.level_span) * 100) : 0;
  const stage = home.companion.growth_stage || 0;
  const nextMile = mile[0];

  const pings = [
    ["thinking_of_you", "💭 Thinking of you"],
    ["miss_you", "🫂 Miss you"],
    ["proud_of_you", "✨ Proud of you"],
    ["you_got_this", "💪 You got this"],
  ];

  body.innerHTML = `
    <div class="joint-grid">
      <div class="joint-card xp-card">
        <div class="xp-level">Lv ${xp.level}</div>
        <div class="xp-sub">Relationship · ${Math.round(xp.xp)} XP</div>
        <div class="xp-bar"><div class="xp-fill" style="width:${pct}%"></div></div>
        <div class="xp-next">${nextMile ? `Next: ${esc(nextMile.label)} (${Math.round(nextMile.remaining)} to go)` : "All milestones hit 🎉"}</div>
      </div>

      <div class="joint-card companion-card" id="companion-card">
        <div class="companion-art">${STAGE_ART[Math.min(stage, STAGE_ART.length - 1)]}</div>
        <div class="companion-name">${esc(home.companion.species_or_skin)} · stage ${stage}</div>
        <div class="companion-mood">${esc(home.companion.mood)}</div>
        <button class="btn small" id="companion-pet">Pet / water</button>
      </div>

      <div class="joint-card countdown-card">
        ${home.next_countdown
          ? `<div class="cd-days">${home.next_countdown.days_until}</div>
             <div class="cd-label">days until ${esc(home.next_countdown.label)}</div>`
          : `<div class="cd-empty">No countdowns yet</div>`}
      </div>
    </div>

    <div class="joint-card ping-card">
      <div class="block-title">Send a ping</div>
      <div class="ping-row">
        ${pings.map(([k, l]) => `<button class="btn ping-btn" data-ping="${k}">${l}</button>`).join("")}
      </div>
    </div>

    ${fb.has_any ? `
      <div class="joint-card flashback-card">
        <div class="block-title">On this day</div>
        ${fb.wall_posts.map((p) => `<div class="flash-item">📸 ${esc(p.caption || p.content)}</div>`).join("")}
        ${fb.messages.map((m) => `<div class="flash-item">💌 ${esc(m.body)}</div>`).join("")}
        ${fb.date_ideas.map((d) => `<div class="flash-item">💞 ${esc(d.title)}</div>`).join("")}
      </div>` : ""}`;

  el("companion-pet").addEventListener("click", async () => {
    try { await API.post("/joint/companion/interact", {}); toast("💚 boosted"); renderJoint(); }
    catch (e) { /* toasted (cooldown 429) */ }
  });
  body.querySelectorAll("[data-ping]").forEach((b) =>
    b.addEventListener("click", async () => {
      await API.post("/joint/ping", { from_profile_id: meProfile(), to_profile_id: otherProfile(), kind: b.dataset.ping });
      toast("Ping sent 💛");
    }));
}

async function jointWall(body) {
  const posts = await API.get("/joint/wall");
  body.innerHTML = `
    <div class="wall-compose">
      <textarea id="wall-input" rows="2" placeholder="Post a meme, a thought, a link…"></textarea>
      <button class="btn primary" id="wall-post">Post</button>
    </div>
    <div class="wall-feed">
      ${posts.length ? posts.map(wallPost).join("") : `<p class="empty-state">Nothing on the wall yet.</p>`}
    </div>`;

  el("wall-post").addEventListener("click", async () => {
    const content = el("wall-input").value.trim();
    if (!content) return;
    const type = /^https?:\/\//.test(content) ? "link" : "text";
    await API.post("/joint/wall", { profile_id: meProfile(), type, content });
    renderJoint();
  });

  body.querySelectorAll("[data-react]").forEach((b) =>
    b.addEventListener("click", async () => {
      await API.post(`/joint/wall/${b.dataset.react}/react`, { profile_id: meProfile(), emoji: b.dataset.emoji });
      renderJoint();
    }));
}

function wallPost(p) {
  const author = window.OPSDECK.profiles.find((x) => x.id === p.profile_id);
  const body = p.type === "link"
    ? `<a href="${escAttr(p.content)}" target="_blank" rel="noopener">${esc(p.content)}</a>`
    : esc(p.content);
  const reactions = (p.reactions || []).map((r) =>
    `<span class="wall-reaction">${esc(r.emoji)} ${r.n}</span>`).join("");
  return `
    <div class="wall-item">
      <div class="wall-meta"><span class="wall-author">${esc(author ? author.display_name : "?")}</span>
        <span class="card-meta">${fmtDate((p.created_at || "").slice(0, 10))}</span></div>
      <div class="wall-body">${body}</div>
      ${p.caption ? `<div class="wall-caption">${esc(p.caption)}</div>` : ""}
      <div class="wall-reactions">
        ${reactions}
        ${["❤️", "😂", "🔥", "🥹"].map((e) =>
          `<button class="react-add" data-react="${p.id}" data-emoji="${e}">${e}</button>`).join("")}
      </div>
    </div>`;
}

async function jointMailbox(body) {
  const msgs = await API.get("/joint/mailbox");
  body.innerHTML = `
    <div class="mailbox-compose joint-card">
      <div class="block-title">Schedule a note</div>
      <textarea id="mb-body" rows="2" placeholder="Write something for later…"></textarea>
      <div class="field-row-inline">
        <input type="datetime-local" id="mb-when">
        <button class="btn primary" id="mb-send">Schedule</button>
      </div>
      <p class="settings-hint">Leave the time blank to deliver now.</p>
    </div>
    <div class="mailbox-list">
      ${msgs.length ? msgs.map((m) => {
        const from = window.OPSDECK.profiles.find((p) => p.id === m.from_profile_id);
        return `<div class="mail-item ${m.delivered ? "delivered" : "pending"}">
          <div class="mail-head"><span>${esc(from ? from.display_name : "?")}</span>
            <span class="chip static ${m.delivered ? "c-teal" : "c-amber"}">${m.delivered ? "delivered" : "scheduled"}</span></div>
          <div class="mail-body">${esc(m.body)}</div>
          <div class="card-meta">${esc((m.deliver_at || "").slice(0, 16).replace("T", " "))}</div>
        </div>`;
      }).join("") : `<p class="empty-state">No messages yet.</p>`}
    </div>`;

  el("mb-send").addEventListener("click", async () => {
    const b = el("mb-body").value.trim();
    if (!b) return;
    const when = el("mb-when").value;
    await API.post("/joint/mailbox", {
      from_profile_id: meProfile(), to_profile_id: otherProfile(),
      body: b, deliver_at: when ? when.replace("T", " ") + ":00" : null,
    });
    toast("Scheduled 💌");
    renderJoint();
  });
}

async function jointPlans(body) {
  const [ideas, bucket] = await Promise.all([
    API.get("/joint/date-ideas"),
    API.get("/joint/bucket-list"),
  ]);
  body.innerHTML = `
    <div class="plans-cols">
      <div class="joint-card">
        <div class="block-title-row">
          <div class="block-title">Date jar</div>
          <button class="btn small" id="draw-idea">🎲 Draw</button>
        </div>
        <div class="field-row-inline">
          <input type="text" id="idea-title" placeholder="A date idea…">
          <button class="btn" id="add-idea">Add</button>
        </div>
        <div id="idea-drawn"></div>
        <div class="idea-list">
          ${ideas.map((i) => `
            <div class="idea-row">
              <span class="idea-name ${i.status === "done" ? "done" : ""}">${esc(i.title)}</span>
              <span class="idea-actions">
                <span class="chip static c-gray">${esc(i.status)}</span>
                ${i.status !== "done" ? `<button class="btn tiny" data-plan-done="${i.id}">Did it</button>` : ""}
              </span>
            </div>`).join("") || `<p class="empty-state">No ideas yet.</p>`}
        </div>
      </div>

      <div class="joint-card">
        <div class="block-title">Bucket list</div>
        <div class="field-row-inline">
          <input type="text" id="bucket-title" placeholder="Someday we should…">
          <button class="btn" id="add-bucket">Add</button>
        </div>
        <div class="bucket-list">
          ${bucket.map((b) => `
            <div class="idea-row">
              <span class="idea-name ${b.status === "done" ? "done" : ""}">${esc(b.title)}</span>
              <span class="idea-actions">
                <span class="chip static ${b.status === "done" ? "c-teal" : "c-gray"}">${esc(b.status)}</span>
                ${b.status !== "done" ? `<button class="btn tiny" data-bucket-done="${b.id}">✓</button>` : ""}
              </span>
            </div>`).join("") || `<p class="empty-state">Nothing here yet.</p>`}
        </div>
      </div>
    </div>`;

  el("add-idea").addEventListener("click", async () => {
    const t = el("idea-title").value.trim(); if (!t) return;
    await API.post("/joint/date-ideas", { created_by: meProfile(), title: t });
    renderJoint();
  });
  el("draw-idea").addEventListener("click", async () => {
    try {
      const r = await API.post("/joint/date-ideas/random", {});
      el("idea-drawn").innerHTML = `<div class="drawn">🎁 ${esc(r.title)}</div>`;
    } catch (e) { el("idea-drawn").innerHTML = `<div class="drawn">No unplanned ideas — add some!</div>`; }
  });
  el("add-bucket").addEventListener("click", async () => {
    const t = el("bucket-title").value.trim(); if (!t) return;
    await API.post("/joint/bucket-list", { title: t });
    renderJoint();
  });
  body.querySelectorAll("[data-plan-done]").forEach((b) =>
    b.addEventListener("click", async () => {
      await API.patch(`/joint/date-ideas/${b.dataset.planDone}`, { status: "done" });
      renderJoint();
    }));
  body.querySelectorAll("[data-bucket-done]").forEach((b) =>
    b.addEventListener("click", async () => {
      await API.patch(`/joint/bucket-list/${b.dataset.bucketDone}`, { status: "done" });
      toast("🎉 crossed off");
      renderJoint();
    }));
}

async function jointQA(body) {
  const [prompt, songs] = await Promise.all([
    API.get("/joint/daily-prompt/today"),
    API.get("/joint/song-of-day"),
  ]);

  const answerBlock = prompt.both_answered
    ? `<div class="qa-answers">
         ${prompt.answers.map((a) => {
           const who = window.OPSDECK.profiles.find((p) => p.id === a.profile_id);
           return `<div class="qa-answer"><span class="qa-who">${esc(who ? who.display_name : "?")}</span>
             <span>${esc(a.answer)}</span></div>`;
         }).join("")}
       </div>`
    : prompt.you_answered
      ? `<p class="settings-hint">You answered. Hidden until ${esc(window.OPSDECK.profiles.find((p) => p.id === otherProfile())?.display_name || "they")} answers too.</p>`
      : `<div class="field-row-inline">
           <input type="text" id="qa-input" placeholder="Your answer…">
           <button class="btn primary" id="qa-submit">Answer</button>
         </div>`;

  body.innerHTML = `
    <div class="joint-card">
      <div class="block-title">Today's question</div>
      <div class="qa-prompt">${esc(prompt.prompt_text)}</div>
      ${answerBlock}
    </div>

    <div class="joint-card">
      <div class="block-title">Song of the day</div>
      <div class="field-row-inline">
        <input type="text" id="song-title" placeholder="Track title">
        <input type="text" id="song-url" placeholder="link (optional)">
        <button class="btn" id="song-add">Add</button>
      </div>
      <div class="song-log">
        ${songs.map((s) => {
          const who = window.OPSDECK.profiles.find((p) => p.id === s.profile_id);
          return `<div class="song-row">
            <span class="song-date card-meta">${fmtDate(s.local_date)}</span>
            ${s.track_url ? `<a href="${escAttr(s.track_url)}" target="_blank" rel="noopener">${esc(s.track_title)}</a>` : esc(s.track_title)}
            <span class="card-meta">${esc(who ? who.display_name : "")}</span>
          </div>`;
        }).join("") || `<p class="empty-state">No songs logged yet.</p>`}
      </div>
    </div>`;

  if (el("qa-submit")) {
    el("qa-submit").addEventListener("click", async () => {
      const a = el("qa-input").value.trim(); if (!a) return;
      await API.post(`/joint/daily-prompt/${prompt.id}/answer`, { profile_id: meProfile(), answer: a });
      renderJoint();
    });
  }
  el("song-add").addEventListener("click", async () => {
    const t = el("song-title").value.trim(); if (!t) return;
    await API.post("/joint/song-of-day", {
      profile_id: meProfile(), track_title: t, track_url: el("song-url").value.trim() || null,
    });
    renderJoint();
  });
}
