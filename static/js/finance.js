// Finance: a personal ledger. Phase 1 - accounts, fast manual entry, the
// transaction list, and CSV import with preview.
//
// The quick-entry form is the whole point of this section: if logging a
// purchase takes more than a few seconds it stops happening, so the form
// stays mounted, resets and refocuses after every submit, and pre-fills
// category + account from the merchant's history. Everything else is
// secondary to that loop.
//
// Money crosses the wire as a decimal *string* (the server owns the
// cents conversion) or as integer cents coming back. Nothing here does
// arithmetic on amounts beyond formatting them.

let fFilters = { account_id: "", category_id: "", uncategorized: false, q: "" };
let fCache = { accounts: [], categories: [], merchants: [] };
let fNextBefore = null;

function fmtMoney(cents) {
  const d = Math.abs(cents) / 100;
  return "$" + d.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function finCatChip(t) {
  if (!t.category_id) {
    return `<span class="chip static c-amber">unfiled</span>`;
  }
  const color = t.category_color || "gray";
  return `<span class="chip static c-${esc(color)}">${esc(t.category_name)}</span>`;
}

function finAcctKey() { return `finAcct:${window.OPSDECK.activeProfile || "primary"}`; }

async function renderFinance() {
  const panel = el("panel");
  panel.innerHTML = `<div class="loading">Loading…</div>`;

  const [accounts, categories, merchants] = await Promise.all([
    API.get("/finance/accounts"),
    API.get("/finance/categories"),
    API.get("/finance/merchants"),
  ]);
  fCache = { accounts, categories, merchants };

  if (!accounts.length) {
    panel.innerHTML = `
      <h1 class="section-title">Finance</h1>
      <div class="joint-card">
        <div class="block-title">Add your first account</div>
        <p class="settings-hint">Transactions live on an account — checking, a credit
        card, or plain cash. Add one to start logging.</p>
        <div class="field-row-inline">
          <input type="text" id="fin-first-name" placeholder="Name (e.g. Checking)">
          <select id="fin-first-type">
            <option value="checking">checking</option><option value="credit">credit</option>
            <option value="cash">cash</option><option value="other">other</option>
          </select>
          <button class="btn primary" id="fin-first-add">Add account</button>
        </div>
      </div>`;
    el("fin-first-add").addEventListener("click", async () => {
      const name = el("fin-first-name").value.trim();
      if (!name) { toast("Name the account", "error"); return; }
      await API.post("/finance/accounts", { name, type: el("fin-first-type").value });
      toast("Account added");
      renderFinance();
    });
    return;
  }

  const lastAcct = localStorage.getItem(finAcctKey());
  const active = accounts.filter((a) => a.is_active);
  const spend = categories.filter((c) => !c.is_income && !c.is_transfer);
  const income = categories.filter((c) => c.is_income);
  const transfer = categories.filter((c) => c.is_transfer);

  panel.innerHTML = `
    <div class="section-head-row">
      <h1 class="section-title">Finance</h1>
      <div class="head-actions">
        <button class="btn" id="fin-import">Import CSV</button>
        <button class="btn" id="fin-accounts">Accounts</button>
        <button class="btn" id="fin-categories">Categories</button>
      </div>
    </div>

    <div class="joint-card fin-quick">
      <form id="fin-quick-form">
        <div class="fin-quick-grid">
          <input type="text" id="fq-amount" inputmode="decimal" placeholder="0.00"
                 autocomplete="off" class="fin-amount-in mono">
          <input type="text" id="fq-merchant" placeholder="Merchant" autocomplete="off"
                 list="fin-merchants">
          <datalist id="fin-merchants">
            ${fCache.merchants.map((m) => `<option value="${escAttr(m.merchant)}">`).join("")}
          </datalist>
          <select id="fq-category">
            <option value="">Category…</option>
            <optgroup label="Spending">
              ${spend.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("")}
            </optgroup>
            <optgroup label="Income">
              ${income.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("")}
            </optgroup>
            <optgroup label="Transfers">
              ${transfer.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("")}
            </optgroup>
          </select>
          <select id="fq-account">
            ${active.map((a) =>
              `<option value="${a.id}" ${String(a.id) === lastAcct ? "selected" : ""}>${esc(a.name)}</option>`).join("")}
          </select>
          <input type="date" id="fq-date" value="${todayISO()}">
          <button type="submit" class="btn primary" id="fq-log">Log</button>
        </div>
        <label class="fin-income-toggle">
          <input type="checkbox" id="fq-credit"> money in (income / refund)
        </label>
      </form>
    </div>

    <div class="joint-card">
      <div class="field-row-inline fin-filters">
        <select id="ff-account">
          <option value="">All accounts</option>
          ${accounts.map((a) => `<option value="${a.id}" ${fFilters.account_id == a.id ? "selected" : ""}>${esc(a.name)}</option>`).join("")}
        </select>
        <select id="ff-category">
          <option value="">All categories</option>
          ${categories.map((c) => `<option value="${c.id}" ${fFilters.category_id == c.id ? "selected" : ""}>${esc(c.name)}</option>`).join("")}
        </select>
        <button class="chip ${fFilters.uncategorized ? "on c-amber" : ""}" id="ff-unfiled">unfiled</button>
        <input type="text" id="ff-q" placeholder="Search…" value="${escAttr(fFilters.q)}">
      </div>
      <div id="fin-list"><div class="loading">Loading…</div></div>
    </div>`;

  // ---- quick entry ----
  const amountIn = el("fq-amount");
  amountIn.focus();

  // Picking a known merchant pre-fills where it usually goes.
  el("fq-merchant").addEventListener("change", () => {
    const m = fCache.merchants.find(
      (x) => x.merchant.toLowerCase() === el("fq-merchant").value.trim().toLowerCase());
    if (!m) return;
    if (m.category_id) el("fq-category").value = m.category_id;
    if (m.account_id && active.some((a) => a.id === m.account_id)) {
      el("fq-account").value = m.account_id;
    }
  });

  el("fin-quick-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const amount = amountIn.value.trim();
    const merchant = el("fq-merchant").value.trim();
    if (!amount) { toast("Amount first", "error"); amountIn.focus(); return; }
    if (!merchant) { toast("Who was it?", "error"); el("fq-merchant").focus(); return; }

    const payload = {
      amount,
      merchant,
      account_id: Number(el("fq-account").value),
      posted_date: el("fq-date").value,
      direction: el("fq-credit").checked ? "credit" : "debit",
      category_id: el("fq-category").value ? Number(el("fq-category").value) : null,
    };
    const btn = el("fq-log");
    btn.disabled = true;
    try {
      await postTransaction(payload);
      localStorage.setItem(finAcctKey(), String(payload.account_id));
      toast(`Logged ${merchant} — ${amount.startsWith("$") ? amount : "$" + amount}`);
      amountIn.value = ""; el("fq-merchant").value = "";
      el("fq-category").value = ""; el("fq-credit").checked = false;
      amountIn.focus();
      refreshMerchants();
      loadTransactions(true);
    } catch (err) { /* toasted */ } finally {
      btn.disabled = false;
    }
  });

  // ---- filters ----
  el("ff-account").addEventListener("change", (e) => {
    fFilters.account_id = e.target.value; loadTransactions(true);
  });
  el("ff-category").addEventListener("change", (e) => {
    fFilters.category_id = e.target.value; loadTransactions(true);
  });
  el("ff-unfiled").addEventListener("click", () => {
    fFilters.uncategorized = !fFilters.uncategorized;
    el("ff-unfiled").classList.toggle("on", fFilters.uncategorized);
    el("ff-unfiled").classList.toggle("c-amber", fFilters.uncategorized);
    loadTransactions(true);
  });
  let qt = null;
  el("ff-q").addEventListener("input", (e) => {
    clearTimeout(qt);
    qt = setTimeout(() => { fFilters.q = e.target.value.trim(); loadTransactions(true); }, 300);
  });

  el("fin-import").addEventListener("click", openImportModal);
  el("fin-accounts").addEventListener("click", openAccountsModal);
  el("fin-categories").addEventListener("click", openCategoriesModal);

  loadTransactions(true);
}

