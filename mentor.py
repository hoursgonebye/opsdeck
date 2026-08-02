"""
The mentor: verification of skill level-ups, and applying AI proposals.

Two modes, both using the same data:

  Queue mode (always on). A level-up attempt is a real row moving through
  states, and an external agent (Claude Code, a cron script, you with curl)
  reads pending attempts, posts questions, then posts a verdict. Nothing is
  hidden - the whole handshake is in the API.

  Direct mode (optional). If ANTHROPIC_API_KEY is set, the server calls the
  Anthropic API itself so verification works without a second tool open.
  Direct mode is a convenience layer on top of the queue, not a bypass: it
  writes the same rows, so you can start an attempt in one mode and finish
  it in the other.

The mentor's job is to be a challenger, not a rubber stamp. Difficulty
scales with tree depth AND existing attribute strength, so verification
gets harder as you get better rather than flattening out.
"""
import json
import os
import urllib.error
import urllib.request

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("OPSDECK_MENTOR_MODEL", "claude-sonnet-4-6")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

DIFFICULTY_GUIDE = {
    1: "foundational. One or two concrete questions. Confirm they actually "
       "understand the basics rather than having read about them.",
    2: "working knowledge. Ask them to explain a mechanism, not recite a "
       "definition. At least one question should require an example.",
    3: "practitioner. Require specifics from real work: a command they ran, "
       "output they interpreted, a decision they made and why.",
    4: "advanced. Probe edge cases and failure modes. Ask what breaks, what "
       "they'd check first, what a naive approach gets wrong.",
    5: "expert. Demand depth a textbook answer cannot fake - tradeoffs, "
       "things they've personally debugged, where the common advice is wrong.",
}

MENTOR_SYSTEM = """You are the mentor in a cybersecurity student's skill-tracking app.

You are a strict examiner, not an assistant and not a cheerleader. This \
person is trying to become genuinely excellent, and they have explicitly \
asked you to hold the bar and gatekeep. They chose this. Every level you \
grant is a claim about reality, and your default posture is skepticism: \
the burden of proof is on them, always.

Rules:
- Ask real, specific questions about the actual skill. Never generic quiz \
questions, never anything answerable from a definition page. Prefer \
questions grounded in their own notes - test whether they understand what \
they wrote, not just that they wrote something.
- Scale difficulty to what you are told. A tier-1 node with a beginner is a \
different conversation than a tier-4 node with someone already strong there. \
Advanced levels require demonstrated depth: a real scenario walked through, \
a tradeoff explained, a decision defended.
- Vague, hand-wavy, or terminology-reciting answers are rejected outright. \
Not softened, not "could be stronger" - rejected, with a plain statement of \
exactly what was missing and what would convince you.
- Thin notes are rejected the same as thin answers. If the attached writeup \
is a one-liner, a pasted walkthrough with nothing of their own, or steps \
with no reasoning about *why* anything worked, say so and refuse.
- "No, not yet" is a legitimate final verdict. You are not tuned so that \
attempts eventually succeed. Repeated failure on the same node is an \
acceptable outcome; passing someone who hasn't earned it is not.
- When an answer genuinely clears the bar, grant it plainly and say what \
was strong. Strictness is about the rigor of the check, not manufactured \
friction - do not reject good work to seem tough.
- Be direct and unsparing about the verdict without being demeaning about \
the person. Strict grader, not a bully. Do not flatter, pad, or apologize.

You are not a customer service agent. You are the person who tells them the \
truth about where they actually stand."""


def available():
    return bool(API_KEY)


