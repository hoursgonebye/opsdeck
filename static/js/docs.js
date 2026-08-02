// Docs: upload or author .md/.html, organise by folder and tags, render
// inline. HTML docs render in a sandboxed iframe rather than being injected
// into the page - an uploaded file is untrusted input, and srcdoc without
// allow-scripts means a stray <script> can't touch the app or its token.

let docsCache = [];
let activeFolder = null;

async function renderDocs() {
  const panel = el("panel");
  panel.innerHTML = `<div class="loading">Loading…</div>`;

  docsCache = await API.get("/docs");

  const folders = [...new Set(docsCache.map((d) => d.folder).filter(Boolean))].sort();
  const shown = activeFolder
    ? docsCache.filter((d) => d.folder === activeFolder)
    : docsCache;

  const folderTabs = `
    <button class="board-tab ${!activeFolder ? "active" : ""}" data-folder="">All</button>
    ${folders.map((f) => `
      <button class="board-tab ${activeFolder === f ? "active" : ""}" data-folder="${escAttr(f)}">
        ${esc(f)}
      </button>`).join("")}`;

  const rows = shown.length ? shown.map((d) => `
    <div class="doc-row" data-doc="${d.id}">
      <span class="doc-kind ${esc(d.kind)}">${esc(d.kind)}</span>
      <span class="doc-title">${esc(d.title)}</span>
      ${d.folder ? `<span class="today-meta">${esc(d.folder)}</span>` : ""}
      <span class="tag-row">${d.tags.map((t) => `<span class="tag">${esc(t)}</span>`).join("")}</span>
      <span class="card-meta">${fmtDate(d.updated_at.slice(0, 10))}</span>
    </div>`).join("")
    : `<p class="empty-state">No documents yet.</p>`;

  panel.innerHTML = `
    <div class="section-head-row">
      <h1 class="section-title">Docs</h1>
      <div class="head-actions">
        <label class="btn" for="doc-upload">Upload</label>
        <input type="file" id="doc-upload" accept=".md,.markdown,.html,.htm,.txt" hidden>
        <button class="btn primary" id="new-doc">+ New doc</button>
      </div>
    </div>
    <div class="board-tabs">${folderTabs}</div>
    <div class="doc-list">${rows}</div>`;

  panel.querySelectorAll("[data-folder]").forEach((tab) => {
    tab.addEventListener("click", () => {
      activeFolder = tab.dataset.folder || null;
      renderDocs();
    });
  });

  panel.querySelectorAll(".doc-row").forEach((row) => {
    row.addEventListener("click", () => openDocViewer(Number(row.dataset.doc)));
  });

  el("new-doc").addEventListener("click", () => openDocEditor(null));

  el("doc-upload").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    if (activeFolder) fd.append("folder", activeFolder);
    await API.upload("/docs/upload", fd);
    toast("Uploaded " + file.name);
    renderDocs();
  });
}

async function openDocViewer(docId) {
  const doc = await API.get(`/docs/${docId}`);
  const panel = el("panel");

  const rendered = doc.kind === "html"
    // allow-scripts WITHOUT allow-same-origin: the frame gets a unique opaque
    // origin, so interactive docs (calculators, mockups) actually work while
    // still being unable to read the parent DOM, cookies, or the API token.
    // Never add allow-same-origin here - the pair together defeats the sandbox.
    ? `<iframe class="doc-frame" sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"
               referrerpolicy="no-referrer" srcdoc="${escAttr(doc.body)}"></iframe>`
    : `<div class="doc-rendered">${renderMarkdown(doc.body)}</div>`;

  panel.innerHTML = `
    <div class="section-head-row">
      <h1 class="section-title">${esc(doc.title)}</h1>
      <div class="head-actions">
        <button class="btn" id="back-docs">← Docs</button>
        <button class="btn" id="edit-doc">Edit</button>
        <button class="btn danger" id="del-doc">Delete</button>
      </div>
    </div>
    <p class="section-sub">
      ${doc.folder ? esc(doc.folder) + " · " : ""}${esc(doc.kind)} · updated ${fmtDate(doc.updated_at.slice(0, 10))}
      ${doc.tags.length ? " · " + doc.tags.map((t) => `<span class="tag">${esc(t)}</span>`).join("") : ""}
    </p>
    ${rendered}`;

  el("back-docs").addEventListener("click", renderDocs);
  el("edit-doc").addEventListener("click", () => openDocEditor(doc));
  el("del-doc").addEventListener("click", async () => {
    if (!confirm("Delete this document?")) return;
    await API.del(`/docs/${doc.id}`);
    renderDocs();
  });
}

