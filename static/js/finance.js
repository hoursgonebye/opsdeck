// Finance: a personal ledger with envelope budgets and an AI assist.
//
// Two views behind one section. Ledger is the quick-entry form (the whole
// point - log a purchase in seconds) plus the transaction list; Budgets is
// balances, envelopes, to-be-budgeted, recurring charges, and the AI
// review/ask panel. The overview strip on top shows what the accounts are
// actually worth, derived server-side from each account's balance anchor
// plus the ledger - nothing here computes money, it only formats cents.

let fFilters = { account_id: "", category_id: "", uncategorized: false, q: "" };
let fCache = { accounts: [], categories: [], merchants: [], summary: null };
let fNextBefore = null;
let fView = "ledger";                       // ledger | budgets
let fPeriod = null;                         // YYYY-MM, defaults to current

function fmtMoney(cents) {
  const d = Math.abs(cents) / 100;
  return "$" + d.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function finCatChip(t) {
  if (!t.category_id) {
    return `<span class="chip static c-amber">unfiled</span>`;
  }
  const color = t.category_color || "gray";
  const aiMark = t.category_source === "ai" ? " ✦" : "";
  return `<span class="chip static c-${esc(color)}" title="${
    t.category_source === "ai" ? "categorized by AI - click row to review" : ""
  }">${esc(t.category_name)}${aiMark}</span>`;
}

function finAcctKey() { return `finAcct:${window.OPSDECK.activeProfile || "primary"}`; }

function thisMonth() {
  const d = new Date();
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;
}

async function renderFinance() {
  const panel = el("panel");
  panel.innerHTML = `<div class="loading">Loading…</div>`;
  if (!fPeriod) fPeriod = thisMonth();

  const [accounts, categories, merchants, summary] = await Promise.all([
    API.get("/finance/accounts"),
    API.get("/finance/categories"),
    API.get("/finance/merchants"),
    API.get(`/finance/summary?period=${fPeriod}`).catch(() => null),
  ]);
  fCache = { accounts, categories, merchants, summary };

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

  // ---- overview strip: what everything is worth, server-derived ----
  const balances = summary ? summary.balances : [];
  const strip = balances.map((b) => `
    <div class="fin-tile" title="${escAttr(b.basis)}">
      <div class="h-label">${esc(b.name)}${b.type === "credit" ? " (owed)" : ""}</div>
      <div class="h-value ${b.type === "credit" && b.balance_cents > 0 ? "fin-neg" : ""}">
        ${b.balance_cents < 0 ? "−" : ""}${fmtMoney(b.balance_cents)}</div>
      <div class="h-sub">${b.basis.startsWith("anchored") ? "as of " + esc(b.basis.slice(9)) : "unanchored estimate"}</div>
    </div>`).join("");
  const netTile = summary ? `
    <div class="fin-tile fin-net">
      <div class="h-label">Net position</div>
      <div class="h-value ${summary.net_cents < 0 ? "fin-neg" : ""}">
        ${summary.net_cents < 0 ? "−" : ""}${fmtMoney(summary.net_cents)}</div>
      <div class="h-sub">accounts − cards</div>
    </div>` : "";

  panel.innerHTML = `
    <div class="section-head-row">
      <h1 class="section-title">Finance</h1>
      <div class="head-actions">
        <button class="btn" id="fin-import">Import CSV</button>
        <button class="btn" id="fin-rules">Rules</button>
        <button class="btn" id="fin-accounts">Accounts</button>
        <button class="btn" id="fin-categories">Categories</button>
      </div>
    </div>

    <div class="fin-tiles">${strip}${netTile}</div>

    <div class="h-controls">
      <div class="h-views">
        <button class="board-tab ${fView === "ledger" ? "active" : ""}" data-fview="ledger">Ledger</button>
        <button class="board-tab ${fView === "budgets" ? "active" : ""}" data-fview="budgets">Budgets</button>
      </div>
    </div>
    <div id="fin-body"></div>`;

  panel.querySelectorAll("[data-fview]").forEach((b) =>
    b.addEventListener("click", () => { fView = b.dataset.fview; renderFinance(); }));
  el("fin-import").addEventListener("click", openImportModal);
  el("fin-rules").addEventListener("click", openRulesModal);
  el("fin-accounts").addEventListener("click", openAccountsModal);
  el("fin-categories").addEventListener("click", openCategoriesModal);

  if (fView === "budgets") renderBudgetsView(el("fin-body"));
  else renderLedgerView(el("fin-body"));
}

// =================================================================== ledger

function renderLedgerView(box) {
  const { accounts, categories, merchants, summary } = fCache;
  const lastAcct = localStorage.getItem(finAcctKey());
  const active = accounts.filter((a) => a.is_active);
  const spend = categories.filter((c) => !c.is_income && !c.is_transfer);
  const income = categories.filter((c) => c.is_income);
  const transfer = categories.filter((c) => c.is_transfer);
  const unfiled = summary ? summary.uncategorized.count : 0;

  box.innerHTML = `
    <div class="joint-card fin-quick">
      <form id="fin-quick-form">
        <div class="fin-quick-grid">
          <input type="text" id="fq-amount" inputmode="decimal" placeholder="0.00"
                 autocomplete="off" class="fin-amount-in mono">
          <input type="text" id="fq-merchant" placeholder="Merchant" autocomplete="off"
                 list="fin-merchants">
          <datalist id="fin-merchants">
            ${merchants.map((m) => `<option value="${escAttr(m.merchant)}">`).join("")}
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
        <span class="settings-hint">No category picked? Rules file known merchants
        automatically.</span>
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
        ${unfiled > 0 ? `<button class="btn small" id="fin-ai-cat">✦ AI categorize (${unfiled})</button>` : ""}
      </div>
      <div id="fin-list"><div class="loading">Loading…</div></div>
    </div>`;

  const amountIn = el("fq-amount");
  amountIn.focus();

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
      const tx = await postTransaction(payload);
      localStorage.setItem(finAcctKey(), String(payload.account_id));
      toast(tx.category_source === "rule"
        ? `Logged ${merchant} → filed by rule`
        : `Logged ${merchant} — ${amount.startsWith("$") ? amount : "$" + amount}`);
      amountIn.value = ""; el("fq-merchant").value = "";
      el("fq-category").value = ""; el("fq-credit").checked = false;
      amountIn.focus();
      refreshMerchants();
      loadTransactions(true);
    } catch (err) { /* toasted */ } finally {
      btn.disabled = false;
    }
  });

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
  el("fin-ai-cat")?.addEventListener("click", openAiCategorize);

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
      category set by ${esc(t.category_source)}${t.category_source === "ai" ? " ✦" : ""}</p>

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
      toast("Saved"); closeModal(); renderFinance();
    });
    el("tx-delete").addEventListener("click", async () => {
      if (!confirm("Delete this transaction? There is no undo.")) return;
      await API.del(`/finance/transactions/${id}`);
      toast("Deleted"); closeModal(); renderFinance();
    });
  });
}