// A duplicate is a 409 the server explains; offer the override rather than
// failing or silently double-logging.
async function postTransaction(payload) {
  try {
    return await API.post("/finance/transactions", payload);
  } catch (err) {
    if (/already logged/.test(err.message)
        && confirm("Identical transaction already logged that day. Add anyway as a separate one?")) {
      return API.post("/finance/transactions", { ...payload, force: true });
    }
    throw err;
  }
}

async function refreshMerchants() {
  fCache.merchants = await API.get("/finance/merchants");
  const dl = el("fin-merchants");
  if (dl) dl.innerHTML = fCache.merchants.map((m) => `<option value="${escAttr(m.merchant)}">`).join("");
}

// ---------- the list ----------
// Re-renders only its own container: a full renderFinance() after every
// submit would steal focus from the quick form and defeat its purpose.
async function loadTransactions(reset) {
  const box = el("fin-list");
  if (!box) return;
  if (reset) fNextBefore = null;

  const params = new URLSearchParams();
  if (fFilters.account_id) params.set("account_id", fFilters.account_id);
  if (fFilters.category_id) params.set("category_id", fFilters.category_id);
  if (fFilters.uncategorized) params.set("uncategorized", "true");
  if (fFilters.q) params.set("q", fFilters.q);
  if (fNextBefore) params.set("before", fNextBefore);

  const d = await API.get(`/finance/transactions?${params}`);
  fNextBefore = d.next_before;

  const rowsHtml = (rows) => {
    let lastDate = null;
    return rows.map((t) => {
      const day = t.posted_date !== lastDate
        ? `<tr class="fin-day"><td colspan="4">${fmtDate(t.posted_date)}</td></tr>` : "";
      lastDate = t.posted_date;
      const sign = t.direction === "credit" ? "+" : "−";
      return `${day}
        <tr class="fin-row clickable" data-tx="${t.id}">
          <td>${esc(t.merchant_raw)}${t.is_pending ? ` <span class="chip static c-gray">pending</span>` : ""}</td>
          <td>${finCatChip(t)}</td>
          <td class="card-meta">${esc(t.account_name)}</td>
          <td class="mono fin-amt ${t.direction}">${sign}${fmtMoney(t.amount_cents)}</td>
        </tr>`;
    }).join("");
  };

  const html = `
    <div class="block-title-row">
      <div class="block-title">Transactions</div>
      <span class="card-meta">${d.total} total</span>
    </div>
    <div class="h-table-wrap">
      <table class="h-table fin-table"><tbody id="fin-rows">
        ${rowsHtml(d.rows) || `<tr><td colspan="4"><p class="empty-state small">Nothing yet. Log the first one above.</p></td></tr>`}
      </tbody></table>
    </div>
    ${fNextBefore ? `<button class="btn small" id="fin-more">Load more</button>` : ""}`;

  if (reset || !el("fin-rows")) {
    box.innerHTML = html;
  } else {
    el("fin-rows").insertAdjacentHTML("beforeend", rowsHtml(d.rows));
    el("fin-more")?.remove();
    if (fNextBefore) el("fin-rows").closest("#fin-list")
      .insertAdjacentHTML("beforeend", `<button class="btn small" id="fin-more">Load more</button>`);
  }

  box.querySelectorAll("[data-tx]").forEach((r) =>
    r.addEventListener("click", () => openTxModal(Number(r.dataset.tx))));
  el("fin-more")?.addEventListener("click", () => loadTransactions(false));
}

