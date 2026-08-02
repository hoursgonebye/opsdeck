"""
Quick capture routing.

The rule here is that writing a thought down must never block on deciding
where it belongs, and must never cost an API call. So capture is instant,
and filing happens in three tiers:

  1. A local heuristic (this file, free, always runs) guesses a destination
     the moment the note is saved - a date-looking phrase suggests an event,
     an imperative with a deadline suggests a card on a specific board.
  2. An agent (Claude Code via the queue) can read pending notes and file
     them properly, using the whole workspace as context.
  3. Direct mode does tier 2 in-process if ANTHROPIC_API_KEY is set.

Tier 1 alone is enough to be useful, which is the point: the expensive
tiers are an upgrade, not a dependency.
"""
import json
import re
from datetime import timedelta

WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

# Words that hint the note is a scheduled thing rather than a task.
EVENT_WORDS = re.compile(
    r"\b(meet|meeting|appointment|appt|call|interview|exam|midterm|final|"
    r"class|lecture|shift|party|dinner|lunch|birthday|due at|starts?|at \d)\b",
    re.I,
)

# Words that hint it belongs in the docs pile, not as a task.
NOTE_WORDS = re.compile(
    r"\b(idea|thought|note to self|remember that|writeup|write-up|reference|"
    r"learned|til\b|snippet|command)\b",
    re.I,
)

DONE_WORDS = re.compile(r"^\s*(did|finished|completed|done)\b", re.I)


def _find_date(text, today):
    """
    Return an ISO date if the note plainly names one, else None. This is
    deliberately conservative - a wrong date is worse than no date, because
    a wrong one silently lands on the calendar.
    """
    t = text.lower()

    if re.search(r"\btoday\b", t):
        return today.isoformat()
    if re.search(r"\btomorrow\b|\btmrw\b", t):
        return (today + timedelta(days=1)).isoformat()
    if re.search(r"\btonight\b", t):
        return today.isoformat()

    m = re.search(r"\bin (\d+) (day|week)s?\b", t)
    if m:
        n = int(m.group(1))
        return (today + timedelta(days=n * (7 if m.group(2) == "week" else 1))).isoformat()

    # "next friday" / "on monday" -> the next such weekday, never in the past.
    m = re.search(r"\b(?:next|on|this)\s+([a-z]+)\b", t)
    if m and m.group(1) in WEEKDAYS:
        delta = (WEEKDAYS[m.group(1)] - today.weekday()) % 7
        return (today + timedelta(days=delta or 7)).isoformat()

    # "aug 14" / "14 aug" / "august 3rd"
    m = re.search(r"\b([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", t)
    if m and m.group(1) in MONTHS:
        return _ymd(today, MONTHS[m.group(1)], int(m.group(2)))
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3,9})\b", t)
    if m and m.group(2) in MONTHS:
        return _ymd(today, MONTHS[m.group(2)], int(m.group(1)))

    # explicit 2026-08-14
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t)
    if m:
        return m.group(0)

    return None


def _ymd(today, month, day):
    """Month/day with no year: assume the next occurrence, not the past."""
    if not 1 <= day <= 31:
        return None
    year = today.year
    if (month, day) < (today.month, today.day):
        year += 1
    try:
        return today.replace(year=year, month=month, day=day).isoformat()
    except ValueError:
        return None


def suggest(conn, text, today, profile_id="primary"):
    """
    Guess where a note belongs. Returns a dict describing the suggestion;
    never raises, never calls out to a network. Board matching is confined
    to the active profile so a note captured on one tab never routes to
    another profile's board.
    """
    body = (text or "").strip()
    if not body:
        return {"kind": "none", "reason": "empty"}

    date = _find_date(body, today)
    first_line = body.splitlines()[0][:120]

    # Score the destination.
    if DONE_WORDS.search(body):
        kind, reason = "done", "reads as something already finished"
    elif EVENT_WORDS.search(body) and date:
        kind, reason = "event", "names a time and a date"
    elif NOTE_WORDS.search(body) and not date:
        kind, reason = "doc", "reads as a note rather than a task"
    else:
        kind, reason = "card", "reads as a task"

    out = {"kind": kind, "reason": reason, "title": first_line, "due": date,
           "profile_id": profile_id}

    if kind == "card":
        out["board"] = _guess_board(conn, body, profile_id)
    return out