// ================================================================== budgets

async function renderBudgetsView(box) {
  const s = fCache.summary;
  if (!s) { box.innerHTML = `<p class="empty-state">Could not load summary.</p>`; return; }

  const [y, m] = fPeriod.split("-").map(Number);
  const prev = m === 1 ? `${y - 1}-12` : `${y}-${pad(m - 1)}`;
  const next = m === 12 ? `${y + 1}-01` : `${y}-${pad(m + 1)}`;
  const monthName = new Date(y, m - 1, 1).toLocaleDateString(undefined,
    { month: "long", year: "numeric" });

  const budgeted = s.categories.filter((c) => c.limit_cents != null);
  const unbudgeted = s.categories.filter((c) => c.limit_cents == null && c.spent_cents !== 0);
  const tbb = s.to_be_budgeted_cents;

  const bar = (c) => {
    const limit = c.effective_limit_cents || 1;
    const pct = Math.min(100, Math.max(0, (c.spent_cents / limit) * 100));
    const over = c.remaining_cents < 0;
    return `
      <div class="fin-env" data-cat="${c.id}">
        <div class="fin-env-head">
          <span class="dot dot-${esc(c.color || "gray")}"></span>
          <span class="fin-env-name">${esc(c.name)}</span>
          <span class="mono ${over ? "fin-neg" : ""}">${fmtMoney(c.spent_cents)}
            / ${fmtMoney(c.effective_limit_cents)}</span>
        </div>
        <div class="h-wd-bar fin-env-bar ${over ? "over" : ""}">
          <span style="width:${pct}%"></span></div>
        <div class="card-meta">
          ${over ? `over by ${fmtMoney(-c.remaining_cents)}` : `${fmtMoney(c.remaining_cents)} left`}
          ${c.rollover ? ` · rollover${c.carry_cents ? ` (carry ${c.carry_cents < 0 ? "−" : ""}${fmtMoney(c.carry_cents)})` : ""}` : ""}
          <button class="btn tiny" data-edit-env="${c.id}">Edit</button>
        </div>
      </div>`;
  };

  box.innerHTML = `
    <div class="joint-card">
      <div class="block-title-row">
        <div class="block-title">
          <button class="btn tiny" id="bud-prev">‹</button>
          ${monthName}
          <button class="btn tiny" id="bud-next">›</button>
        </div>
        <span class="card-meta">income received ${fmtMoney(s.income_received_cents)}
          · spent ${fmtMoney(s.spend_total_cents)}</span>
      </div>

      <div class="fin-tbb ${tbb < 0 ? "fin-neg" : ""}">
        <span class="h-label">To be budgeted</span>
        <span class="h-value">${tbb < 0 ? "−" : ""}${fmtMoney(tbb)}</span>
        ${tbb < 0 ? `<span class="card-meta">budgets exceed income received this month</span>` : ""}
      </div>

      ${budgeted.map(bar).join("") || `<p class="empty-state small">No envelopes for ${monthName} yet.</p>`}

      <div class="field-row-inline">
        <select id="bud-cat">
          <option value="">Add envelope…</option>
          ${s.categories.filter((c) => c.limit_cents == null)
            .map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("")}
        </select>
        <input type="text" id="bud-limit" inputmode="decimal" placeholder="Limit" class="mono">
        <label class="fin-income-toggle"><input type="checkbox" id="bud-roll"> rollover</label>
        <button class="btn" id="bud-add">Set</button>
        <button class="btn" id="bud-copy">Copy last month</button>
      </div>

      ${unbudgeted.length ? `
        <p class="settings-hint">Spending without an envelope:
          ${unbudgeted.map((c) => `${esc(c.name)} ${fmtMoney(c.spent_cents)}`).join(" · ")}
          ${s.uncategorized.count ? ` · unfiled ${fmtMoney(s.uncategorized.spent_cents)}` : ""}</p>` : ""}
    </div>

    <div class="fin-two-col">
      <div class="joint-card">
        <div class="block-title-row">
          <div class="block-title">Recurring charges</div>
          <span class="card-meta">detected from the ledger</span>
        </div>
        <div id="fin-recurring"><div class="loading">Scanning…</div></div>
      </div>

      <div class="joint-card">
        <div class="block-title-row">
          <div class="block-title">✦ Review</div>
          <button class="btn tiny" id="rev-gen">Generate for ${monthName}</button>
        </div>
        <div id="fin-review"><div class="loading">Loading…</div></div>
        <p class="settings-hint">Questions — “can I afford X”, “what should I
        expect from my shifts” — go to the mentor (✦, bottom right). It reads
        these same numbers and runs on your subscription, not the metered API.</p>
      </div>
    </div>`;

  el("bud-prev").addEventListener("click", () => { fPeriod = prev; renderFinance(); });
  el("bud-next").addEventListener("click", () => { fPeriod = next; renderFinance(); });

  el("bud-add").addEventListener("click", async () => {
    const cid = el("bud-cat").value;
    const limit = el("bud-limit").value.trim();
    if (!cid || !limit) { toast("Pick a category and a limit", "error"); return; }
    await API.post("/finance/budgets", {
      category_id: Number(cid), period_start: `${fPeriod}-01`,
      limit, rollover: el("bud-roll").checked,
    });
    toast("Envelope set"); renderFinance();
  });

  el("bud-copy").addEventListener("click", async () => {
    const r = await API.post("/finance/budgets/copy-from",
      { source_period: prev, target_period: fPeriod });
    toast(r.copied ? `Copied ${r.copied} envelopes from last month`
                   : "Nothing to copy (or all already set)");
    renderFinance();
  });

  box.querySelectorAll("[data-edit-env]").forEach((b) =>
    b.addEventListener("click", () => {
      const c = budgeted.find((x) => x.id === Number(b.dataset.editEnv));
      const val = prompt(`Monthly limit for ${c.name} ($):`,
                         (c.limit_cents / 100).toFixed(2));
      if (val === null) return;
      if (val.trim() === "" || val.trim() === "0") {
        API.del(`/finance/budgets/${c.budget_id}`).then(() => renderFinance());
      } else {
        API.post("/finance/budgets", {
          category_id: c.id, period_start: `${fPeriod}-01`, limit: val.trim(),
          rollover: c.rollover,
        }).then(() => renderFinance());
      }
    }));

  loadRecurring();
  loadReview();

  el("rev-gen").addEventListener("click", async () => {
    const btn = el("rev-gen");
    btn.disabled = true; btn.textContent = "Thinking…";
    try {
      await API.post("/finance/ai/reviews/generate", { period_start: `${fPeriod}-01` });
      loadReview();
    } catch (e) { /* toasted */ }
    btn.disabled = false; btn.textContent = `Generate for ${monthName}`;
  });

}

