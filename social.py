"""
The Joint tab - everything two people share.

All of this hangs off one append-only table, activity_events, exactly like
the personal XP system hangs off skill_levels: relationship XP, companion
growth and milestone celebrations are queries over that log, not stored
counters that can drift. Everything else here is straightforward CRUD on
per-feature tables (mailbox, wall, date ideas, countdowns, ...).

Mounted at /api/joint. It shares the app's token auth but is deliberately a
separate blueprint from the profile-scoped api.py: joint content is not
"the active profile's" content, it belongs to the household.
"""
import json
import math
import os
import uuid
from datetime import timedelta
from functools import wraps

from flask import Blueprint, jsonify, request

from db import connect
from recurrence import today_local, now_local, parse_dt, fmt_dt, expand_event

social = Blueprint("social", __name__, url_prefix="/api/joint")

API_TOKEN = os.environ.get("OPSDECK_TOKEN", "")
INTERACT_COOLDOWN_MIN = 60          # companion pet/water cooldown
COMPANION_STAGE_XP = 150            # relationship-XP per growth stage
COMPANION_MAX_STAGE = 6

PING_KINDS = {
    "thinking_of_you": "is thinking of you",
    "miss_you": "misses you",
    "proud_of_you": "is proud of you",
    "you_got_this": "says you got this",
}


def require_token(fn):
    @wraps(fn)
    def wrapper(*a, **k):
        if not API_TOKEN:
            return jsonify({"error": "OPSDECK_TOKEN is not set on the server"}), 500
        supplied = request.headers.get("X-API-Token") or request.args.get("token")
        if supplied != API_TOKEN:
            return jsonify({"error": "bad or missing API token"}), 401
        return fn(*a, **k)
    return wrapper


def body():
    return request.get_json(force=True, silent=True) or {}


def one(conn, sql, params=()):
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