// ---------- edit one transaction ----------
async function openTxModal(id) {
  const t = await API.get(`/finance/transactions/${id}`);
  if (!t || t.error) return;
  const cats = fCache.categories;

  openModal(`
    <div class="modal-head">
      <h2 class="modal-title">${esc(t.merchant_raw)}</h2>
      <button class="icon-btn" onclick="closeModal()">×</button>
    </div>
    <p class="card-meta">${esc(t.account_name)} · logged via ${esc(t.source)} ·
      category set by ${esc(t.category_source)}</p>

    <div class="field-row">
      <div><label class="field-label">Amount</label>
        <input type="text" id="tx-amount" inputmode="decimal" class="mono"
               value="${(t.amount_cents / 100).toFixed(2)}"></div>
      <div><label class="field-label">Date</label>
        <input type="date" id="tx-date" value="${t.posted_date}"></div>
    </div>
    <label class="field-label">Category</label>
    <select id="tx-category">
      <option value="">— uncategorized —</option>
      ${cats.map((c) => `<option value="${c.id}" ${t.category_id === c.id ? "selected" : ""}>${esc(c.name)}</option>`).join("")}
    </select>
    <label class="field-label">Notes</label>
    <textarea id="tx-notes" rows="2">${esc(t.notes || "")}</textarea>
    <label class="fin-income-toggle">
      <input type="checkbox" id="tx-pending" ${t.is_pending ? "checked" : ""}> pending
    </label>

    <div class="modal-actions">
      <button class="btn danger" id="tx-delete">Delete</button>
      <button class="btn primary" id="tx-save">Save</button>
    </div>
  `, () => {
    el("tx-save").addEventListener("click", async () => {
      await API.patch(`/finance/transactions/${id}`, {
        amount: el("tx-amount").value.trim(),
        posted_date: el("tx-date").value,
        category_id: el("tx-category").value ? Number(el("tx-category").value) : null,
        notes: el("tx-notes").value.trim(),
        is_pending: el("tx-pending").checked ? 1 : 0,
      });
      toast("Saved"); closeModal(); loadTransactions(true);
    });
    el("tx-delete").addEventListener("click", async () => {
      if (!confirm("Delete this transaction? There is no undo.")) return;
      await API.del(`/finance/transactions/${id}`);
      toast("Deleted"); closeModal(); loadTransactions(true); refreshMerchants();
    });
  });
}