function openDocEditor(doc) {
  const d = doc || { title: "", kind: "md", body: "", folder: "", tags: [] };

  openModal(`
    <div class="modal-head">
      <h2 class="modal-title">${doc ? "Edit doc" : "New doc"}</h2>
      <button class="icon-btn" onclick="closeModal()">×</button>
    </div>

    <label class="field-label">Title</label>
    <input type="text" id="doc-title" value="${escAttr(d.title)}">

    <div class="field-row">
      <div>
        <label class="field-label">Format</label>
        <select id="doc-kind">
          <option value="md" ${d.kind === "md" ? "selected" : ""}>Markdown</option>
          <option value="html" ${d.kind === "html" ? "selected" : ""}>HTML</option>
        </select>
      </div>
      <div>
        <label class="field-label">Folder</label>
        <input type="text" id="doc-folder" value="${escAttr(d.folder)}" placeholder="optional">
      </div>
    </div>

    <label class="field-label">Tags (comma separated)</label>
    <input type="text" id="doc-tags" value="${escAttr((d.tags || []).join(", "))}">

    <label class="field-label">Content</label>
    <textarea id="doc-body" class="modal-textarea mono" rows="14">${esc(d.body)}</textarea>

    <div class="modal-actions">
      <button class="btn primary" id="save-doc">Save</button>
    </div>
  `, () => {
    el("save-doc").addEventListener("click", async () => {
      const payload = {
        title: el("doc-title").value || "Untitled",
        kind: el("doc-kind").value,
        folder: el("doc-folder").value,
        body: el("doc-body").value,
        tags: el("doc-tags").value.split(",").map((t) => t.trim()).filter(Boolean),
      };
      const saved = doc
        ? await API.patch(`/docs/${doc.id}`, payload)
        : await API.post("/docs", payload);
      closeModal();
      toast("Saved");
      openDocViewer(saved.id);
    });
  });
}

// A deliberately small Markdown subset - headings, emphasis, code, lists,
// links, quotes, rules. Everything is escaped first, so no raw HTML passes
// through from a doc into the page.
function renderMarkdown(src) {
  let html = esc(src);

  const blocks = [];
  html = html.replace(/```([\s\S]*?)```/g, (m, code) => {
    blocks.push(`<pre><code>${code.trim()}</code></pre>`);
    return `\u0000BLOCK${blocks.length - 1}\u0000`;
  });

  html = html
    .replace(/^###### (.*)$/gm, "<h6>$1</h6>")
    .replace(/^##### (.*)$/gm, "<h5>$1</h5>")
    .replace(/^#### (.*)$/gm, "<h4>$1</h4>")
    .replace(/^### (.*)$/gm, "<h3>$1</h3>")
    .replace(/^## (.*)$/gm, "<h2>$1</h2>")
    .replace(/^# (.*)$/gm, "<h1>$1</h1>")
    .replace(/^&gt; (.*)$/gm, "<blockquote>$1</blockquote>")
    .replace(/^(---|\*\*\*)$/gm, "<hr>")
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
             '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');

  // Group consecutive list items into a single <ul>/<ol>.
  html = html.replace(/(?:^[-*+] .*$\n?)+/gm, (m) =>
    "<ul>" + m.trim().split("\n").map((l) => `<li>${l.replace(/^[-*+] /, "")}</li>`).join("") + "</ul>");
  html = html.replace(/(?:^\d+\. .*$\n?)+/gm, (m) =>
    "<ol>" + m.trim().split("\n").map((l) => `<li>${l.replace(/^\d+\. /, "")}</li>`).join("") + "</ol>");

  html = html
    .split(/\n{2,}/)
    .map((chunk) => {
      const t = chunk.trim();
      if (!t) return "";
      if (/^<(h\d|ul|ol|pre|blockquote|hr)/.test(t)) return t;
      if (t.startsWith("\u0000BLOCK")) return t;
      return `<p>${t.replace(/\n/g, "<br>")}</p>`;
    })
    .join("\n");

  return html.replace(/\u0000BLOCK(\d+)\u0000/g, (m, i) => blocks[Number(i)]);
}