def _call(system, user, max_tokens=1200):
    """Single Anthropic API call returning concatenated text blocks."""
    payload = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    req = urllib.request.Request(
        ANTHROPIC_URL,
        data=json.dumps(payload).encode(),
        headers={
            "content-type": "application/json",
            "x-api-key": API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as res:
        data = json.loads(res.read().decode())
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def _extract_json(text):
    """Pull a JSON object out of a reply that may be fenced or prefaced."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start:end + 1])


def generate_questions(context, notes=None):
    """
    Ask the mentor for verification questions, grounded in the user's own
    notes when provided. Returns a list of question strings. Raises if
    direct mode is unavailable.
    """
    if not available():
        raise RuntimeError("ANTHROPIC_API_KEY not set - use queue mode instead")

    d = context["difficulty"]
    notes_block = (
        f"\n\nTheir notes for this attempt (mandatory reading - base at least "
        f"one question directly on something they wrote):\n{notes[:6000]}"
        if notes else ""
    )
    prompt = f"""A skill level-up has been requested. Write the verification questions.

Context:
{json.dumps(context, indent=2)}

Difficulty {d}/5 - {DIFFICULTY_GUIDE.get(d, DIFFICULTY_GUIDE[3])}{notes_block}

Write {2 if d <= 2 else 3} questions specific to "{context['node']['title']}". \
They must be answerable only by someone who has actually done this, not \
someone who has read about it. If notes were provided, probe the weakest or \
least-explained part of them - the goal is to test whether they understand \
what they wrote.

Respond with JSON only, no other text:
{{"questions": ["...", "..."]}}"""

    return _extract_json(_call(MENTOR_SYSTEM, prompt))["questions"]


def grade_answers(context, questions, answers, evidence=None):
    """
    Ask the mentor to judge the answers.
    Returns {"granted": bool, "feedback": str, "suggested_nodes": [...]}.
    """
    if not available():
        raise RuntimeError("ANTHROPIC_API_KEY not set - use queue mode instead")

    qa = "\n\n".join(
        f"Q{i+1}: {q}\nA{i+1}: {a or '(no answer given)'}"
        for i, (q, a) in enumerate(zip(questions, answers))
    )
    ev = (f"\n\nTheir notes for this attempt:\n{evidence[:6000]}"
          if evidence else "\n\n(No notes were attached.)")
    d = context["difficulty"]

    prompt = f"""Judge whether this level-up is earned. Default to no; they must convince you.

Context:
{json.dumps(context, indent=2)}

Difficulty {d}/5 - {DIFFICULTY_GUIDE.get(d, DIFFICULTY_GUIDE[3])}

{qa}{ev}

Judge the notes AND the answers together. Grant only if both hold up: the \
notes show real work of their own (specific commands, techniques, reasoning \
about why things worked - not a pasted walkthrough) and the answers \
demonstrate they understand what they wrote. Reject for: vague answers, \
recited terminology, dodged specifics, or notes too thin to defend. When \
rejecting, state exactly what was missing and what would convince you next \
time - "you documented the steps but not why the exploit worked" beats \
"needs more detail".

You may also suggest 1-2 adjacent skill nodes worth adding to their tree \
based on what they demonstrated. Only suggest nodes that follow naturally \
from this one.

Respond with JSON only, no other text:
{{"granted": true/false,
  "feedback": "2-4 sentences, direct, addressed to them",
  "suggested_nodes": [{{"title": "...", "domain": "...", "tier": 2,
                        "rationale": "...",
                        "weights": [{{"attribute_key": "pentest", "weight": 1.0}}]}}]}}"""

    out = _extract_json(_call(MENTOR_SYSTEM, prompt, max_tokens=1600))
    out.setdefault("suggested_nodes", [])
    out.setdefault("feedback", "")
    out["granted"] = bool(out.get("granted"))
    return out


def recommend_room(context):
    """
    Ask the mentor to point the user at their next TryHackMe room(s), based
    on weakest/stalest attributes and what's already been completed.
    Returns {"recommendations": [{room_code, room_title, reason, node_ids}], "summary": str}.
    """
    if not available():
        raise RuntimeError("ANTHROPIC_API_KEY not set - use queue mode instead")

    prompt = f"""Recommend the user's next TryHackMe work.

Their current state:
{json.dumps(context, indent=2)}

Rules:
- Target the weakest or stalest attributes first, using tree node levels to
  find the specific sub-area that's lagging (e.g. "Pentest is fine overall
  but privilege escalation on Windows has no levels").
- Recommend 1-3 specific, real TryHackMe rooms by their exact room code (the
  URL slug) and title. Never recommend anything in completed_rooms.
- If you are not certain a room code is exactly right, say so in the reason
  and give the room title so the user can search for it - do not invent
  plausible-looking codes silently.
- Each recommendation names which existing tree node(s) it should feed
  (node_ids from tree_nodes), so a completion can trigger verification there.
- Be specific about WHY: "you're weak on X relative to Y, this room covers
  exactly that" - not a generic list.

Respond with JSON only, no other text:
{{"summary": "1-2 sentences on where their gaps are",
  "recommendations": [{{"room_code": "...", "room_title": "...",
                        "reason": "...", "node_ids": [1, 2]}}]}}"""

    out = _extract_json(_call(MENTOR_SYSTEM, prompt, max_tokens=1400))
    out.setdefault("recommendations", [])
    out.setdefault("summary", "")
    return out


# ------------------------------------------------- applying proposals

def apply_proposal(conn, proposal):
    """
    Execute an approved proposal's action list.

    Actions are deliberately a small vocabulary rather than arbitrary SQL -
    an approved proposal should not be able to do anything the API couldn't,
    and the user approving a summary shouldn't be authorising a blank cheque.
    """
    actions = json.loads(proposal["actions"] or "[]")
    applied, errors = [], []

    for a in actions:
        op = a.get("op")
        try:
            if op == "move_card":
                conn.execute(
                    "UPDATE cards SET list_id=?, position=? WHERE id=?",
                    (a["list_id"], a.get("position", 0), a["card_id"]),
                )
            elif op == "set_due":
                conn.execute(
                    "UPDATE cards SET due_at=? WHERE id=?", (a.get("due_at"), a["card_id"])
                )
            elif op == "update_card":
                for f in ("title", "description", "completed", "archived"):
                    if f in a:
                        conn.execute(f"UPDATE cards SET {f}=? WHERE id=?", (a[f], a["card_id"]))
            elif op == "create_card":
                pos = conn.execute(
                    "SELECT COALESCE(MAX(position),-1)+1 FROM cards WHERE list_id=?",
                    (a["list_id"],),
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO cards (list_id,title,description,due_at,position) VALUES (?,?,?,?,?)",
                    (a["list_id"], a.get("title", "Untitled"), a.get("description", ""),
                     a.get("due_at"), pos),
                )
            elif op == "create_node":
                owner = a.get("profile_id") or (
                    proposal["profile_id"] if "profile_id" in proposal.keys() else "primary")
                cur = conn.execute(
                    """INSERT INTO skill_nodes (title,description,domain,x,y,tier,max_level,
                                                unlock_attr,unlock_value,profile_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (a["title"], a.get("description", ""), a.get("domain", "general"),
                     a.get("x", 0), a.get("y", 0), a.get("tier", 1), a.get("max_level", 5),
                     a.get("unlock_attr"), a.get("unlock_value"), owner),
                )
                nid = cur.lastrowid
                for w in a.get("weights", []):
                    conn.execute(
                        "INSERT OR REPLACE INTO node_weights (node_id,attribute_key,weight) VALUES (?,?,?)",
                        (nid, w["attribute_key"], w.get("weight", 1.0)),
                    )
                for parent in a.get("parents", []):
                    conn.execute(
                        "INSERT OR IGNORE INTO skill_edges (from_id,to_id) VALUES (?,?)",
                        (parent, nid),
                    )
            elif op == "update_node":
                for f in ("title", "description", "domain", "x", "y", "tier",
                          "max_level", "unlock_attr", "unlock_value"):
                    if f in a:
                        conn.execute(
                            f"UPDATE skill_nodes SET {f}=? WHERE id=?", (a[f], a["node_id"])
                        )
            elif op == "create_edge":
                conn.execute(
                    "INSERT OR IGNORE INTO skill_edges (from_id,to_id) VALUES (?,?)",
                    (a["from_id"], a["to_id"]),
                )
            elif op == "delete_edge":
                conn.execute(
                    "DELETE FROM skill_edges WHERE from_id=? AND to_id=?",
                    (a["from_id"], a["to_id"]),
                )
            elif op == "create_routine":
                conn.execute(
                    "INSERT INTO routines (name,time_group,notes,profile_id) VALUES (?,?,?,?)",
                    (a["name"], a.get("time_group", "anytime"), a.get("notes", ""),
                     a.get("profile_id", proposal["profile_id"] if "profile_id" in proposal.keys() else "primary")),
                )

            # ---- destructive ops -------------------------------------
            # These delete rows for real. They still route through the
            # proposal queue, so nothing here runs until the user has read a
            # plain-language summary and approved it. FK cascades mean
            # deleting a parent takes its children (a board takes its lists
            # and cards) - that is intended, and is why the summary should
            # say so.
            elif op == "delete_card":
                conn.execute("DELETE FROM cards WHERE id=?", (a["card_id"],))
            elif op == "delete_list":
                conn.execute("DELETE FROM lists WHERE id=?", (a["list_id"],))
            elif op == "delete_board":
                conn.execute("DELETE FROM boards WHERE id=?", (a["board_id"],))
            elif op == "delete_node":
                conn.execute("DELETE FROM skill_nodes WHERE id=?", (a["node_id"],))
            elif op == "delete_routine":
                conn.execute("DELETE FROM routines WHERE id=?", (a["routine_id"],))
            elif op == "delete_doc":
                conn.execute("DELETE FROM docs WHERE id=?", (a["doc_id"],))
            elif op == "delete_event":
                conn.execute("DELETE FROM events WHERE id=?", (a["event_id"],))
            elif op == "delete_label":
                conn.execute("DELETE FROM labels WHERE id=?", (a["label_id"],))
            elif op == "delete_checklist_item":
                conn.execute("DELETE FROM checklist_items WHERE id=?", (a["item_id"],))
            elif op == "delete_attribute":
                conn.execute("DELETE FROM attributes WHERE id=?", (a["attribute_id"],))

            else:
                errors.append(f"unknown op: {op}")
                continue
            applied.append(op)
        except Exception as exc:  # keep going; report what failed
            errors.append(f"{op}: {exc}")

    return {"applied": applied, "errors": errors}