// ---------- accounts ----------
async function openAccountsModal() {
  const accounts = await API.get("/finance/accounts");
  openModal(`
    <div class="modal-head">
      <h2 class="modal-title">Accounts</h2>
      <button class="icon-btn" onclick="closeModal()">×</button>
    </div>
    ${accounts.map((a) => `
      <div class="feed-row">
        <span class="feed-main">
          <span class="feed-name">${esc(a.name)} ${a.is_active ? "" : "· retired"}</span>
          <span class="card-meta">${esc(a.type)}${a.institution ? " · " + esc(a.institution) : ""} · ${a.tx_count} transactions</span>
        </span>
        <button class="btn tiny" data-toggle="${a.id}" data-on="${a.is_active}">
          ${a.is_active ? "Retire" : "Restore"}</button>
      </div>`).join("")}
    <label class="field-label">Add an account</label>
    <div class="field-row-inline">
      <input type="text" id="acc-name" placeholder="Name">
      <select id="acc-type">
        <option value="checking">checking</option><option value="credit">credit</option>
        <option value="cash">cash</option><option value="other">other</option>
      </select>
      <input type="text" id="acc-inst" placeholder="Institution (optional)">
      <button class="btn primary" id="acc-add">Add</button>
    </div>
    <p class="notes-gate-hint">Retiring an account hides it from entry; its
    history stays. Accounts are never deleted — the ledger is the record.</p>
  `, (modal) => {
    el("acc-add").addEventListener("click", async () => {
      const name = el("acc-name").value.trim();
      if (!name) { toast("Name it", "error"); return; }
      await API.post("/finance/accounts", {
        name, type: el("acc-type").value,
        institution: el("acc-inst").value.trim() || null,
      });
      toast("Added"); closeModal(); renderFinance();
    });
    modal.querySelectorAll("[data-toggle]").forEach((b) =>
      b.addEventListener("click", async () => {
        await API.patch(`/finance/accounts/${b.dataset.toggle}`,
                        { is_active: b.dataset.on === "1" ? 0 : 1 });
        closeModal(); renderFinance();
      }));
  });
}

