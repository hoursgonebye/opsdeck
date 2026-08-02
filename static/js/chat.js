// Mentor chat: a conversation with Claude Code running in the terminal
// container. The browser talks to /api/mentor/chat on this origin; the app
// proxies to the bridge. Nothing here knows the terminal exists.
//
// History is kept in memory for the tab plus the session id in localStorage,
// so switching sections and back doesn't wipe the conversation.

let chatLog = [];
let chatSession = localStorage.getItem("opsdeck-chat-session") || null;
let chatBusy = false;

async function renderChat() {
  const panel = el("panel");
  panel.innerHTML = `
    <div class="section-head-row">
      <h1 class="section-title">Mentor</h1>
      <div class="head-actions">
        <span id="chat-status" class="chat-status"></span>
        <button class="btn" id="chat-reset">New conversation</button>
      </div>
    </div>
    <p class="section-sub">
      Runs on your Claude subscription in the terminal container — it reads
      your real boards, tree and progress before it answers.
    </p>

    <div class="chat-wrap">
      <div class="chat-log" id="chat-log"></div>
      <div class="chat-compose">
        <textarea id="chat-input" rows="1"
                  placeholder="Ask the mentor… (Enter to send, Shift+Enter for a newline)"></textarea>
        <button class="btn primary" id="chat-send">Send</button>
      </div>
    </div>`;

  paintChat();

  el("chat-send").addEventListener("click", sendChat);
  el("chat-reset").addEventListener("click", () => {
    chatLog = [];
    chatSession = null;
    localStorage.removeItem("opsdeck-chat-session");
    paintChat();
  });

  const input = el("chat-input");
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 180) + "px";
  });
  input.focus();

  checkChatHealth();
}

async function checkChatHealth() {
  const badge = el("chat-status");
  if (!badge) return;
  try {
    const h = await API.get("/mentor/chat/health");
    if (!h.available) {
      badge.textContent = "terminal offline";
      badge.className = "chat-status bad";
    } else if (!h.logged_in) {
      badge.textContent = "not logged in";
      badge.className = "chat-status warn";
    } else {
      badge.textContent = "ready";
      badge.className = "chat-status ok";
    }
  } catch (e) {
    badge.textContent = "";
  }
}

function paintChat() {
  const box = el("chat-log");
  if (!box) return;

  if (!chatLog.length) {
    box.innerHTML = `
      <div class="chat-empty">
        <p>Ask about your tree, your boards, what to work on next — or open a
        level-up and let it grade you.</p>
        <div class="chat-suggestions">
          ${["What should I focus on this week?",
             "Where am I weakest in the tree?",
             "Look at my overdue cards and tell me what actually matters."]
            .map((s) => `<button class="chip" data-suggest="${escAttr(s)}">${esc(s)}</button>`).join("")}
        </div>
      </div>`;
    box.querySelectorAll("[data-suggest]").forEach((b) =>
      b.addEventListener("click", () => {
        el("chat-input").value = b.dataset.suggest;
        sendChat();
      }));
    return;
  }

  box.innerHTML = chatLog.map((m) => {
    if (m.role === "error") {
      return `<div class="chat-msg error"><div class="chat-bubble">${esc(m.text)}</div></div>`;
    }
    const inner = m.role === "mentor"
      ? renderMarkdown(m.text)          // reuse the docs renderer; escapes first
      : esc(m.text).replace(/\n/g, "<br>");
    return `
      <div class="chat-msg ${m.role}">
        <div class="chat-who">${m.role === "you" ? "you" : "mentor"}</div>
        <div class="chat-bubble">${inner}</div>
      </div>`;
  }).join("") + (chatBusy
    ? `<div class="chat-msg mentor"><div class="chat-who">mentor</div>
         <div class="chat-bubble thinking">thinking…</div></div>`
    : "");

  box.scrollTop = box.scrollHeight;
}

async function sendChat() {
  if (chatBusy) return;
  const input = el("chat-input");
  const text = input.value.trim();
  if (!text) return;

  chatLog.push({ role: "you", text });
  input.value = "";
  input.style.height = "auto";
  chatBusy = true;
  paintChat();

  try {
    const res = await API.post("/mentor/chat", { message: text, session: chatSession });
    chatSession = res.session || chatSession;
    if (chatSession) localStorage.setItem("opsdeck-chat-session", chatSession);
    chatLog.push({ role: "mentor", text: res.reply || "(no reply)" });
  } catch (e) {
    // API.request already toasted; put it in the transcript too so the
    // conversation shows what happened rather than silently stalling.
    chatLog.push({ role: "error", text: e.message || "Mentor unreachable." });
  } finally {
    chatBusy = false;
    paintChat();
    checkChatHealth();
  }
}