# Board routing: match the note against each board's own vocabulary (its
# title plus the titles of cards already on it) instead of a hardcoded
# keyword table, so it keeps working when boards are renamed or added.
STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "at", "by",
    "with", "is", "it", "this", "that", "my", "me", "i", "do", "get", "need",
    "new", "up", "out", "from", "about", "into", "over", "then", "than",
}


def _tokens(s):
    return {w for w in re.findall(r"[a-z0-9]{3,}", (s or "").lower()) if w not in STOPWORDS}


def _guess_board(conn, text, profile_id="primary"):
    """Return {"board_id","list_id","board_title","list_title"} or None."""
    words = _tokens(text)
    if not words:
        return None

    best, best_score = None, 0
    boards = conn.execute(
        "SELECT id, title FROM boards WHERE archived=0 AND profile_id=? ORDER BY position, id",
        (profile_id,),
    ).fetchall()

    for b in boards:
        vocab = _tokens(b["title"])
        for c in conn.execute(
            "SELECT c.title FROM cards c JOIN lists l ON l.id=c.list_id "
            "WHERE l.board_id=? AND c.archived=0 LIMIT 60",
            (b["id"],),
        ):
            vocab |= _tokens(c["title"])

        # Board title words count double - a note saying "homelab" should go
        # to HomeLab even if no card there says it yet.
        score = len(words & _tokens(b["title"])) * 2 + len(words & vocab)
        if score > best_score:
            best, best_score = b, score

    if not best or best_score == 0:
        # No signal: fall back to the first board rather than guessing wildly.
        best = boards[0] if boards else None
        if not best:
            return None

    target = conn.execute(
        "SELECT id, title FROM lists WHERE board_id=? AND archived=0 "
        "ORDER BY position, id LIMIT 1",
        (best["id"],),
    ).fetchone()
    if not target:
        return None

    return {
        "board_id": best["id"], "board_title": best["title"],
        "list_id": target["id"], "list_title": target["title"],
        "confident": best_score > 0,
    }


def file_note(conn, note, plan, today):
    """
    Apply a filing plan to a note. `plan` is the suggestion dict (possibly
    edited by the user or replaced by an agent). Returns a human string
    describing what was created.
    """
    kind = plan.get("kind")
    title = (plan.get("title") or note["body"]).strip()[:200]
    body = note["body"]
    # The note carries the profile it was captured under; everything it
    # spawns belongs to that same profile.
    pid = note["profile_id"] if "profile_id" in note.keys() else plan.get("profile_id", "primary")

    if kind == "event":
        start = plan.get("due") or today.isoformat()
        conn.execute(
            "INSERT INTO events (title,description,start_at,all_day,color,profile_id) "
            "VALUES (?,?,?,?,?,?)",
            (title, body, start, 1, plan.get("color", "blue"), pid),
        )
        return f"event on {start}"

    if kind == "doc":
        conn.execute(
            "INSERT INTO docs (title,kind,body,folder,profile_id) VALUES (?,?,?,?,?)",
            (title, "md", body, plan.get("folder", "Quick notes"), pid),
        )
        return "doc in Quick notes"

    if kind == "routine":
        conn.execute(
            "INSERT INTO routines (name,time_group,notes,profile_id) VALUES (?,?,?,?)",
            (title, plan.get("time_group", "anytime"), "", pid),
        )
        return "routine"

    # default: a card
    list_id = plan.get("list_id")
    if not list_id:
        guess = _guess_board(conn, body, pid)
        list_id = guess and guess["list_id"]
    if not list_id:
        raise ValueError("no list to file into - create a board first")

    pos = conn.execute(
        "SELECT COALESCE(MAX(position),-1)+1 FROM cards WHERE list_id=?", (list_id,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO cards (list_id,title,description,due_at,position) VALUES (?,?,?,?,?)",
        (list_id, title, body if body != title else "", plan.get("due"), pos),
    )
    row = conn.execute(
        "SELECT l.title AS list_title, b.title AS board_title FROM lists l "
        "JOIN boards b ON b.id=l.board_id WHERE l.id=?", (list_id,)
    ).fetchone()
    where = f"{row['board_title']} / {row['list_title']}" if row else "a board"
    return f"card on {where}"
