// The mentor flow: level-up verification and the proposal inbox.
//
// A level-up is never a button that just increments a number. The order is
// deliberate and strict: notes first (no writeup, no attempt), then the bar
// is shown up front, then the mentor asks questions grounded in the notes,
// and only answers that hold up get the level. "Not yet" is a real verdict.

async function startLevelUp(nodeId, opts = {}) {
  closeModal();
  openModal(`<div class="mentor-wait">
    <div class="mentor-badge">MENTOR</div>
    <p>Checking the bar…</p>
  </div>`);

  let preview, docs;
  try {
    [preview, docs] = await Promise.all([
      API.get(`/tree/nodes/${nodeId}/levelup/preview`),
      API.get("/docs"),
    ]);
  } catch (e) {
    closeModal();
    return;
  }

  const diff = preview.difficulty;
  const fails = preview.rejected_attempts;

  openModal(`
    <div class="modal-head">
      <div>
        <div class="mentor-badge">MENTOR</div>
        <h2 class="modal-title">Level ${preview.target_level} — the bar</h2>
      </div>
      <button class="icon-btn" onclick="closeModal()">×</button>
    </div>

    <div class="difficulty-row">
      <span class="card-meta">Difficulty</span>
      ${Array.from({ length: 5 }, (_, i) =>
        `<span class="pip ${i < diff ? "on" : ""}"></span>`).join("")}
      <span class="card-meta">${diff}/5</span>
      ${fails ? `<span class="chip static c-red">${fails} failed attempt${fails > 1 ? "s" : ""}</span>` : ""}
    </div>

    <div class="expectation-box">
      <div class="expectation-title">Expect to prove</div>
      <p>${esc(preview.expectations)}</p>
    </div>

    <label class="field-label">Your notes (required)</label>
    <select id="evidence-doc">
      <option value="">— pick a writeup —</option>
      ${docs.map((d) => `<option value="${d.id}">${esc(d.title)}</option>`).join("")}
    </select>
    <p class="notes-gate-hint">
      Notes come first. A real writeup — what you did, the commands or
      techniques, and <em>why</em> they worked — at least
      ${preview.notes_min_chars} characters. Thin notes are rejected before
      the mentor even asks a question.
    </p>

    <div class="modal-actions">
      <button class="btn" id="write-notes">Write notes first</button>
      <button class="btn primary" id="begin-attempt">Begin verification</button>
    </div>
  `, () => {
    el("write-notes").addEventListener("click", () => {
      closeModal();
      go("docs");
      toast("Write the room/skill up, then come back to the node", "info", 5000);
    });

    el("begin-attempt").addEventListener("click", async () => {
      const evidence = el("evidence-doc").value;
      if (!evidence) {
        toast("Pick your notes doc — no writeup, no attempt", "error");
        return;
      }
      openModal(`<div class="mentor-wait">
        <div class="mentor-badge">MENTOR</div>
        <p>Reading your notes, preparing questions…</p>
      </div>`);

      let attempt;
      try {
        attempt = await API.post(`/tree/nodes/${nodeId}/levelup`, {
          evidence_doc: Number(evidence),
          room_code: opts.roomCode || null,
        });
      } catch (e) {
        // The API client already toasted the notes-gate message.
        startLevelUp(nodeId, opts);
        return;
      }

      const questions = JSON.parse(attempt.questions || "[]");
      if (!questions.length) {
        showQueued(attempt);
        return;
      }
      showQuestions(attempt.id, attempt, questions);
    });
  });
}

function showQueued(attempt) {
  openModal(`
    <div class="modal-head">
      <h2 class="modal-title">Verification queued</h2>
      <button class="icon-btn" onclick="closeModal()">×</button>
    </div>
    <p class="node-desc">
      Attempt <strong>#${attempt.id}</strong> is open at difficulty
      ${attempt.difficulty}/5, notes attached, waiting on questions.
    </p>
    <p class="node-desc">
      Set <code>ANTHROPIC_API_KEY</code> to have the mentor respond in-app,
      or have Claude Code pick it up:
    </p>
    <pre class="code-hint">GET  /api/attempts?status=awaiting_questions
POST /api/attempts/${attempt.id}/questions</pre>
    <div class="modal-actions">
      <button class="btn danger" id="cancel-attempt">Cancel attempt</button>
      <button class="btn" onclick="closeModal()">Close</button>
    </div>
  `, () => {
    el("cancel-attempt").addEventListener("click", async () => {
      await API.del(`/attempts/${attempt.id}`);
      closeModal();
      renderTree();
    });
  });
}

