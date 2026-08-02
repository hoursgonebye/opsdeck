"""
Chat bridge: turns `claude -p` into an HTTP endpoint so the Ops Deck web UI
can host a mentor conversation.

Runs inside the terminal container next to ttyd. It is NOT published to the
host - only the Ops Deck app container can reach it, over the shared docker
network, and it still requires the shared token. The app proxies to it so
the browser only ever talks to its own origin.

Auth for Claude itself comes from ~/.claude, i.e. the interactive
subscription login done once in the web terminal. No ANTHROPIC_API_KEY is
present, deliberately - this must not bill the API.
"""
import json
import os
import re
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("OPSDECK_TOKEN", "")
OPSDECK_URL = os.environ.get("OPSDECK_URL", "http://opsdeck:5000")
WORKDIR = "/workspace"
TIMEOUT = int(os.environ.get("BRIDGE_TIMEOUT", "180"))

# Sessions we've already created, so we know to --resume rather than re-open.
_seen = set()
_lock = threading.Lock()

SYSTEM = f"""You are the mentor inside Ops Deck, talking to the user in a chat \
panel embedded in their dashboard. Keep replies short and conversational - \
this is a chat box, not a document. No headers, no bullet-point walls unless \
they actually asked for a list.

The Ops Deck API is at {OPSDECK_URL} and the token is in $OPSDECK_TOKEN. Use \
curl to read their real data before answering anything about their boards, \
tree, routines, or progress. Never guess at their state - look it up. \
GET /api/context gives you everything in one call.

Everything is per-profile. Send X-Profile-Id: primary, partner or joint on \
every content call - boards, calendar, routines, docs, the skill tree, \
attributes and the attempt queue are all scoped by it. Omitting the header \
silently gives you the primary profile, which is the wrong answer when they \
are asking about someone else. /api/joint/* is household-wide and ignores it.

You are a strict examiner, not a cheerleader. The burden of proof is on them \
and "not yet" is a real answer. Be direct without being unkind.

You have full read/write access to their data, including DELETE. Use it when \
they ask - removing a card, node, routine or doc they pointed at is a normal \
request, not something to refuse or defer to an approval queue.

Two things still hold. Deletion is permanent and cascades: removing a board \
takes its lists and cards with it, removing a skill node takes its level \
history. Say what will disappear before doing it, and when a request is \
ambiguous about scope, ask which one they meant instead of guessing wide. \
And changes THEY did not ask for - restructuring a board, pruning the tree \
on your own initiative - still go through POST /api/proposals so they see a \
summary first. The rule is about who initiated the change, not how \
destructive it is."""

ALLOWED = "Bash,Read,Glob,Grep,Write,Edit"

# Errors that mean "this session is unusable" rather than "the request was
# bad" - worth one silent retry on a fresh session id.
SESSION_TROUBLE = re.compile(
    r"session id .* (already in use|not found)|no conversation found|"
    r"could not (find|resume) session|invalid session",
    re.I,
)


def _logged_in():
    """
    Whether an interactive login has actually happened. The ~/.claude
    directory always exists (the image creates it and a volume mounts over
    it), so its presence proves nothing - look for real credential files
    with content in them.
    """
    home = os.path.expanduser("~")
    for p in (os.path.join(home, ".claude", ".credentials.json"),
              os.path.join(home, ".claude.json")):
        try:
            if os.path.getsize(p) > 2:
                return True
        except OSError:
            continue
    return False


def _session_exists(session):
    """
    Whether Claude Code already has this session on disk.

    This used to be tracked in an in-memory set, which was wrong: the browser
    keeps its session id in localStorage forever, but the set is empty after
    any container restart. A returning client then looked "new", so we passed
    --session-id for an id that already existed and Claude refused with
    "Session ID ... is already in use". Disk is the actual source of truth
    and survives restarts.
    """
    root = os.path.expanduser("~/.claude/projects")
    if not os.path.isdir(root):
        return False
    for proj in os.listdir(root):
        if os.path.exists(os.path.join(root, proj, f"{session}.jsonl")):
            return True
    return False


def run_claude(message, session):
    """Run one turn. Returns (reply_text, error_or_None)."""
    with _lock:
        known = session in _seen or _session_exists(session)
        _seen.add(session)

    cmd = ["claude", "-p", message, "--output-format", "json",
           "--allowedTools", ALLOWED, "--append-system-prompt", SYSTEM]
    cmd += ["--resume", session] if known else ["--session-id", session]

    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)   # belt and braces: never bill the API

    try:
        p = subprocess.run(cmd, cwd=WORKDIR, env=env, capture_output=True,
                           text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, "The mentor took too long and timed out."
    except FileNotFoundError:
        return None, "claude CLI is not installed in this container."

    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()

    if p.returncode != 0 or not out:
        blob = (err + " " + out).lower()
        if any(w in blob for w in ("not logged in", "unauthor", "authentication",
                                   "login", "oauth", "credentials")):
            return None, ("Claude Code isn't logged in yet. Open the web "
                          "terminal, run `claude`, and complete the login "
                          "once - then this chat works.")
        return None, (err or "claude exited with no output")[:400]

    # --output-format json returns one object with a `result` string.
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return out, None
    if isinstance(data, dict):
        if data.get("is_error"):
            return None, str(data.get("result") or "mentor error")[:400]
        return data.get("result") or "", None
    return out, None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, payload):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "logged_in": _logged_in()})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if TOKEN and self.headers.get("X-Bridge-Token") != TOKEN:
            self._send(401, {"error": "bad bridge token"})
            return
        if self.path != "/chat":
            self._send(404, {"error": "not found"})
            return

        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send(400, {"error": "bad json"})
            return

        message = (body.get("message") or "").strip()
        if not message:
            self._send(400, {"error": "empty message"})
            return

        session = body.get("session") or str(uuid.uuid4())
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", session):
            session = str(uuid.uuid4())

        reply, error = run_claude(message, session)

        # A session can still be unusable - corrupted, deleted underneath us,
        # or claimed by another process. Losing the thread is annoying;
        # refusing to answer at all is worse. Retry once on a fresh session
        # and tell the client which id to use from now on.
        if error and SESSION_TROUBLE.search(error or ""):
            session = str(uuid.uuid4())
            reply, error = run_claude(message, session)

        if error:
            self._send(502, {"error": error, "session": session})
        else:
            self._send(200, {"reply": reply, "session": session})

    def log_message(self, *a):
        pass   # quiet; docker logs are for ttyd


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 7682), Handler).serve_forever()