// ---------- categories ----------
async function openCategoriesModal() {
  const cats = await API.get("/finance/categories");
  openModal(`
    <div class="modal-head">
      <h2 class="modal-title">Categories</h2>
      <button class="icon-btn" onclick="closeModal()">×</button>
    </div>
    <div class="fin-cat-list">
      ${cats.map((c) => `
        <div class="feed-row">
          <span class="dot dot-${esc(c.color || "gray")}"></span>
          <span class="feed-main">
            <span class="feed-name">${esc(c.name)}</span>
            <span class="card-meta">${c.is_transfer ? "excluded from spending · " : ""}${c.is_income ? "income · " : ""}${c.tx_count} transactions</span>
          </span>
        </div>`).join("")}
    </div>
    <label class="field-label">Add a category</label>
    <div class="field-row-inline">
      <input type="text" id="cat-name" placeholder="Name">
      <select id="cat-color">
        ${LABEL_COLORS.map((c) => `<option value="${c}">${c}</option>`).join("")}
      </select>
      <label class="fin-income-toggle"><input type="checkbox" id="cat-income"> income</label>
      <button class="btn primary" id="cat-add">Add</button>
    </div>
  `, () => {
    el("cat-add").addEventListener("click", async () => {
      const name = el("cat-name").value.trim();
      if (!name) { toast("Name it", "error"); return; }
      await API.post("/finance/categories", {
        name, color: el("cat-color").value, is_income: el("cat-income").checked,
      });
      toast("Added"); closeModal(); renderFinance();
    });
  });
}

// ---------- CSV import ----------
// upload -> preview (nothing written) -> user reviews new vs duplicate ->
// commit. Duplicates ship with an explicit per-row "import anyway" check.
let fPreview = null;

async function openImportModal() {
  const active = fCache.accounts.filter((a) => a.is_active);
  openModal(`
    <div class="modal-head">
      <h2 class="modal-title">Import CSV</h2>
      <button class="icon-btn" onclick="closeModal()">×</button>
    </div>
    <label class="field-label">Into account</label>
    <select id="imp-account">
      ${active.map((a) => `<option value="${a.id}">${esc(a.name)}</option>`).join("")}
    </select>
    <label class="field-label">File</label>
    <input type="file" id="imp-file" accept=".csv,text/csv">
    <p class="notes-gate-hint">Capital One and Discover exports are recognized
    automatically; anything else asks you to point at the right columns.
    Nothing is written until you confirm the preview.</p>
    <div id="imp-body"></div>
    <div class="modal-actions">
      <button class="btn primary" id="imp-preview">Preview</button>
    </div>
  `, () => {
    el("imp-preview").addEventListener("click", () => runImportPreview(null));
  });
}