async function openAttempt(attemptId) {
  closeModal();
  const attempt = await API.get(`/attempts/${attemptId}`);
  const questions = attempt.questions || [];
  if (!questions.length) {
    toast("Still waiting on the mentor's questions", "info");
    return;
  }
  showQuestions(attemptId, attempt, questions, attempt.answers || []);
}

async function showQuestions(attemptId, attempt, questions, existing = []) {
  const title = attempt.node_title || attempt.context?.node?.title || "this skill";
  const diff = attempt.difficulty || attempt.context?.difficulty || 1;

  let notesTitle = "";
  if (attempt.evidence_doc) {
    try { notesTitle = (await API.get(`/docs/${attempt.evidence_doc}`)).title; }
    catch (e) { /* doc deleted mid-attempt; the server still has nothing to grade against */ }
  }

  openModal(`
    <div class="modal-head">
      <div>
        <div class="mentor-badge">MENTOR</div>
        <h2 class="modal-title">${esc(title)}</h2>
      </div>
      <button class="icon-btn" onclick="closeModal()">×</button>
    </div>

    <div class="difficulty-row">
      <span class="card-meta">Difficulty</span>
      ${Array.from({ length: 5 }, (_, i) =>
        `<span class="pip ${i < diff ? "on" : ""}"></span>`).join("")}
      <span class="card-meta">${diff}/5</span>
      ${notesTitle ? `<span class="chip static c-teal">notes: ${esc(truncate(notesTitle, 24))}</span>` : ""}
    </div>

    <p class="mentor-intro">
      Answer in your own words. The mentor has read your notes and will
      grade both together — specifics beat definitions, and vague answers
      are rejected, not nudged.
    </p>

    ${questions.map((q, i) => `
      <div class="question-block">
        <div class="question-text"><span class="qnum">${i + 1}</span>${esc(q)}</div>
        <textarea class="modal-textarea" data-answer="${i}" rows="3"
                  placeholder="Your answer…">${esc(existing[i] || "")}</textarea>
      </div>`).join("")}

    <div class="modal-actions">
      <button class="btn" id="not-ready" title="Closes the attempt without a verdict — it doesn't count as a failure">
        Not ready yet
      </button>
      <button class="btn primary" id="submit-answers">Submit for review</button>
    </div>
  `, (modal) => {
    el("not-ready").addEventListener("click", async () => {
      await API.del(`/attempts/${attemptId}`);
      closeModal();
      toast("Attempt closed — no verdict, no failure. Come back when you're ready.", "info", 4500);
      renderTree();
    });

    el("submit-answers").addEventListener("click", async () => {
      const answers = [...modal.querySelectorAll("[data-answer]")].map((t) => t.value.trim());
      if (answers.every((a) => !a)) {
        toast("Answer at least one question", "error");
        return;
      }

      openModal(`<div class="mentor-wait">
        <div class="mentor-badge">MENTOR</div>
        <p>Reviewing your answers against your notes…</p>
      </div>`);

      let result;
      try {
        result = await API.post(`/attempts/${attemptId}/answer`, { answers });
      } catch (e) {
        closeModal();
        return;
      }

      if (result.status === "grading") {
        openModal(`
          <div class="modal-head">
            <h2 class="modal-title">Submitted</h2>
            <button class="icon-btn" onclick="closeModal()">×</button>
          </div>
          <p class="node-desc">Your answers are queued for review.</p>
          <pre class="code-hint">GET  /api/attempts?status=grading
POST /api/attempts/${attemptId}/verdict</pre>
          <div class="modal-actions">
            <button class="btn" onclick="closeModal()">Close</button>
          </div>`);
        return;
      }

      showVerdict(result);
    });
  });
}