def many(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def notify(conn, profile_id, source_type, title, notif_body="", link=None):
    """Drop an in-app notification. Caller commits.

    Also fans out to Web Push (fire-and-forget thread), so anything the app
    already notifies about reaches phones with the tab closed - no feature
    needs its own push code."""
    conn.execute(
        "INSERT INTO notifications (profile_id,source_type,title,body,link) VALUES (?,?,?,?,?)",
        (profile_id, source_type, title, notif_body, link),
    )
    try:
        import push
        push.send_async(profile_id, title, notif_body, link=link, tag=source_type)
    except Exception:
        pass          # push is best-effort; the in-app row is the record


# ================================================================ activity

def _xp_config(conn):
    row = one(conn, "SELECT weights FROM relationship_xp_config WHERE id=1")
    return json.loads(row["weights"]) if row else {}


def relationship_level(xp):
    """A gentle curve: level N needs ~50*N^2 XP, so early levels come fast."""
    return int((xp / 50.0) ** 0.5) if xp > 0 else 0


def log_activity(conn, profile_id, source_type, source_id=None, weight=None):
    """
    Record one relationship-relevant action and cascade its side effects:
    companion growth and milestone celebrations. Does NOT commit - the
    caller owns the transaction. Safe to call from anywhere (api.py hooks
    this into routine completion and joint-card completion).
    """
    cfg = _xp_config(conn)
    w = weight if weight is not None else cfg.get(source_type, 1.0)
    conn.execute(
        "INSERT INTO activity_events (profile_id,source_type,source_id,weight) VALUES (?,?,?,?)",
        (profile_id, source_type, source_id, w),
    )
    _sync_companion(conn)
    _check_milestones(conn)


def _total_xp(conn):
    row = one(conn, "SELECT COALESCE(SUM(weight),0) AS s FROM activity_events")
    return round(row["s"], 2) if row else 0.0


def _sync_companion(conn):
    xp = _total_xp(conn)
    stage = min(int(xp // COMPANION_STAGE_XP), COMPANION_MAX_STAGE)
    conn.execute("UPDATE companion SET xp=?, growth_stage=? WHERE id=1", (xp, stage))


def _best_streak(conn):
    """Longest current routine streak across everyone - milestone input."""
    rows = many(conn, "SELECT id FROM routines WHERE active=1")
    best = 0
    today = today_local()
    for r in rows:
        dates = {x["local_date"] for x in conn.execute(
            "SELECT local_date FROM routine_completions WHERE routine_id=? "
            "ORDER BY local_date DESC LIMIT 400", (r["id"],))}
        if not dates:
            continue
        cur = today
        if cur.isoformat() not in dates:
            cur = cur - timedelta(days=1)
        streak = 0
        while cur.isoformat() in dates:
            streak += 1
            cur = cur - timedelta(days=1)
        best = max(best, streak)
    return best


def _check_milestones(conn):
    """Fire any milestone whose threshold has just been crossed."""
    xp = _total_xp(conn)
    streak = _best_streak(conn)
    for m in many(conn, "SELECT * FROM milestones WHERE celebrated=0"):
        crossed = ((m["type"] == "relationship_xp" and xp >= m["threshold"]) or
                   (m["type"] == "streak" and streak >= m["threshold"]))
        if crossed:
            conn.execute("UPDATE milestones SET celebrated=1 WHERE id=?", (m["id"],))
            for pid in ("primary", "partner"):
                notify(conn, pid, "milestone", f"Milestone: {m['label']}",
                       "You crossed a milestone together.", link="#joint")


# ================================================================ 4.1 calendar

@social.route("/calendar", methods=["GET"])
@require_token
def joint_calendar():
    """Merged, read-only feed of both individuals' + joint events, tagged."""
    if "start" not in request.args:
        return jsonify({"error": "start (and end) required"}), 400
    win_start = parse_dt(request.args["start"])
    win_end = parse_dt(request.args.get("end", request.args["start"])) + timedelta(days=1)

    conn = connect()
    events = many(conn, "SELECT * FROM events WHERE profile_id IN ('primary','partner','joint')")
    overrides = many(conn, "SELECT * FROM event_overrides")
    conn.close()

    by_event = {}
    for ov in overrides:
        by_event.setdefault(ov["event_id"], {})[ov["occurrence"]] = ov

    out = []
    for ev in events:
        for occ in expand_event(ev, win_start, win_end, by_event.get(ev["id"])):
            occ["owner_profile_id"] = ev["profile_id"]
            out.append(occ)
    out.sort(key=lambda o: o["start_at"])
    return jsonify(out)


# ================================================================ 4.3 relationship XP

@social.route("/relationship-xp", methods=["GET"])
@require_token
def get_relationship_xp():
    conn = connect()
    xp = _total_xp(conn)
    level = relationship_level(xp)
    next_level_xp = 50 * (level + 1) ** 2
    # weekly history for a little sparkline
    history = many(conn, """
        SELECT substr(created_at,1,10) AS day, ROUND(SUM(weight),2) AS xp
        FROM activity_events GROUP BY day ORDER BY day DESC LIMIT 30""")
    by_source = many(conn, """
        SELECT source_type, ROUND(SUM(weight),2) AS xp, COUNT(*) AS n
        FROM activity_events GROUP BY source_type ORDER BY xp DESC""")
    conn.close()
    return jsonify({
        "xp": xp, "level": level,
        "into_level": round(xp - 50 * level ** 2, 2),
        "level_span": next_level_xp - 50 * level ** 2,
        "by_source": by_source,
        "history": list(reversed(history)),
    })


@social.route("/relationship-xp/config", methods=["GET", "PATCH"])
@require_token
def relationship_xp_config():
    conn = connect()
    if request.method == "PATCH":
        cfg = _xp_config(conn)
        cfg.update(body())
        conn.execute("UPDATE relationship_xp_config SET weights=? WHERE id=1", (json.dumps(cfg),))
        conn.commit()
    out = _xp_config(conn)
    conn.close()
    return jsonify(out)


# ================================================================ 4.4 mailbox

def _deliver_due_mail(conn):
    """Flip any past-due messages to delivered and notify the recipient(s)."""
    now = now_local()
    due = many(conn, "SELECT * FROM mailbox_messages WHERE delivered=0 AND deliver_at<=?",
               (fmt_dt(now),))
    for m in due:
        conn.execute("UPDATE mailbox_messages SET delivered=1 WHERE id=?", (m["id"],))
        recipients = [m["to_profile_id"]] if m["to_profile_id"] else ["primary", "partner"]
        for pid in recipients:
            notify(conn, pid, "mailbox", "You have a new message",
                   m["body"][:120], link="#joint")
    if due:
        conn.commit()
    return len(due)


@social.route("/mailbox", methods=["GET", "POST"])
@require_token
def mailbox():
    conn = connect()
    if request.method == "POST":
        d = body()
        if not (d.get("body") or "").strip():
            conn.close()
            return jsonify({"error": "body required"}), 400
        cur = conn.execute(
            "INSERT INTO mailbox_messages (from_profile_id,to_profile_id,body,deliver_at) "
            "VALUES (?,?,?,?)",
            (d.get("from_profile_id", "primary"), d.get("to_profile_id"),
             d["body"], d.get("deliver_at") or fmt_dt(now_local())),
        )
        conn.commit()
        out = one(conn, "SELECT * FROM mailbox_messages WHERE id=?", (cur.lastrowid,))
        conn.close()
        return jsonify(out), 201

    _deliver_due_mail(conn)
    status = request.args.get("status")
    sql = "SELECT * FROM mailbox_messages"
    params = ()
    if status == "pending":
        sql += " WHERE delivered=0"
    elif status == "delivered":
        sql += " WHERE delivered=1"
    sql += " ORDER BY deliver_at DESC LIMIT 100"
    out = many(conn, sql, params)
    conn.close()
    return jsonify(out)


# ================================================================ 4.5 wall

@social.route("/wall", methods=["GET", "POST"])
@require_token
def wall():
    conn = connect()
    if request.method == "POST":
        d = body()
        cur = conn.execute(
            "INSERT INTO wall_posts (profile_id,type,content,caption) VALUES (?,?,?,?)",
            (d.get("profile_id", "primary"), d.get("type", "text"),
             d.get("content", ""), d.get("caption")),
        )
        pid = d.get("profile_id", "primary")
        for other in ("primary", "partner"):
            if other != pid:
                notify(conn, other, "wall", "New post on the wall",
                       (d.get("caption") or d.get("content") or "")[:100], link="#joint")
        conn.commit()
        out = one(conn, "SELECT * FROM wall_posts WHERE id=?", (cur.lastrowid,))
        conn.close()
        out["reactions"] = []
        return jsonify(out), 201

    before = request.args.get("before")
    sql = "SELECT * FROM wall_posts"
    params = ()
    if before:
        sql += " WHERE created_at < ?"
        params = (before,)
    sql += " ORDER BY created_at DESC LIMIT 30"
    posts = many(conn, sql, params)
    for p in posts:
        p["reactions"] = many(
            conn,
            "SELECT emoji, COUNT(*) AS n, GROUP_CONCAT(profile_id) AS who "
            "FROM wall_reactions WHERE post_id=? GROUP BY emoji",
            (p["id"],),
        )
    conn.close()
    return jsonify(posts)


@social.route("/wall/<int:pid>/react", methods=["POST"])
@require_token
def wall_react(pid):
    d = body()
    profile_id = d.get("profile_id", "primary")
    emoji = d.get("emoji", "❤️")
    conn = connect()
    existing = one(conn, "SELECT 1 FROM wall_reactions WHERE post_id=? AND profile_id=? AND emoji=?",
                   (pid, profile_id, emoji))
    if existing:
        conn.execute("DELETE FROM wall_reactions WHERE post_id=? AND profile_id=? AND emoji=?",
                     (pid, profile_id, emoji))
        toggled = "off"
    else:
        conn.execute("INSERT INTO wall_reactions (post_id,profile_id,emoji) VALUES (?,?,?)",
                     (pid, profile_id, emoji))
        toggled = "on"
    conn.commit()
    conn.close()
    return jsonify({"post_id": pid, "emoji": emoji, "state": toggled})


# ================================================================ 4.6 date ideas

@social.route("/date-ideas", methods=["GET", "POST"])
@require_token
def date_ideas():
    conn = connect()
    if request.method == "POST":
        d = body()
        cur = conn.execute(
            "INSERT INTO date_ideas (created_by,title,description,tags,status) VALUES (?,?,?,?,?)",
            (d.get("created_by", "primary"), d.get("title", "Untitled"),
             d.get("description"), json.dumps(d.get("tags", {})), d.get("status", "idea")),
        )
        conn.commit()
        out = one(conn, "SELECT * FROM date_ideas WHERE id=?", (cur.lastrowid,))
        conn.close()
        out["tags"] = json.loads(out["tags"])
        return jsonify(out), 201

    status = request.args.get("status")
    tag = request.args.get("tag")
    rows = many(conn, "SELECT * FROM date_ideas ORDER BY created_at DESC")
    conn.close()
    out = []
    for r in rows:
        r["tags"] = json.loads(r["tags"] or "{}")
        if status and r["status"] != status:
            continue
        if tag and tag not in r["tags"].values():
            continue
        out.append(r)
    return jsonify(out)


@social.route("/date-ideas/random", methods=["POST"])
@require_token
def date_idea_random():
    """Draw a random unplanned idea, optionally filtered by tag value."""
    tag = request.args.get("tag") or body().get("tag")
    conn = connect()
    rows = many(conn, "SELECT * FROM date_ideas WHERE status='idea'")
    conn.close()
    pool = []
    for r in rows:
        r["tags"] = json.loads(r["tags"] or "{}")
        if tag and tag not in r["tags"].values():
            continue
        pool.append(r)
    if not pool:
        return jsonify({"error": "no unplanned ideas match"}), 404
    # No Math.random equivalent needed server-side; pick by a rotating index
    # derived from the current minute so repeated draws vary.
    idx = now_local().minute % len(pool)
    return jsonify(pool[idx])


@social.route("/date-ideas/<int:iid>", methods=["PATCH", "DELETE"])
@require_token
def date_idea_modify(iid):
    conn = connect()
    if request.method == "DELETE":
        conn.execute("DELETE FROM date_ideas WHERE id=?", (iid,))
        conn.commit()
        conn.close()
        return jsonify({"deleted": iid})
    d = body()
    for f in ("title", "description", "status", "planned_date"):
        if f in d:
            conn.execute(f"UPDATE date_ideas SET {f}=? WHERE id=?", (d[f], iid))
    if "tags" in d:
        conn.execute("UPDATE date_ideas SET tags=? WHERE id=?", (json.dumps(d["tags"]), iid))
    conn.commit()
    out = one(conn, "SELECT * FROM date_ideas WHERE id=?", (iid,))
    conn.close()
    out["tags"] = json.loads(out["tags"] or "{}")
    return jsonify(out)


# ================================================================ 4.7 companion

@social.route("/companion", methods=["GET"])
@require_token
def get_companion():
    conn = connect()
    c = one(conn, "SELECT * FROM companion WHERE id=1")
    streak = _best_streak(conn)
    conn.close()
    # Mood derives from recent streak health rather than being stored.
    mood = "thriving" if streak >= 7 else "happy" if streak >= 2 else \
           "content" if streak >= 1 else "sleepy"
    c["mood"] = mood
    c["next_stage_xp"] = (c["growth_stage"] + 1) * COMPANION_STAGE_XP
    return jsonify(c)


@social.route("/companion/interact", methods=["POST"])
@require_token
def companion_interact():
    conn = connect()
    c = one(conn, "SELECT * FROM companion WHERE id=1")
    now = now_local()
    if c["last_interacted_at"]:
        last = parse_dt(c["last_interacted_at"])
        if (now - last) < timedelta(minutes=INTERACT_COOLDOWN_MIN):
            wait = INTERACT_COOLDOWN_MIN - int((now - last).total_seconds() // 60)
            conn.close()
            return jsonify({"error": f"on cooldown, try again in ~{wait} min"}), 429
    conn.execute("UPDATE companion SET last_interacted_at=? WHERE id=1", (fmt_dt(now),))
    log_activity(conn, request.headers.get("X-Profile-Id", "primary"), "interaction",
                 source_id="companion")
    conn.commit()
    out = one(conn, "SELECT * FROM companion WHERE id=1")
    conn.close()
    return jsonify(out)


# ================================================================ 4.8 countdowns

@social.route("/countdowns", methods=["GET", "POST"])
@require_token
def countdowns():
    conn = connect()
    if request.method == "POST":
        d = body()
        cur = conn.execute(
            "INSERT INTO countdowns (label,target_date,recurring) VALUES (?,?,?)",
            (d.get("label", "Countdown"), d["target_date"], int(d.get("recurring", 0))),
        )
        conn.commit()
        out = one(conn, "SELECT * FROM countdowns WHERE id=?", (cur.lastrowid,))
        conn.close()
        return jsonify(out), 201
    rows = many(conn, "SELECT * FROM countdowns ORDER BY target_date")
    conn.close()
    today = today_local()
    for r in rows:
        r["days_until"] = (parse_dt(r["target_date"]).date() - today).days
    return jsonify(rows)


@social.route("/countdowns/next", methods=["GET"])
@require_token
def countdown_next():
    conn = connect()
    rows = many(conn, "SELECT * FROM countdowns")
    conn.close()
    today = today_local()
    upcoming = []
    for r in rows:
        d = (parse_dt(r["target_date"]).date() - today).days
        if r["recurring"] and d < 0:
            # roll a recurring date forward to its next anniversary
            tgt = parse_dt(r["target_date"]).date()
            while tgt < today:
                try:
                    tgt = tgt.replace(year=tgt.year + 1)
                except ValueError:
                    tgt = tgt + timedelta(days=365)
            d = (tgt - today).days
        if d >= 0:
            r["days_until"] = d
            upcoming.append(r)
    upcoming.sort(key=lambda r: r["days_until"])
    return jsonify(upcoming[0] if upcoming else None)


@social.route("/countdowns/<int:cid>", methods=["PATCH", "DELETE"])
@require_token
def countdown_modify(cid):
    conn = connect()
    if request.method == "DELETE":
        conn.execute("DELETE FROM countdowns WHERE id=?", (cid,))
        conn.commit()
        conn.close()
        return jsonify({"deleted": cid})
    d = body()
    for f in ("label", "target_date", "recurring"):
        if f in d:
            conn.execute(f"UPDATE countdowns SET {f}=? WHERE id=?", (d[f], cid))
    conn.commit()
    out = one(conn, "SELECT * FROM countdowns WHERE id=?", (cid,))
    conn.close()
    return jsonify(out)


# ================================================================ 4.9 song of the day

@social.route("/song-of-day", methods=["GET", "POST"])
@require_token
def song_of_day():
    conn = connect()
    if request.method == "POST":
        d = body()
        cur = conn.execute(
            "INSERT INTO song_of_day (profile_id,track_title,track_url,note,local_date) "
            "VALUES (?,?,?,?,?)",
            (d.get("profile_id", "primary"), d.get("track_title", ""),
             d.get("track_url"), d.get("note"), d.get("date") or today_local().isoformat()),
        )
        conn.commit()
        out = one(conn, "SELECT * FROM song_of_day WHERE id=?", (cur.lastrowid,))
        conn.close()
        return jsonify(out), 201
    days = int(request.args.get("range", 30))
    start = (today_local() - timedelta(days=days)).isoformat()
    rows = many(conn, "SELECT * FROM song_of_day WHERE local_date>=? ORDER BY local_date DESC, id DESC",
                (start,))
    conn.close()
    return jsonify(rows)


# ================================================================ 4.10 daily question

def _prompt_for_today(conn):
    """Return today's prompt row, creating it from the seed bank if needed."""
    today = today_local().isoformat()
    row = one(conn, "SELECT * FROM daily_prompts WHERE local_date=?", (today,))
    if row:
        return row
    from db import SEED_DAILY_PROMPTS
    count = conn.execute("SELECT COUNT(*) FROM daily_prompts").fetchone()[0]
    text = SEED_DAILY_PROMPTS[count % len(SEED_DAILY_PROMPTS)]
    cur = conn.execute("INSERT INTO daily_prompts (prompt_text,local_date) VALUES (?,?)",
                       (text, today))
    conn.commit()
    return one(conn, "SELECT * FROM daily_prompts WHERE id=?", (cur.lastrowid,))


@social.route("/daily-prompt/today", methods=["GET"])
@require_token
def daily_prompt_today():
    viewer = request.headers.get("X-Profile-Id", "primary")
    conn = connect()
    prompt = _prompt_for_today(conn)
    answers = many(conn, "SELECT profile_id, answer, answered_at FROM prompt_answers WHERE prompt_id=?",
                   (prompt["id"],))
    conn.close()
    answered_by = {a["profile_id"] for a in answers}
    both_in = {"primary", "partner"}.issubset(answered_by)
    # Answers stay hidden until both have answered - the two-player reveal.
    return jsonify({
        "id": prompt["id"], "prompt_text": prompt["prompt_text"], "date": prompt["local_date"],
        "you_answered": viewer in answered_by,
        "both_answered": both_in,
        "answers": answers if both_in else [
            {"profile_id": a["profile_id"], "answer": None} for a in answers],
    })


@social.route("/daily-prompt/<int:pid>/answer", methods=["POST"])
@require_token
def daily_prompt_answer(pid):
    d = body()
    profile_id = d.get("profile_id") or request.headers.get("X-Profile-Id", "primary")
    ans = (d.get("answer") or "").strip()
    if not ans:
        return jsonify({"error": "answer required"}), 400
    conn = connect()
    conn.execute(
        "INSERT INTO prompt_answers (prompt_id,profile_id,answer) VALUES (?,?,?) "
        "ON CONFLICT(prompt_id,profile_id) DO UPDATE SET answer=excluded.answer, "
        "answered_at=datetime('now')",
        (pid, profile_id, ans),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@social.route("/daily-prompt/history", methods=["GET"])
@require_token
def daily_prompt_history():
    conn = connect()
    prompts = many(conn, "SELECT * FROM daily_prompts ORDER BY local_date DESC LIMIT 60")
    for p in prompts:
        p["answers"] = many(conn, "SELECT profile_id, answer FROM prompt_answers WHERE prompt_id=?",
                            (p["id"],))
    conn.close()
    return jsonify(prompts)


# ================================================================ 4.11 ping

@social.route("/ping", methods=["POST"])
@require_token
def ping():
    d = body()
    to = d.get("to_profile_id", "partner")
    kind = d.get("kind", "thinking_of_you")
    if kind not in PING_KINDS:
        return jsonify({"error": "unknown ping kind"}), 400
    frm = d.get("from_profile_id") or request.headers.get("X-Profile-Id", "primary")
    conn = connect()
    sender = one(conn, "SELECT display_name FROM profiles WHERE id=?", (frm,))
    name = sender["display_name"] if sender else "Someone"
    notify(conn, to, "ping", f"{name} {PING_KINDS[kind]}", link="#joint")
    log_activity(conn, frm, "ping", source_id=kind)
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "kind": kind})


# ================================================================ 4.12 flashback

@social.route("/flashback", methods=["GET"])
@require_token
def flashback():
    """Old content from 1/3/6/12 months ago that lands on today's date."""
    today = today_local()
    targets = []
    for months in (1, 3, 6, 12):
        m = today.month - months
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        try:
            targets.append(today.replace(year=y, month=m))
        except ValueError:
            pass
    dates = {t.isoformat() for t in targets}
    like = [f"{d}%" for d in dates]

    conn = connect()
    out = {"wall_posts": [], "messages": [], "date_ideas": []}
    for d in dates:
        out["wall_posts"] += many(conn, "SELECT * FROM wall_posts WHERE substr(created_at,1,10)=?", (d,))
        out["messages"] += many(conn,
            "SELECT * FROM mailbox_messages WHERE delivered=1 AND substr(deliver_at,1,10)=?", (d,))
        out["date_ideas"] += many(conn,
            "SELECT * FROM date_ideas WHERE status='done' AND substr(planned_date,1,10)=?", (d,))
    conn.close()
    out["has_any"] = any(out[k] for k in ("wall_posts", "messages", "date_ideas"))
    return jsonify(out)


# ================================================================ 4.13 milestones

@social.route("/milestones/upcoming", methods=["GET"])
@require_token
def milestones_upcoming():
    conn = connect()
    xp = _total_xp(conn)
    streak = _best_streak(conn)
    rows = many(conn, "SELECT * FROM milestones WHERE celebrated=0 ORDER BY threshold")
    conn.close()
    for m in rows:
        cur = xp if m["type"] == "relationship_xp" else streak
        m["current"] = round(cur, 2)
        m["remaining"] = round(max(0, m["threshold"] - cur), 2)
    return jsonify(rows)


@social.route("/milestones/recent", methods=["GET"])
@require_token
def milestones_recent():
    conn = connect()
    rows = many(conn, "SELECT * FROM milestones WHERE celebrated=1 ORDER BY threshold DESC")
    conn.close()
    return jsonify(rows)


# ================================================================ 4.14 bucket list

@social.route("/bucket-list", methods=["GET", "POST"])
@require_token
def bucket_list():
    conn = connect()
    if request.method == "POST":
        d = body()
        cur = conn.execute(
            "INSERT INTO bucket_list_items (title,category,status) VALUES (?,?,?)",
            (d.get("title", "Untitled"), d.get("category"), d.get("status", "someday")),
        )
        conn.commit()
        out = one(conn, "SELECT * FROM bucket_list_items WHERE id=?", (cur.lastrowid,))
        conn.close()
        return jsonify(out), 201
    rows = many(conn, "SELECT * FROM bucket_list_items ORDER BY "
                "CASE status WHEN 'planned' THEN 0 WHEN 'someday' THEN 1 ELSE 2 END, created_at DESC")
    conn.close()
    return jsonify(rows)


@social.route("/bucket-list/<int:iid>", methods=["PATCH", "DELETE"])
@require_token
def bucket_item_modify(iid):
    conn = connect()
    if request.method == "DELETE":
        conn.execute("DELETE FROM bucket_list_items WHERE id=?", (iid,))
        conn.commit()
        conn.close()
        return jsonify({"deleted": iid})
    d = body()
    for f in ("title", "category", "status"):
        if f in d:
            conn.execute(f"UPDATE bucket_list_items SET {f}=? WHERE id=?", (d[f], iid))
    # Completing an item auto-posts a little celebration to the wall.
    if d.get("status") == "done":
        conn.execute("UPDATE bucket_list_items SET completed_at=datetime('now') WHERE id=?", (iid,))
        item = one(conn, "SELECT title FROM bucket_list_items WHERE id=?", (iid,))
        conn.execute(
            "INSERT INTO wall_posts (profile_id,type,content,caption) VALUES ('joint','text',?,?)",
            (f"✅ Did it: {item['title']}", "Bucket list win"))
    conn.commit()
    out = one(conn, "SELECT * FROM bucket_list_items WHERE id=?", (iid,))
    conn.close()
    return jsonify(out)


# ================================================================ home summary

@social.route("/home", methods=["GET"])
@require_token
def joint_home():
    """One call for the Joint tab landing view."""
    conn = connect()
    _deliver_due_mail(conn)
    xp = _total_xp(conn)
    level = relationship_level(xp)
    companion = one(conn, "SELECT * FROM companion WHERE id=1")
    streak = _best_streak(conn)
    upcoming_mile = one(conn, "SELECT * FROM milestones WHERE celebrated=0 ORDER BY threshold LIMIT 1")
    countdowns_rows = many(conn, "SELECT * FROM countdowns")
    today = today_local()
    nxt = None
    for r in countdowns_rows:
        dd = (parse_dt(r["target_date"]).date() - today).days
        if dd >= 0 and (nxt is None or dd < nxt["days_until"]):
            nxt = {**r, "days_until": dd}
    latest_songs = many(conn, "SELECT * FROM song_of_day ORDER BY local_date DESC, id DESC LIMIT 2")
    conn.close()
    return jsonify({
        "relationship": {"xp": xp, "level": level},
        "companion": {**companion, "mood": "thriving" if streak >= 7 else "happy"},
        "next_countdown": nxt,
        "next_milestone": upcoming_mile,
        "recent_songs": latest_songs,
    })