async function loadRecurring() {
  const box = el("fin-recurring");
  if (!box) return;
  const rec = await API.get("/finance/recurring").catch(() => []);
  box.innerHTML = rec.length ? rec.slice(0, 10).map((r) => `
    <div class="feed-row">
      <span class="feed-main">
        <span class="feed-name">${esc(r.merchant)}</span>
        <span class="card-meta">~${fmtMoney(r.amount_cents)} every ${r.interval_days}d ·
          seen ${r.times_seen}× · last ${fmtDate(r.last_seen)}</span>
      </span>
      <span class="mono">${fmtMoney(r.monthly_cost_cents)}/mo</span>
    </div>`).join("")
    : `<p class="empty-state small">Nothing recurring detected yet — needs three
       similar charges at a regular interval.</p>`;
}

async function loadReview() {
  const box = el("fin-review");
  if (!box) return;
  const revs = await API.get(`/finance/ai/reviews?period=${fPeriod}`).catch(() => []);
  box.innerHTML = revs.length
    ? `<p class="fin-review-body">${esc(revs[0].body)}</p>
       <span class="card-meta">generated ${esc((revs[0].created_at || "").slice(0, 16))}</span>`
    : `<p class="empty-state small">No review for this month yet.</p>`;
}

// ============================================================ AI categorize

