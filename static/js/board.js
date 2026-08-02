// Boards: multiple boards with a switcher, drag-and-drop cards, and a card
// detail modal covering description, due date, labels, checklist, and
// attachments. All mutations go through the same REST API scripts use.

let boards = [];
let activeBoardId = null;
let dragCardId = null;

async function renderBoard() {
  const panel = el("panel");
  panel.innerHTML = `<div class="loading">Loading…</div>`;

  boards = await API.get("/boards");
  if (!boards.length) {
    panel.innerHTML = `
      <h1 class="section-title">Boards</h1>
      <p class="empty-state">No boards yet.</p>
      <button class="btn primary" onclick="promptNewBoard()">Create a board</button>`;
    return;
  }

  if (!boards.find((b) => b.id === activeBoardId)) activeBoardId = boards[0].id;
  const board = boards.find((b) => b.id === activeBoardId);

  const tabs = boards.map((b) => `
    <button class="board-tab ${b.id === activeBoardId ? "active" : ""}" data-board="${b.id}">
      ${esc(b.title)}
    </button>`).join("");

  const listsHtml = board.lists.map((list) => `
    <div class="board-list" data-list="${list.id}">
      <div class="board-list-header">
        <span class="list-title" data-list="${list.id}">${esc(list.title)}</span>
        <span class="board-list-count">${list.cards.length}</span>
        <button class="icon-btn" data-action="del-list" data-list="${list.id}" title="Delete list">×</button>
      </div>
      <div class="board-cards" data-droplist="${list.id}">
        ${list.cards.map(cardHtml).join("")}
      </div>
      <div class="board-add-card">
        <input type="text" placeholder="+ Add a card" data-list="${list.id}">
      </div>
    </div>`).join("");

  panel.innerHTML = `
    <div class="section-head-row">
      <h1 class="section-title">Boards</h1>
      <div class="head-actions">
        <button class="btn" id="manage-labels">Labels</button>
        <button class="btn" id="archive-board">Archive board</button>
        <button class="btn primary" id="new-board">+ Board</button>
      </div>
    </div>
    <div class="board-tabs">${tabs}</div>
    <div class="board">
      ${listsHtml}
      <div class="board-add-list" id="add-list-btn">+ Add a list</div>
    </div>`;

  attachBoardHandlers(board);
}

function cardHtml(c) {
  const labels = c.labels.map((l) => `<span class="label-chip ${esc(l.color)}" title="${escAttr(l.name)}"></span>`).join("");
  const checkDone = c.checklist.filter((i) => i.done).length;
  const meta = [];
  if (c.due_at) meta.push(`<span class="due-pill ${dueClass(c.due_at)}">${fmtDate(c.due_at)}</span>`);
  if (c.checklist.length) meta.push(`<span class="card-meta">☑ ${checkDone}/${c.checklist.length}</span>`);
  if (c.attachments.length) meta.push(`<span class="card-meta">📎 ${c.attachments.length}</span>`);
  if (c.description) meta.push(`<span class="card-meta">≡</span>`);

  return `
    <div class="board-card ${c.completed ? "completed" : ""}" draggable="true" data-card="${c.id}">
      ${labels ? `<div class="label-row">${labels}</div>` : ""}
      <span class="board-card-text">${esc(c.title)}</span>
      ${meta.length ? `<div class="card-meta-row">${meta.join("")}</div>` : ""}
    </div>`;
}

