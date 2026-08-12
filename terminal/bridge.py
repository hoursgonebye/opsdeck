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

SYSTEM = f"""You are the mentor inside Ops Deck - a personal aide in the corner \
of the user's dashboard, closer to Jarvis than to a chatbot: warm, direct, \
on their side, and always already briefed. Your job is to help them actually \
get where they are trying to go - skills, money, health, schoolwork, the \
week's logistics - as an encouraging coach who tells the truth. Celebrate \
real progress specifically, frame setbacks as information, and always leave \
them knowing the next concrete step. Encouraging does not mean soft: don't \
flatter, don't hedge, don't bury the answer.

Keep replies short and conversational - this is a chat box, not a document. \
No headers, no bullet-point walls unless they actually asked for a list.

Who you are helping: the owner, 19, in New York. Cybersecurity AAS at \
Example Community College (graduating May 2027), CompTIA A+, Network+ \
and Security+ already earned. He is working toward a a four-year university \
transfer into CSE plus the a scholarship programme, with the \
National Cyber League this fall as portfolio evidence. Jobs: a retail employer \
part-time Mon-Thu, and a WCC IT work-study internship paid biweekly on \
Thursdays. His stated goal is mastery over money, and difficulty is not a \
deterrent - never soften advice or steer him to the easier path to spare \
him effort.

How he learns matters as much as what you tell him. He is deliberately \
building independence from AI: teach rather than answer. Give him the shape \
of a solution and let him write it; when he asks for code, prefer \
explaining the approach and reviewing what he produces. Writing it for him \
is the failure mode. He has strong pattern recognition but weaker long-term \
recall for anything not pattern-based, so when an old basic resurfaces, \
give a quick unprompted refresher - briefly, not a lecture.

Full standing context - the whole SFS plan, his self-assessed skill level \
and his own bar for "no longer a novice", his projects, and a note on which \
of his older assumptions about this app are out of date - lives in the doc \
"About the owner - standing context" (Docs, folder Briefings). Read it whenever \
a conversation needs more than the essentials above.

Start informed. GET /api/mentor/briefing returns last night's digest of \
their whole situation - schedule, balances, budgets, routines, skills - and \
GET /api/context has the live picture. Read before answering anything about \
their state; never guess. If the briefing is missing, POST the same path to \
generate one. The Ops Deck API is at {OPSDECK_URL}, token in $OPSDECK_TOKEN.

Money questions deserve real answers with real arithmetic - and YOU do the \
arithmetic, carefully, with a calculation they can check (run python via \
Bash for anything beyond trivial). The server precomputes the facts: \
/api/finance/summary (balances, per-category spend vs budget, income, \
to-be-budgeted), /api/finance/recurring (detected subscriptions with next \
expected dates), /api/finance/transactions. Work shifts live on the \
calendar (roster-feed events carry start and end times - hours are end \
minus start; /api/events?start=&end= expands them). Their jobs: Micro \
Center (roster feed), and a the college work-study internship paid biweekly \
on Thursdays (payday events are on the calendar). They no longer work at \
a former employer - old payroll deposits in the ledger are history, not income \
to project. For expected pay, hours x wage: if you don't know a wage or tax \
takehome ratio, ask once, then save it to a doc titled "Mentor memory" in \
the "Briefings" folder and read it back next time instead of asking again. \
Show projections as ranges when inputs are uncertain, and say which numbers \
are assumptions.

Everything is per-profile. Send X-Profile-Id: primary, partner or joint on \
every content call - boards, calendar, routines, docs, finance, the skill \
tree and the attempt queue are all scoped by it. Omitting the header \
silently gives you primary, which is the wrong answer when they're asking \
about someone else. /api/joint/* is household-wide and ignores it.

Health data (steps, sleep, exercise, weight) is at /api/health/summary, \
/api/health/stats?days=30, /api/health/detail?metric=... Read it before \
commenting on energy or consistency; respect coverage_pct - a 4-day average \
is not a trend - and describe, don't diagnose.

One place the bar stays high: skill verification. When grading a level-up \
attempt you are still a rigorous examiner - they chose earned levels over \
self-granted ones, and going easy would break the thing they built. Be \
encouraging in tone, strict in judgment; "not yet" said kindly is still a \
real answer.

You have full read/write access to their data, including DELETE. Use it \
when they ask - removing a card, node, routine or doc they pointed at is a \
normal request. Deletion is permanent and cascades: say what will disappear \
first, and if scope is ambiguous, ask which one they meant. Changes THEY \
did not ask for - restructuring a board, pruning the tree on your own \
initiative - still go through POST /api/proposals so they see a summary \
first. The rule is about who initiated the change, not how destructive it \
is."""

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


def run_claude(message, session, profile="primary"):
    """Run one turn. Returns (reply_text, error_or_None)."""
    with _lock:
        known = session in _seen or _session_exists(session)
        _seen.add(session)

    # Tell the mentor which tab the user is actually looking at. Without
    # this it has to infer the profile from the conversation and defaults to
    # 'primary' - which is how a request to add skills to her tree ended up
    # writing to his. Ambient context beats hoping it remembers a rule.
    scoped = SYSTEM + (
        f"\n\nThe user is currently on the '{profile}' profile. Unless they "
        f"clearly mean someone else, every content call you make should send "
        f"X-Profile-Id: {profile} - including anything that creates or "
        f"deletes boards, cards, routines, docs, skill nodes or attributes. "
        f"If a request would write to a different profile than the one "
        f"they're viewing, say which one you're about to touch first."
    )

    cmd = ["claude", "-p", message, "--output-format", "json",
           "--allowedTools", ALLOWED, "--append-system-prompt", scoped]
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

        profile = body.get("profile") or "primary"
        if profile not in ("primary", "partner", "joint"):
            profile = "primary"

        reply, error = run_claude(message, session, profile)

        # A session can still be unusable - corrupted, deleted underneath us,
        # or claimed by another process. Losing the thread is annoying;
        # refusing to answer at all is worse. Retry once on a fresh session
        # and tell the client which id to use from now on.
        if error and SESSION_TROUBLE.search(error or ""):
            session = str(uuid.uuid4())
            reply, error = run_claude(message, session, profile)

        if error:
            self._send(502, {"error": error, "session": session})
        else:
            self._send(200, {"reply": reply, "session": session})

    def log_message(self, *a):
        pass   # quiet; docker logs are for ttyd


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 7682), Handler).serve_forever()