async function openAiCategorize() {
  openModal(`
    <div class="modal-head">
      <h2 class="modal-title">✦ AI categorize</h2>
      <button class="icon-btn" onclick="closeModal()">×</button>
    </div>
    <div id="aic-body"><div class="loading">Rules first, then the model…</div></div>
  `, async () => {
    let r;
    try {
      r = await API.post("/finance/ai/categorize", {});
    } catch (e) {
      el("aic-body").innerHTML = `<p class="empty-state">${esc(e.message)}</p>`;
      return;
    }
    if (!r.suggestions.length) {
      el("aic-body").innerHTML = `
        <p class="empty-state">${r.ruled
          ? `Rules filed ${r.ruled} on their own — nothing left for the model.`
          : "No suggestions."}</p>`;
      if (r.ruled) { loadTransactions(true); }
      return;
    }
    el("aic-body").innerHTML = `
      ${r.ruled ? `<p class="settings-hint">Rules filed ${r.ruled} first; the model saw only the rest.</p>` : ""}
      ${r.rules_suggested ? `<p class="settings-hint">${r.rules_suggested} new rules proposed — review them under Rules (inactive until you enable them).</p>` : ""}
      ${r.suggestions.map((s, i) => `
        <label class="fin-sugg">
          <input type="checkbox" data-sugg="${i}" ${s.confidence >= 0.7 ? "checked" : ""}>
          <span class="feed-main">
            <span class="feed-name">#${s.transaction_id}</span>
            <span class="card-meta">→ ${esc(s.category_name)} · ${Math.round(s.confidence * 100)}%</span>
          </span>
        </label>`).join("")}
      <div class="modal-actions">
        <button class="btn primary" id="aic-accept">Accept checked</button>
      </div>`;
    el("aic-accept").addEventListener("click", async () => {
      const chosen = [...document.querySelectorAll("[data-sugg]:checked")]
        .map((c) => r.suggestions[Number(c.dataset.sugg)])
        .map((s) => ({ transaction_id: s.transaction_id, category_id: s.category_id }));
      if (!chosen.length) { toast("Nothing checked", "error"); return; }
      const res = await API.post("/finance/ai/categorize/accept", { accepted: chosen });
      toast(`Applied ${res.applied}${res.skipped ? `, ${res.skipped} already handled` : ""}`);
      closeModal(); renderFinance();
    });
  });
}