function attachBoardHandlers(board) {
  const panel = el("panel");

  panel.querySelectorAll(".board-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      activeBoardId = Number(tab.dataset.board);
      renderBoard();
    });
  });

  el("new-board").addEventListener("click", promptNewBoard);
  el("manage-labels").addEventListener("click", () => openLabelManager(board));

  el("archive-board").addEventListener("click", async () => {
    if (!confirm(`Archive "${board.title}"? You can still reach it via the API.`)) return;
    await API.patch(`/boards/${board.id}`, { archived: 1 });
    activeBoardId = null;
    renderBoard();
  });

  el("add-list-btn").addEventListener("click", async () => {
    const title = prompt("List name:");
    if (!title) return;
    await API.post("/lists", { board_id: board.id, title });
    renderBoard();
  });

  panel.querySelectorAll('[data-action="del-list"]').forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm("Delete this list and its cards?")) return;
      await API.del(`/lists/${btn.dataset.list}`);
      renderBoard();
    });
  });

  panel.querySelectorAll(".list-title").forEach((span) => {
    span.addEventListener("dblclick", async () => {
      const title = prompt("Rename list:", span.textContent.trim());
      if (!title) return;
      await API.patch(`/lists/${span.dataset.list}`, { title });
      renderBoard();
    });
  });

  panel.querySelectorAll(".board-add-card input").forEach((input) => {
    input.addEventListener("keydown", async (e) => {
      if (e.key !== "Enter" || !input.value.trim()) return;
      await API.post("/cards", { list_id: Number(input.dataset.list), title: input.value.trim() });
      input.value = "";
      renderBoard();
    });
  });

  panel.querySelectorAll(".board-card").forEach((card) => {
    card.addEventListener("click", () => openCardModal(Number(card.dataset.card), board));
    card.addEventListener("dragstart", () => {
      dragCardId = Number(card.dataset.card);
      card.classList.add("dragging");
    });
    card.addEventListener("dragend", () => card.classList.remove("dragging"));
  });

  panel.querySelectorAll(".board-cards").forEach((container) => {
    container.addEventListener("dragover", (e) => {
      e.preventDefault();
      container.classList.add("drag-over");
    });
    container.addEventListener("dragleave", () => container.classList.remove("drag-over"));
    container.addEventListener("drop", async (e) => {
      e.preventDefault();
      container.classList.remove("drag-over");
      if (dragCardId == null) return;

      const listId = Number(container.dataset.droplist);
      const others = [...container.querySelectorAll(".board-card:not(.dragging)")]
        .map((n) => Number(n.dataset.card));

      let insertAt = others.length;
      const nodes = [...container.querySelectorAll(".board-card:not(.dragging)")];
      for (let i = 0; i < nodes.length; i++) {
        const r = nodes[i].getBoundingClientRect();
        if (e.clientY < r.top + r.height / 2) { insertAt = i; break; }
      }
      others.splice(insertAt, 0, dragCardId);

      await API.post("/cards/reorder", { list_id: listId, card_ids: others });
      dragCardId = null;
      renderBoard();
    });
  });
}

async function promptNewBoard() {
  const title = prompt("Board name:");
  if (!title) return;
  const b = await API.post("/boards", { title });
  activeBoardId = b.id;
  renderBoard();
}