async function runImportPreview(mapping) {
  const file = el("imp-file").files[0];
  if (!file) { toast("Choose a file", "error"); return; }
  const fd = new FormData();
  fd.append("file", file);
  fd.append("account_id", el("imp-account").value);
  if (mapping) fd.append("mapping", JSON.stringify(mapping));

  let r;
  try {
    r = await API.upload("/finance/import/preview", fd);
  } catch (err) {
    // 422 = unrecognized format; the response carried the headers, but the
    // API client throws on non-2xx - refetch shape via a manual fetch is
    // overkill, so parse headers client-side for the mapping UI instead.
    if (/unrecognized format/.test(err.message)) {
      const text = await file.text();
      const headers = text.split(/\r?\n/)[0].split(",").map((h) => h.replace(/^"|"$/g, "").trim());
      renderMappingUI(headers);
      return;
    }
    return;
  }
  fPreview = r;

  const shown = r.rows.slice(0, 200);
  el("imp-body").innerHTML = `
    <div class="block-title-row">
      <div class="block-title">${r.new_count} new · ${r.duplicate_count} duplicate${
        r.skipped_unparseable ? ` · ${r.skipped_unparseable} unparseable` : ""}</div>
      <span class="card-meta">${esc(r.format || "custom")}</span>
    </div>
    <div class="h-table-wrap fin-preview">
      <table class="h-table"><thead>
        <tr><th>Date</th><th>Merchant</th><th>Amount</th><th></th></tr></thead>
        <tbody>
        ${shown.map((row, i) => `
          <tr class="${row.duplicate ? "fin-dup" : ""}">
            <td class="mono">${esc(row.posted_date)}</td>
            <td>${esc(row.merchant_raw)}</td>
            <td class="mono">${row.direction === "credit" ? "+" : "−"}${fmtMoney(row.amount_cents)}</td>
            <td>${row.duplicate
              ? `<label class="fin-income-toggle"><input type="checkbox" data-force="${i}"> import anyway</label>`
              : "new"}</td>
          </tr>`).join("")}
        </tbody>
      </table>
    </div>
    ${r.rows.length > 200 ? `<p class="card-meta">Showing 200 of ${r.rows.length} rows; all are counted.</p>` : ""}
    <div class="modal-actions">
      <button class="btn primary" id="imp-commit">Import ${r.new_count} new</button>
    </div>`;

  el("imp-commit").addEventListener("click", async () => {
    const forced = new Set(
      [...document.querySelectorAll("[data-force]:checked")].map((c) => Number(c.dataset.force)));
    const rows = fPreview.rows
      .map((row, i) => ({ ...row, force: forced.has(i) }))
      .filter((row) => !row.duplicate || row.force);
    if (!rows.length) { toast("Nothing selected to import", "error"); return; }
    const btn = el("imp-commit");
    btn.disabled = true; btn.textContent = "Importing…";
    const res = await API.post("/finance/import/commit", {
      account_id: Number(el("imp-account").value), rows,
    });
    toast(`Imported ${res.imported} · skipped ${res.skipped_duplicates} duplicates${
      res.imported_despite_duplicate ? ` · ${res.imported_despite_duplicate} forced` : ""}`, "info", 6000);
    closeModal(); renderFinance();
  });
}

function renderMappingUI(headers) {
  const opts = (sel) => `<option value="">—</option>` +
    headers.map((h) => `<option value="${escAttr(h)}" ${h === sel ? "selected" : ""}>${esc(h)}</option>`).join("");
  el("imp-body").innerHTML = `
    <div class="block-title">Unrecognized format — map the columns</div>
    <div class="field-row">
      <div><label class="field-label">Date column</label><select id="map-date">${opts()}</select></div>
      <div><label class="field-label">Merchant column</label><select id="map-merchant">${opts()}</select></div>
    </div>
    <div class="field-row">
      <div><label class="field-label">Amount column</label><select id="map-amount">${opts()}</select></div>
      <div><label class="field-label">…or Debit / Credit columns</label>
        <div class="field-row-inline">
          <select id="map-debit">${opts()}</select><select id="map-credit">${opts()}</select>
        </div></div>
    </div>
    <label class="fin-income-toggle"><input type="checkbox" id="map-flip">
      charges are negative in this file</label>
    <div class="modal-actions">
      <button class="btn primary" id="map-go">Preview with this mapping</button>
    </div>`;
  el("map-go").addEventListener("click", () => {
    const mapping = {
      date_col: el("map-date").value, merchant_col: el("map-merchant").value,
      amount_col: el("map-amount").value || null,
      debit_col: el("map-debit").value || null, credit_col: el("map-credit").value || null,
      flip_sign: el("map-flip").checked,
    };
    if (!mapping.date_col || !mapping.merchant_col
        || (!mapping.amount_col && !mapping.debit_col && !mapping.credit_col)) {
      toast("Map date, merchant, and an amount column", "error"); return;
    }
    runImportPreview(mapping);
  });
}