// ==================================================================== rules

async function openRulesModal() {
  const rules = await API.get("/finance/rules");
  const cats = fCache.categories;
  openModal(`
    <div class="modal-head">
      <h2 class="modal-title">Category rules</h2>
      <button class="icon-btn" onclick="closeModal()">×</button>
    </div>
    <p class="settings-hint">First match wins, by priority. Rules file new and
    imported transactions; they never touch anything you categorized by hand.</p>
    <div class="fin-cat-list">
      ${rules.map((r) => `
        <div class="feed-row ${r.is_active ? "" : "fin-rule-off"}">
          <span class="feed-main">
            <span class="feed-name mono">${esc(r.match_type)} “${esc(r.pattern)}”</span>
            <span class="card-meta">→ ${esc(r.category_name)} · p${r.priority}
              ${r.origin === "ai_suggested" ? " · ✦ suggested" : ""}
              ${r.hit_count ? ` · ${r.hit_count} hits` : ""}
              ${r.account_name ? ` · ${esc(r.account_name)} only` : ""}</span>
          </span>
          <button class="btn tiny" data-rtoggle="${r.id}" data-on="${r.is_active}">
            ${r.is_active ? "Disable" : "Enable"}</button>
          <button class="btn tiny danger" data-rdrop="${r.id}">Delete</button>
        </div>`).join("") || `<p class="empty-state small">No rules yet. The AI can
          propose some, or add one below.</p>`}
    </div>
    <label class="field-label">Add a rule</label>
    <div class="field-row-inline">
      <select id="rule-type">
        <option value="contains">contains</option>
        <option value="starts_with">starts with</option>
        <option value="exact">exact</option>
        <option value="regex">regex</option>
      </select>
      <input type="text" id="rule-pattern" placeholder="pattern (matches lowercase merchant)">
      <select id="rule-cat">
        ${cats.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("")}
      </select>
      <button class="btn primary" id="rule-add">Add</button>
    </div>
    <div class="modal-actions">
      <button class="btn" id="rules-dry">Preview run</button>
      <button class="btn primary" id="rules-run">Run on unfiled</button>
    </div>
    <div id="rules-out"></div>
  `, (modal) => {
    el("rule-add").addEventListener("click", async () => {
      const pattern = el("rule-pattern").value.trim();
      if (!pattern) { toast("Pattern required", "error"); return; }
      await API.post("/finance/rules", {
        match_type: el("rule-type").value, pattern,
        category_id: Number(el("rule-cat").value),
      });
      toast("Rule added"); closeModal(); openRulesModal();
    });
    modal.querySelectorAll("[data-rtoggle]").forEach((b) =>
      b.addEventListener("click", async () => {
        await API.patch(`/finance/rules/${b.dataset.rtoggle}`,
                        { is_active: b.dataset.on === "1" ? 0 : 1 });
        closeModal(); openRulesModal();
      }));
    modal.querySelectorAll("[data-rdrop]").forEach((b) =>
      b.addEventListener("click", async () => {
        await API.del(`/finance/rules/${b.dataset.rdrop}`);
        closeModal(); openRulesModal();
      }));
    const run = async (dry) => {
      const r = await API.post(`/finance/rules/apply?dry_run=${dry}`);
      el("rules-out").innerHTML = `
        <p class="settings-hint">${dry ? "Would file" : "Filed"} ${r.matched}
        of ${r.scanned} unfiled transactions.</p>
        ${r.changes.slice(0, 20).map((c) =>
          `<div class="card-meta">${esc(c.merchant)} → ${esc(c.category_name)}</div>`).join("")}`;
      if (!dry && r.matched) { loadTransactions(true); }
    };
    el("rules-dry").addEventListener("click", () => run(true));
    el("rules-run").addEventListener("click", () => run(false));
  });
}