function showVerdict(result) {
  const granted = result.granted;

  const unlocks = (result.unlocked_nodes || []).length
    ? `<div class="unlock-note">
         Unlocked: ${result.unlocked_nodes.map((n) => esc(n.title)).join(", ")}
       </div>`
    : "";

  const proposal = result.proposal_id
    ? `<div class="unlock-note subtle">
         The mentor suggested new nodes — waiting in Growth › proposals.
       </div>`
    : "";

  openModal(`
    <div class="modal-head">
      <div>
        <div class="mentor-badge ${granted ? "granted" : "rejected"}">
          ${granted ? "LEVEL GRANTED" : "NOT YET"}
        </div>
        ${granted ? `<h2 class="modal-title">Level ${result.new_level}</h2>` : ""}
      </div>
      <button class="icon-btn" onclick="closeModal()">×</button>
    </div>

    <p class="verdict-feedback">${esc(result.feedback)}</p>
    ${granted ? "" : `<p class="mentor-intro">
      A failed attempt stays on the record. Close the gap it named — in your
      notes and in your head — and try again.
    </p>`}
    ${unlocks}
    ${proposal}

    <div class="modal-actions">
      ${granted
        ? `<button class="btn primary" id="verdict-close">Continue</button>`
        : `<button class="btn" id="verdict-close">Close</button>
           <button class="btn primary" id="retry">Try again</button>`}
    </div>
  `, () => {
    el("verdict-close").addEventListener("click", async () => {
      closeModal();
      await renderTree();
      if (granted) celebrateLevelUp(result.node_id);
    });
    el("retry")?.addEventListener("click", async () => {
      closeModal();
      await renderTree();
      startLevelUp(result.node_id);
    });
  });
}

// ---------- Proposals ----------
async function renderProposalsInto(container) {
  const proposals = await API.get("/proposals?status=pending");

  if (!proposals.length) {
    container.innerHTML = `<p class="empty-state">No pending proposals.</p>`;
    return;
  }

  container.innerHTML = proposals.map((p) => `
    <div class="proposal" data-proposal="${p.id}">
      <div class="proposal-head">
        <span class="chip static c-purple">${esc(p.kind.replace("_", " "))}</span>
        <span class="proposal-title">${esc(p.title)}</span>
      </div>
      ${p.rationale ? `<p class="proposal-rationale">${esc(p.rationale)}</p>` : ""}
      <div class="proposal-actions-list">
        ${p.actions.map((a) => `<div class="action-line">${esc(describeAction(a))}</div>`).join("")}
      </div>
      <div class="proposal-buttons">
        <button class="btn" data-reject="${p.id}">Reject</button>
        <button class="btn primary" data-approve="${p.id}">Approve</button>
      </div>
    </div>`).join("");

  container.querySelectorAll("[data-approve]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const res = await API.post(`/proposals/${btn.dataset.approve}/approve`, {});
      toast(res.errors?.length
        ? `Applied with ${res.errors.length} error(s)`
        : `Applied ${res.applied.length} change(s)`,
        res.errors?.length ? "error" : "info");
      renderGrowth();
    });
  });

  container.querySelectorAll("[data-reject]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await API.post(`/proposals/${btn.dataset.reject}/reject`, {});
      renderGrowth();
    });
  });
}

// Turn an action object into a line a human can actually check before approving.
function describeAction(a) {
  switch (a.op) {
    case "create_node":  return `Add node "${a.title}" (${a.domain}, tier ${a.tier})`;
    case "update_node":  return `Edit node #${a.node_id}`;
    case "create_edge":  return `Connect node #${a.from_id} → #${a.to_id}`;
    case "delete_edge":  return `Disconnect #${a.from_id} → #${a.to_id}`;
    case "move_card":    return `Move card #${a.card_id} to list #${a.list_id}`;
    case "set_due":      return `Set card #${a.card_id} due ${a.due_at || "(cleared)"}`;
    case "update_card":  return `Update card #${a.card_id}`;
    case "create_card":  return `Create card "${a.title}"`;
    case "create_routine": return `Add routine "${a.name}"`;
    default:             return a.op || "unknown action";
  }
}