// ---------- Card detail modal ----------
async function openCardModal(cardId, board) {
  let card = null;
  for (const l of board.lists) {
    const found = l.cards.find((c) => c.id === cardId);
    if (found) { card = found; break; }
  }
  if (!card) return;

  const labelPicker = board.labels.map((l) => {
    const on = card.labels.some((cl) => cl.id === l.id);
    return `<button class="label-pick ${esc(l.color)} ${on ? "on" : ""}" data-label="${l.id}">
              ${esc(l.name)}
            </button>`;
  }).join("");

  const checklistHtml = card.checklist.map((i) => `
    <div class="check-item ${i.done ? "done" : ""}" data-item="${i.id}">
      <span class="check small"></span>
      <span class="check-text">${esc(i.text)}</span>
      <button class="icon-btn" data-action="del-item" data-item="${i.id}">×</button>
    </div>`).join("");

  const attachHtml = card.attachments.map((a) => `
    <div class="attach-row">
      <a href="/api/attachments/${a.id}?token=${encodeURIComponent(window.OPSDECK.token)}" target="_blank">
        ${esc(a.filename)}
      </a>
      <span class="card-meta">${(a.size / 1024).toFixed(0)} KB</span>
      <button class="icon-btn" data-action="del-attach" data-attach="${a.id}">×</button>
    </div>`).join("");

  openModal(`
    <div class="modal-head">
      <input class="modal-title-input" id="card-title" value="${escAttr(card.title)}">
      <button class="icon-btn" onclick="closeModal()">×</button>
    </div>

    <label class="field-label">Description</label>
    <textarea id="card-desc" class="modal-textarea" rows="4">${esc(card.description)}</textarea>

    <div class="field-row">
      <div>
        <label class="field-label">Due date</label>
        <input type="date" id="card-due" value="${card.due_at ? card.due_at.slice(0, 10) : ""}">
      </div>
      <div>
        <label class="field-label">Status</label>
        <select id="card-status">
          <option value="0" ${card.completed ? "" : "selected"}>Open</option>
          <option value="1" ${card.completed ? "selected" : ""}>Completed</option>
        </select>
      </div>
    </div>

    <label class="field-label">Labels</label>
    <div class="label-picker">${labelPicker || '<span class="empty-state">No labels on this board yet.</span>'}</div>

    <label class="field-label">Checklist</label>
    <div id="checklist">${checklistHtml}</div>
    <input type="text" id="new-check-item" class="inline-input" placeholder="+ Add checklist item">

    <label class="field-label">Attachments</label>
    <div id="attachments">${attachHtml}</div>
    <input type="file" id="attach-file" class="inline-input">

    <div class="modal-actions">
      <button class="btn danger" id="delete-card">Delete card</button>
      <button class="btn primary" id="save-card">Save</button>
    </div>
  `, (modal) => {
    const selected = new Set(card.labels.map((l) => l.id));

    modal.querySelectorAll(".label-pick").forEach((btn) => {
      btn.addEventListener("click", () => {
        const id = Number(btn.dataset.label);
        selected.has(id) ? selected.delete(id) : selected.add(id);
        btn.classList.toggle("on");
      });
    });

    modal.querySelectorAll(".check-item .check").forEach((chk) => {
      chk.addEventListener("click", async () => {
        const item = chk.closest(".check-item");
        const isDone = item.classList.contains("done");
        await API.patch(`/checklist/${item.dataset.item}`, { done: isDone ? 0 : 1 });
        item.classList.toggle("done");
      });
    });

    modal.querySelectorAll('[data-action="del-item"]').forEach((btn) => {
      btn.addEventListener("click", async () => {
        await API.del(`/checklist/${btn.dataset.item}`);
        btn.closest(".check-item").remove();
      });
    });

    el("new-check-item").addEventListener("keydown", async (e) => {
      if (e.key !== "Enter" || !e.target.value.trim()) return;
      await API.post(`/cards/${card.id}/checklist`, { text: e.target.value.trim() });
      e.target.value = "";
      closeModal();
      await renderBoard();
      openCardModal(card.id, boards.find((b) => b.id === activeBoardId));
    });

    modal.querySelectorAll('[data-action="del-attach"]').forEach((btn) => {
      btn.addEventListener("click", async () => {
        await API.del(`/attachments/${btn.dataset.attach}`);
        btn.closest(".attach-row").remove();
      });
    });

    el("attach-file").addEventListener("change", async (e) => {
      const file = e.target.files[0];
      if (!file) return;
      const fd = new FormData();
      fd.append("file", file);
      await API.upload(`/cards/${card.id}/attachments`, fd);
      toast("Attached " + file.name);
      closeModal();
      await renderBoard();
      openCardModal(card.id, boards.find((b) => b.id === activeBoardId));
    });

    el("delete-card").addEventListener("click", async () => {
      if (!confirm("Delete this card?")) return;
      await API.del(`/cards/${card.id}`);
      closeModal();
      renderBoard();
    });

    el("save-card").addEventListener("click", async () => {
      await API.patch(`/cards/${card.id}`, {
        title: el("card-title").value,
        description: el("card-desc").value,
        due_at: el("card-due").value || null,
        completed: Number(el("card-status").value),
        label_ids: [...selected],
      });
      closeModal();
      toast("Saved");
      renderBoard();
    });
  });
}

function openLabelManager(board) {
  const rows = board.labels.map((l) => `
    <div class="attach-row">
      <span class="label-chip ${esc(l.color)}"></span>
      <span style="flex:1">${esc(l.name)}</span>
      <button class="icon-btn" data-action="del-label" data-label="${l.id}">×</button>
    </div>`).join("");

  openModal(`
    <div class="modal-head">
      <h2 class="modal-title">Labels</h2>
      <button class="icon-btn" onclick="closeModal()">×</button>
    </div>
    <div id="label-list">${rows || '<p class="empty-state">No labels yet.</p>'}</div>
    <label class="field-label">New label</label>
    <div class="field-row">
      <input type="text" id="label-name" placeholder="Name">
      <select id="label-color">
        ${LABEL_COLORS.map((c) => `<option value="${c}">${c}</option>`).join("")}
      </select>
    </div>
    <div class="modal-actions">
      <button class="btn primary" id="add-label">Add label</button>
    </div>
  `, (modal) => {
    modal.querySelectorAll('[data-action="del-label"]').forEach((btn) => {
      btn.addEventListener("click", async () => {
        await API.del(`/labels/${btn.dataset.label}`);
        btn.closest(".attach-row").remove();
      });
    });
    el("add-label").addEventListener("click", async () => {
      const name = el("label-name").value.trim();
      if (!name) return;
      await API.post("/labels", { board_id: board.id, name, color: el("label-color").value });
      closeModal();
      renderBoard();
    });
  });
}