// ================================================================= accounts

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
          <span class="card-meta">${esc(a.type)}${a.institution ? " · " + esc(a.institution) : ""}
            · ${a.tx_count} transactions
            ${a.balance_anchor_date ? ` · anchored ${esc(a.balance_anchor_date)}` : " · no balance anchor"}</span>
        </span>
        <button class="btn tiny" data-anchor="${a.id}">Set balance</button>
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
    <p class="notes-gate-hint">"Set balance" anchors the account: today's true
    balance (for cards, the amount owed), from which the shown balance is
    derived as the ledger grows. A 360 Checking import anchors automatically.</p>
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
    modal.querySelectorAll("[data-anchor]").forEach((b) =>
      b.addEventListener("click", async () => {
        const a = accounts.find((x) => x.id === Number(b.dataset.anchor));
        const val = prompt(
          a.type === "credit"
            ? `Current amount owed on ${a.name} ($):`
            : `Current balance of ${a.name} ($):`);
        if (val === null || !val.trim()) return;
        try {
          await API.patch(`/finance/accounts/${a.id}`, { balance: val.trim() });
          toast("Anchored"); closeModal(); renderFinance();
        } catch (e) { /* toasted */ }
      }));
  });
}

// =============================================================== categories

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

// =============================================================== CSV import
// upload -> preview (nothing written) -> user reviews new vs duplicate ->
// commit. Duplicates ship with an explicit per-row "import anyway" check.
// Formats that carry a running balance (360 Checking) also anchor the
// account's derived balance at commit.
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
    <p class="notes-gate-hint">Capital One (card and 360 Checking) and Discover
    exports are recognized automatically; anything else asks you to point at
    the right columns. Nothing is written until you confirm the preview.</p>
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
    ${r.anchor ? `<p class="settings-hint">Ending balance ${fmtMoney(r.anchor.cents)}
      as of ${esc(r.anchor.date)} — will anchor this account's balance.</p>` : ""}
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
      anchor: fPreview.anchor || null,
    });
    toast(`Imported ${res.imported} · skipped ${res.skipped_duplicates} duplicates${
      res.anchored ? " · balance anchored" : ""}`, "info", 6000);
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
