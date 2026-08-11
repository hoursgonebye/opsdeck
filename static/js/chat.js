// Mentor dock: a floating button in the bottom-right corner that opens a
// chat drawer over whatever section is on screen. It used to be a full tab;
// a mentor you have to navigate to is a mentor you stop consulting, so now
// it follows you everywhere and never steals your place.
//
// The conversation still runs on the Claude subscription in the terminal
// container - the browser talks to /api/mentor/chat on this origin, the app
// proxies to the bridge, and nothing here knows the terminal exists.
//
// History is kept in memory for the tab plus the session id in localStorage,
// so closing and reopening the dock doesn't wipe the conversation.

let chatLog = [];
let chatSession = localStorage.getItem("opsdeck-chat-session") || null;
let chatBusy = false;
let chatHealthChecked = false;

function initMentorDock() {
  const fab = el("mentor-fab");
  const dock = el("mentor-dock");
  if (!fab || !dock) return;

  fab.addEventListener("click", () => {
    const opening = dock.hidden;
    dock.hidden = !dock.hidden;
    fab.classList.toggle("open", opening);
    if (opening) {
      paintChat();
      el("chat-input").focus();
      if (!chatHealthChecked) { checkChatHealth(); chatHealthChecked = true; }
    }
  });
  el("mentor-close").addEventListener("click", () => {
    dock.hidden = true;
    fab.classList.remove("open");
  });

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
    input.style.height = Math.min(input.scrollHeight, 140) + "px";
  });
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
        <p>Your aide — already briefed on your day, your money, your tree.
        Ask anything.</p>
        <div class="chat-suggestions">
          ${["How am I set up for this week?",
             "How much should I expect from my next paychecks?",
             "How will my finances look by the end of the month?",
             "What should I focus on today?"]
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

// The dock's elements are static HTML above the script tags, so this can
// bind immediately at load.
initMentorDock();
