"""
REST API for Ops Deck.

Every route lives under /api and requires the token in an
X-API-Token header (or ?token= for quick curl testing). The browser UI
uses the same endpoints, so anything the UI can do, a script can do -
there is no privileged private API.

Read API.md for the full endpoint reference.
"""
import json
import os
import re
import urllib.parse
import uuid
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file, abort, g
from werkzeug.utils import secure_filename

import calendars
import growth
import health
import mentor
import quicknote
import social
import thm
from db import connect, UPLOAD_DIR
from recurrence import (
    expand_event, parse_dt, fmt_dt, now_local, today_local, describe_rrule,
)

api = Blueprint("api", __name__, url_prefix="/api")

API_TOKEN = os.environ.get("OPSDECK_TOKEN", "")
MAX_UPLOAD_MB = int(os.environ.get("OPSDECK_MAX_UPLOAD_MB", "25"))

# Notes gate: a level-up attempt cannot even begin without a notes doc that
# clears this floor. This is the cheap mechanical check; the mentor still
# judges whether the notes have real substance.
NOTES_MIN_CHARS = int(os.environ.get("OPSDECK_NOTES_MIN_CHARS", "300"))
NOTES_MIN_LINES = 3


def require_token(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not API_TOKEN:
            # No token configured - refuse rather than silently run open.
            return jsonify({"error": "OPSDECK_TOKEN is not set on the server"}), 500
        supplied = request.headers.get("X-API-Token") or request.args.get("token")
        if supplied != API_TOKEN:
            return jsonify({"error": "bad or missing API token"}), 401
        return fn(*args, **kwargs)
    return wrapper


PRIMARY_PROFILE = "primary"
_VALID_PROFILES = None   # cached set of profile ids, refreshed lazily


def _profile_ids(conn):
    global _VALID_PROFILES
    if _VALID_PROFILES is None:
        _VALID_PROFILES = {r["id"] for r in conn.execute("SELECT id FROM profiles")}
    return _VALID_PROFILES


def invalidate_profile_cache():
    global _VALID_PROFILES
    _VALID_PROFILES = None


def resolve_profile():
    """
    The active profile for this request. Chosen by the X-Profile-Id header
    (the SPA sets it to whichever tab is active) and validated against the
    profiles table; anything missing or unknown falls back to 'primary', so
    a legacy client that never sends the header behaves exactly as before.

    Design note: scoping lives in a header rather than an /api/profiles/{id}/
    path segment. It gives identical isolation without registering the whole
    blueprint twice or threading an id kwarg through every handler, and the
    single-page app fully controls the header. See ARCHITECTURE.md.
    """
    supplied = request.headers.get("X-Profile-Id") or request.args.get("profile")
    if not supplied:
        g.profile_id = PRIMARY_PROFILE
        return
    conn = connect()
    try:
        g.profile_id = supplied if supplied in _profile_ids(conn) else PRIMARY_PROFILE
    finally:
        conn.close()


api.before_request(resolve_profile)


def active_profile():
    """The resolved profile id for this request (defaults to primary)."""
    return getattr(g, "profile_id", PRIMARY_PROFILE)


def body():
    return request.get_json(force=True, silent=True) or {}


# The stylesheet only defines these; anything else renders as an unstyled
# blank chip. An agent writing through the API had no way to know that, so
# the server normalises rather than trusting the caller.
PALETTE = ("gray", "blue", "teal", "green", "amber", "red", "purple", "pink")
MAX_TIER = 5


def _safe_color(value, fallback="teal"):
    return value if value in PALETTE else fallback


def _safe_tier(value, fallback=1):
    """Tier drives XP value and verification difficulty, so an out-of-range
    tier silently inflates both. Clamp to the documented 1-5."""
    try:
        return max(1, min(MAX_TIER, int(value)))
    except (TypeError, ValueError):
        return fallback


def one(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def many(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ----------------------------------------------------------- mentor chat
# The browser never talks to the terminal container directly: this proxies
# to the chat bridge over the internal docker network, so the chat panel is
# same-origin and covered by the app's own token auth.

MENTOR_BRIDGE = os.environ.get("OPSDECK_BRIDGE_URL", "http://terminal:7682")


@api.route("/mentor/chat", methods=["POST"])
@require_token
def mentor_chat():
    import urllib.error
    import urllib.request

    d = body()
    message = (d.get("message") or "").strip()
    if not message:
        return jsonify({"error": "empty message"}), 400

    # Forward which profile the user is viewing, so the mentor scopes its own
    # API calls to the same tab instead of defaulting to primary.
    payload = json.dumps({
        "message": message,
        "session": d.get("session"),
        "profile": active_profile(),
    }).encode()
    req = urllib.request.Request(
        MENTOR_BRIDGE + "/chat",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Bridge-Token": os.environ.get("OPSDECK_TOKEN", ""),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=200) as res:
            return jsonify(json.loads(res.read().decode()))
    except urllib.error.HTTPError as e:
        try:
            return jsonify(json.loads(e.read().decode())), e.code
        except Exception:
            return jsonify({"error": f"bridge error {e.code}"}), 502
    except urllib.error.URLError as e:
        return jsonify({
            "error": "Mentor terminal is unreachable. Is the opsdeck-terminal "
                     f"container running? ({e.reason})"
        }), 503


@api.route("/mentor/chat/health", methods=["GET"])
@require_token
def mentor_chat_health():
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(MENTOR_BRIDGE + "/health", timeout=5) as res:
            out = json.loads(res.read().decode())
        out["available"] = True
        return jsonify(out)
    except (urllib.error.URLError, OSError, ValueError):
        return jsonify({"available": False, "logged_in": False})


# ------------------------------------------------------------ quick notes
# Capture is deliberately dumb and instant: store the text, attach a free
# local guess, return. Filing is a separate, later decision - by the user,
# or by an agent reading the pending queue.

@api.route("/notes/quick", methods=["POST"])
@require_token
def create_quick_note():
    d = body()
    text = (d.get("body") or "").strip()
    if not text:
        return jsonify({"error": "empty note"}), 400

    conn = connect()
    pid = active_profile()
    suggestion = quicknote.suggest(conn, text, today_local(), profile_id=pid)
    cur = conn.execute(
        "INSERT INTO quick_notes (body, suggestion, profile_id) VALUES (?,?,?)",
        (text, json.dumps(suggestion), pid),
    )
    nid = cur.lastrowid

    # file_now is the one-tap path - but only when the guess is actually
    # worth trusting. A low-confidence board match means the heuristic found
    # no signal and is falling back to whatever board comes first; filing on
    # that silently puts notes somewhere wrong. Leave it pending instead and
    # let the user (or an agent) place it.
    board = suggestion.get("board") or {}
    trustworthy = suggestion.get("kind") != "card" or board.get("confident")

    if d.get("file_now") and trustworthy:
        note = one(conn, "SELECT * FROM quick_notes WHERE id=?", (nid,))
        try:
            where = quicknote.file_note(conn, note, suggestion, today_local())
            conn.execute(
                "UPDATE quick_notes SET status='filed', filed_as=?, "
                "resolved_at=datetime('now') WHERE id=?",
                (where, nid),
            )
        except ValueError as e:
            conn.commit()
            conn.close()
            return jsonify({"error": str(e)}), 400

    conn.commit()
    out = one(conn, "SELECT * FROM quick_notes WHERE id=?", (nid,))
    conn.close()
    out["suggestion"] = json.loads(out["suggestion"] or "{}")
    return jsonify(out), 201


@api.route("/notes/quick", methods=["GET"])
@require_token
def list_quick_notes():
    status = request.args.get("status", "pending")
    conn = connect()
    sql = "SELECT * FROM quick_notes WHERE profile_id=?"
    params = [active_profile()]
    if status != "all":
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY created_at DESC, id DESC"
    out = many(conn, sql, params)
    conn.close()
    for n in out:
        n["suggestion"] = json.loads(n["suggestion"] or "{}")
    return jsonify(out)


@api.route("/notes/quick/<int:nid>/file", methods=["POST"])
@require_token
def file_quick_note(nid):
    """
    Apply a filing plan. With no body, the stored heuristic suggestion is
    used; an agent (or the user) can post a better plan instead.
    """
    conn = connect()
    note = one(conn, "SELECT * FROM quick_notes WHERE id=?", (nid,))
    if not note:
        conn.close()
        return jsonify({"error": "no such note"}), 404
    if note["status"] != "pending":
        conn.close()
        return jsonify({"error": f"note already {note['status']}"}), 409

    plan = body() or json.loads(note["suggestion"] or "{}")
    try:
        where = quicknote.file_note(conn, note, plan, today_local())
    except ValueError as e:
        conn.close()
        return jsonify({"error": str(e)}), 400

    conn.execute(
        "UPDATE quick_notes SET status='filed', filed_as=?, "
        "resolved_at=datetime('now') WHERE id=?",
        (where, nid),
    )
    conn.commit()
    out = one(conn, "SELECT * FROM quick_notes WHERE id=?", (nid,))
    conn.close()
    out["suggestion"] = json.loads(out["suggestion"] or "{}")
    return jsonify(out)


@api.route("/notes/quick/<int:nid>", methods=["DELETE"])
@require_token
def dismiss_quick_note(nid):
    conn = connect()
    conn.execute(
        "UPDATE quick_notes SET status='dismissed', resolved_at=datetime('now') "
        "WHERE id=? AND status='pending'",
        (nid,),
    )
    conn.commit()
    conn.close()
    return jsonify({"dismissed": nid})


# --------------------------------------------------------------- web push
# Real notifications on phones with the tab closed. The browser subscribes
# via its push service using our VAPID public key; the server stores the
# subscription per profile and social.notify() fans out to it.

@api.route("/push/vapid-key", methods=["GET"])
@require_token
def push_vapid_key():
    import push
    try:
        return jsonify({"key": push.public_key()})
    except Exception as e:
        return jsonify({"error": f"push not available: {e}"}), 503


@api.route("/push/subscribe", methods=["POST"])
@require_token
def push_subscribe():
    import push
    d = body()
    sub = d.get("subscription") or {}
    conn = connect()
    # A bound device names its profile explicitly, so a boot-time refresh
    # while viewing the Us tab still registers under the right person.
    pid = d.get("profile")
    if not pid or pid not in _profile_ids(conn):
        pid = active_profile()
    ok = push.save_subscription(conn, pid, sub, claim=bool(d.get("claim")))
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM push_subscriptions WHERE profile_id=?",
                     (pid,)).fetchone()[0]
    conn.close()
    if not ok:
        return jsonify({"error": "subscription needs endpoint and keys"}), 400
    return jsonify({"subscribed": True, "profile": pid, "devices": n}), 201


@api.route("/push/unsubscribe", methods=["POST"])
@require_token
def push_unsubscribe():
    import push
    endpoint = (body().get("endpoint") or "").strip()
    if not endpoint:
        return jsonify({"error": "endpoint required"}), 400
    conn = connect()
    n = push.drop_subscription(conn, endpoint)
    conn.commit()
    conn.close()
    return jsonify({"removed": n})


@api.route("/push/test", methods=["POST"])
@require_token
def push_test():
    """Prove the whole pipe works, end to end, on demand."""
    import push
    sent = push.send_to_profile(active_profile(), "Ops Deck",
                                "Push notifications are working on this device.",
                                tag="push-test")
    return jsonify({"sent_to_devices": sent})


# ------------------------------------------------------- mentor briefing
# A deterministic per-profile daily digest the chat mentor reads before
# answering, so "what's my situation" costs zero model calls to establish.
# Written nightly by briefing.py's scheduler; these endpoints read it and
# regenerate it on demand.

@api.route("/mentor/briefing", methods=["GET", "POST"])
@require_token
def mentor_briefing():
    import briefing as _briefing
    conn = connect()
    if request.method == "POST":
        title, briefing_body = _briefing.generate(conn, active_profile())
        conn.close()
        return jsonify({"title": title, "body": briefing_body}), 201
    doc = _briefing.latest(conn, active_profile())
    conn.close()
    if not doc:
        return jsonify({"error": "no briefing yet - POST here to generate one"}), 404
    return jsonify(doc)


# --------------------------------------------------------- calendar feeds
# Subscribed read-only .ics feeds. Their events land in the normal events
# table tagged with feed_id, so Today, the month grid and the merged Us view
# pick them up without knowing feeds exist.

def _next_sync_at(feed):
    """
    When the background sweeper will next pick this feed up.

    Returned in UTC to match last_synced_at, which SQLite writes with
    datetime('now'); the browser converts. None means "not on a schedule" -
    auto-sync is off, or the feed is disabled, or it has never synced and
    the next tick will take it regardless.
    """
    if calendars.AUTO_SYNC_MINUTES <= 0 or not feed.get("enabled"):
        return None
    if not feed.get("last_synced_at"):
        return None
    try:
        base = datetime.strptime(feed["last_synced_at"][:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return (base + timedelta(minutes=calendars.AUTO_SYNC_MINUTES)
            ).strftime("%Y-%m-%d %H:%M:%S")


@api.route("/calendar/feeds", methods=["GET"])
@require_token
def list_feeds():
    conn = connect()
    rows = many(conn,
        "SELECT f.*, (SELECT COUNT(*) FROM events e WHERE e.feed_id=f.id) AS event_count "
        "FROM calendar_feeds f WHERE f.profile_id=? ORDER BY f.id",
        (active_profile(),))
    conn.close()
    # Never hand the URL back to the browser: it is a bearer credential for
    # the roster, and it has no business sitting in the DOM.
    for r in rows:
        r["url_host"] = urllib.parse.urlparse(r.pop("url", "")).netloc
        r["auto_sync_minutes"] = calendars.AUTO_SYNC_MINUTES
        r["next_sync_at"] = _next_sync_at(r)
    return jsonify(rows)


@api.route("/calendar/feeds", methods=["POST"])
@require_token
def create_feed():
    d = body()
    url = (d.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://", "webcal://")):
        return jsonify({"error": "url must be http(s) or webcal"}), 400
    url = re.sub(r"^webcal://", "https://", url, flags=re.I)

    conn = connect()
    cur = conn.execute(
        "INSERT INTO calendar_feeds (profile_id,name,url,color) VALUES (?,?,?,?)",
        (active_profile(), (d.get("name") or "Subscribed calendar").strip(),
         url, _safe_color(d.get("color"), "blue")),
    )
    fid = cur.lastrowid
    conn.commit()

    feed = one(conn, "SELECT * FROM calendar_feeds WHERE id=?", (fid,))
    result = calendars.sync_feed(conn, feed)
    if not result.get("ok"):
        # A feed that cannot be read is not worth keeping - report why and
        # leave nothing behind rather than a permanently broken row.
        conn.execute("DELETE FROM calendar_feeds WHERE id=?", (fid,))
        conn.commit()
        conn.close()
        return jsonify(result), 400
    conn.commit()
    conn.close()
    return jsonify({"id": fid, **result}), 201


@api.route("/calendar/feeds/<int:fid>/sync", methods=["POST"])
@require_token
def sync_feed_now(fid):
    conn = connect()
    feed = one(conn, "SELECT * FROM calendar_feeds WHERE id=? AND profile_id=?",
               (fid, active_profile()))
    if not feed:
        conn.close()
        return jsonify({"error": "no such feed"}), 404
    result = calendars.sync_feed(conn, feed)
    if not result.get("ok"):
        conn.execute("UPDATE calendar_feeds SET last_status=?, last_synced_at=datetime('now') "
                     "WHERE id=?", (result.get("error", "failed")[:200], fid))
    conn.commit()
    conn.close()
    return jsonify(result), 200 if result.get("ok") else 502


@api.route("/calendar/feeds/<int:fid>", methods=["PATCH", "DELETE"])
@require_token
def modify_feed(fid):
    conn = connect()
    feed = one(conn, "SELECT * FROM calendar_feeds WHERE id=? AND profile_id=?",
               (fid, active_profile()))
    if not feed:
        conn.close()
        return jsonify({"error": "no such feed"}), 404

    if request.method == "DELETE":
        # Take the imported events with it: leaving orphaned shifts behind
        # after unsubscribing would be surprising.
        removed = conn.execute("DELETE FROM events WHERE feed_id=?", (fid,)).rowcount
        conn.execute("DELETE FROM calendar_feeds WHERE id=?", (fid,))
        conn.commit()
        conn.close()
        return jsonify({"deleted": fid, "events_removed": removed})

    d = body()
    for f in ("name", "enabled"):
        if f in d:
            conn.execute(f"UPDATE calendar_feeds SET {f}=? WHERE id=?", (d[f], fid))
    if "color" in d:
        conn.execute("UPDATE calendar_feeds SET color=? WHERE id=?",
                     (_safe_color(d["color"], "blue"), fid))
        conn.execute("UPDATE events SET color=? WHERE feed_id=?",
                     (_safe_color(d["color"], "blue"), fid))
    conn.commit()
    out = one(conn, "SELECT * FROM calendar_feeds WHERE id=?", (fid,))
    conn.close()
    out["url_host"] = urllib.parse.urlparse(out.pop("url", "")).netloc
    return jsonify(out)


@api.route("/calendar/feeds/sync-all", methods=["POST"])
@require_token
def sync_all_feeds():
    conn = connect()
    feeds = many(conn, "SELECT * FROM calendar_feeds WHERE profile_id=? AND enabled=1",
                 (active_profile(),))
    out = []
    for feed in feeds:
        r = calendars.sync_feed(conn, feed)
        if not r.get("ok"):
            conn.execute("UPDATE calendar_feeds SET last_status=?, "
                         "last_synced_at=datetime('now') WHERE id=?",
                         (r.get("error", "failed")[:200], feed["id"]))
        out.append({"id": feed["id"], "name": feed["name"], **r})
    conn.commit()
    conn.close()
    return jsonify(out)


# ----------------------------------------------------------------- health
# Ingest is provider-agnostic on purpose: the Google connector is one
# caller, but a Tasker task or a curl one-liner can POST the same shape.

@api.route("/health", methods=["GET"])
@require_token
def get_health():
    conn = connect()
    out = {
        "summary": health.summary(conn, active_profile()),
        "series": health.series(conn, active_profile(),
                                int(request.args.get("days", 30)),
                                request.args.get("metric")),
        "metrics": health.METRICS,
        "provider": {
            "configured": health.configured(),
            "connected": health.connected(conn, active_profile()),
        },
    }
    conn.close()
    return jsonify(out)


@api.route("/health", methods=["POST"])
@require_token
def post_health():
    """
    Record one metric, or many. Accepts either:
        {"metric": "steps", "value": 8400, "date": "2026-08-04"}
        {"entries": [{...}, {...}]}
    `date` defaults to today, `source` to 'manual'.
    """
    d = body()
    entries = d.get("entries") or [d]
    conn = connect()
    written, errors = 0, []
    for e in entries:
        try:
            health.record(
                conn, active_profile(), e["metric"], e["value"],
                local_date=e.get("date"), unit=e.get("unit"),
                source=e.get("source", "manual"),
            )
            written += 1
        except (KeyError, ValueError, TypeError) as exc:
            errors.append(str(exc))
    conn.commit()
    conn.close()
    status = 201 if written and not errors else (207 if written else 400)
    return jsonify({"written": written, "errors": errors}), status


@api.route("/health/summary", methods=["GET"])
@require_token
def health_summary():
    conn = connect()
    out = health.summary(conn, active_profile(), int(request.args.get("days", 7)))
    conn.close()
    return jsonify(out)


@api.route("/health/detail", methods=["GET"])
@require_token
def health_detail():
    """Everything about one metric: stats, series, weekday shape, sources."""
    metric = request.args.get("metric")
    if not metric:
        return jsonify({"error": "metric required"}), 400
    conn = connect()
    out = health.detail(conn, active_profile(), metric,
                        int(request.args.get("days", 30)))
    conn.close()
    return jsonify(out)


@api.route("/health/stats", methods=["GET"])
@require_token
def health_stats():
    """Stats for one metric, or every tracked metric when none is named."""
    conn = connect()
    pid = active_profile()
    days = int(request.args.get("days", 30))
    metric = request.args.get("metric")
    if metric:
        out = health.stats(conn, pid, metric, days)
    else:
        out = {m: health.stats(conn, pid, m, days)
               for m in health.tracked_metrics(conn, pid)}
    conn.close()
    return jsonify(out)


@api.route("/health/raw", methods=["GET"])
@require_token
def health_raw():
    """Every stored row, filterable - the 'let me see all of it' endpoint."""
    conn = connect()
    pid = active_profile()
    out = {
        "rows": health.raw(
            conn, pid,
            metric=request.args.get("metric"),
            source=request.args.get("source"),
            start=request.args.get("start"),
            end=request.args.get("end"),
            limit=int(request.args.get("limit", 500)),
        ),
        "tracked": health.tracked_metrics(conn, pid),
        "sources": health.by_source(conn, pid, days=3650),
    }
    conn.close()
    return jsonify(out)


@api.route("/health/connect", methods=["GET"])
@require_token
def health_connect():
    """Returns the Google consent URL for the browser to open."""
    if not health.configured():
        return jsonify({"error": "Set OPSDECK_GOOGLE_CLIENT_ID and "
                                 "OPSDECK_GOOGLE_CLIENT_SECRET, then restart."}), 400
    # State carries the profile so the callback knows whose tokens these are,
    # and a nonce so a stray callback cannot bind an unrelated account.
    nonce = uuid.uuid4().hex
    conn = connect()
    _set_setting(conn, f"oauth_state_{nonce}", active_profile())
    conn.commit()
    conn.close()
    return jsonify({"url": health.auth_url(active_profile(), nonce)})


@api.route("/health/callback", methods=["GET"])
def health_callback():
    """
    Google redirects the browser here. Deliberately not @require_token - it
    is a top-level browser navigation and cannot carry the header; the state
    nonce is what proves the exchange was one we started.
    """
    code = request.args.get("code")
    state = request.args.get("state", "")
    if not code:
        return "<h3>Authorisation failed or was cancelled.</h3>", 400

    conn = connect()
    row = one(conn, "SELECT value FROM settings WHERE key=?", (f"oauth_state_{state}",))
    if not row:
        conn.close()
        return "<h3>Unrecognised or expired authorisation attempt.</h3>", 400
    profile_id = row["value"]
    conn.execute("DELETE FROM settings WHERE key=?", (f"oauth_state_{state}",))

    try:
        health.exchange_code(conn, profile_id, code)
        conn.commit()
    except Exception as exc:
        conn.close()
        return f"<h3>Token exchange failed:</h3><pre>{exc}</pre>", 502
    conn.close()
    return ("<h3>Health connected.</h3>"
            "<p>You can close this tab and return to Ops Deck.</p>"
            "<script>setTimeout(()=>window.close(),1500)</script>")


@api.route("/health/sync", methods=["POST"])
@require_token
def health_sync():
    conn = connect()
    out = health.sync(conn, active_profile(), int(body().get("days", 7)))
    conn.close()
    return jsonify(out), 200 if out.get("ok") else 400


@api.route("/health/disconnect", methods=["POST"])
@require_token
def health_disconnect():
    conn = connect()
    health.disconnect(conn, active_profile())
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# --------------------------------------------------------- profiles / settings

@api.route("/profiles", methods=["GET"])
@require_token
def list_profiles():
    conn = connect()
    rows = many(conn, "SELECT id,type,display_name,avatar_url,position FROM profiles ORDER BY position, id")
    conn.close()
    return jsonify(rows)


@api.route("/profiles/<pid>", methods=["PATCH"])
@require_token
def update_profile(pid):
    d = body()
    conn = connect()
    if not one(conn, "SELECT 1 FROM profiles WHERE id=?", (pid,)):
        conn.close()
        return jsonify({"error": "no such profile"}), 404
    for f in ("display_name", "avatar_url"):
        if f in d:
            conn.execute(f"UPDATE profiles SET {f}=? WHERE id=?", (d[f], pid))
    conn.commit()
    out = one(conn, "SELECT id,type,display_name,avatar_url,position FROM profiles WHERE id=?", (pid,))
    conn.close()
    return jsonify(out)


@api.route("/profiles/<pid>/settings", methods=["GET"])
@require_token
def get_settings(pid):
    conn = connect()
    row = one(conn, "SELECT settings FROM profile_settings WHERE profile_id=?", (pid,))
    conn.close()
    if not row:
        return jsonify({"error": "no such profile"}), 404
    return jsonify(json.loads(row["settings"] or "{}"))


@api.route("/profiles/<pid>/settings", methods=["PATCH"])
@require_token
def update_settings(pid):
    """Deep-merges the patch into the stored settings rather than replacing."""
    patch = body()
    conn = connect()
    row = one(conn, "SELECT settings FROM profile_settings WHERE profile_id=?", (pid,))
    if not row:
        conn.close()
        return jsonify({"error": "no such profile"}), 404
    current = json.loads(row["settings"] or "{}")

    def deep_merge(dst, src):
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                deep_merge(dst[k], v)
            else:
                dst[k] = v
        return dst

    merged = deep_merge(current, patch)
    conn.execute("UPDATE profile_settings SET settings=? WHERE profile_id=?",
                 (json.dumps(merged), pid))
    conn.commit()
    conn.close()
    return jsonify(merged)


# ----------------------------------------------------------------- themes

@api.route("/themes", methods=["GET"])
@require_token
def list_themes():
    conn = connect()
    rows = many(conn, "SELECT * FROM themes ORDER BY is_custom, id")
    conn.close()
    for r in rows:
        r["colors"] = json.loads(r["colors"])
        r["is_custom"] = bool(r["is_custom"])
    return jsonify(rows)


@api.route("/themes/<tid>", methods=["GET"])
@require_token
def get_theme(tid):
    conn = connect()
    row = one(conn, "SELECT * FROM themes WHERE id=?", (tid,))
    conn.close()
    if not row:
        return jsonify({"error": "no such theme"}), 404
    row["colors"] = json.loads(row["colors"])
    row["is_custom"] = bool(row["is_custom"])
    return jsonify(row)


@api.route("/themes", methods=["POST"])
@require_token
def create_theme():
    d = body()
    if not d.get("colors"):
        return jsonify({"error": "colors required"}), 400
    tid = d.get("id") or f"custom-{uuid.uuid4().hex[:8]}"
    conn = connect()
    conn.execute(
        "INSERT INTO themes (id,name,is_custom,owner_profile_id,colors) VALUES (?,?,1,?,?)",
        (tid, d.get("name", "Custom"), d.get("owner_profile_id") or active_profile(),
         json.dumps(d["colors"])),
    )
    conn.commit()
    row = one(conn, "SELECT * FROM themes WHERE id=?", (tid,))
    conn.close()
    row["colors"] = json.loads(row["colors"])
    row["is_custom"] = bool(row["is_custom"])
    return jsonify(row), 201


@api.route("/themes/<tid>", methods=["PATCH", "DELETE"])
@require_token
def modify_theme(tid):
    conn = connect()
    row = one(conn, "SELECT * FROM themes WHERE id=?", (tid,))
    if not row:
        conn.close()
        return jsonify({"error": "no such theme"}), 404
    if not row["is_custom"]:
        conn.close()
        return jsonify({"error": "built-in themes cannot be edited"}), 403

    if request.method == "DELETE":
        conn.execute("DELETE FROM themes WHERE id=?", (tid,))
        conn.commit()
        conn.close()
        return jsonify({"deleted": tid})

    d = body()
    if "name" in d:
        conn.execute("UPDATE themes SET name=? WHERE id=?", (d["name"], tid))
    if "colors" in d:
        conn.execute("UPDATE themes SET colors=? WHERE id=?", (json.dumps(d["colors"]), tid))
    conn.commit()
    out = one(conn, "SELECT * FROM themes WHERE id=?", (tid,))
    conn.close()
    out["colors"] = json.loads(out["colors"])
    out["is_custom"] = bool(out["is_custom"])
    return jsonify(out)


# ------------------------------------------------------------ notifications

@api.route("/notifications", methods=["GET"])
@require_token
def list_notifications():
    only_unseen = request.args.get("unseen") == "1"
    conn = connect()
    sql = "SELECT * FROM notifications WHERE profile_id=?"
    params = [active_profile()]
    if only_unseen:
        sql += " AND seen=0"
    sql += " ORDER BY created_at DESC LIMIT 50"
    rows = many(conn, sql, params)
    conn.close()
    return jsonify(rows)


@api.route("/notifications/<int:nid>/seen", methods=["POST"])
@require_token
def mark_notification_seen(nid):
    conn = connect()
    conn.execute("UPDATE notifications SET seen=1 WHERE id=? AND profile_id=?",
                 (nid, active_profile()))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@api.route("/notifications/seen-all", methods=["POST"])
@require_token
def mark_all_seen():
    conn = connect()
    conn.execute("UPDATE notifications SET seen=1 WHERE profile_id=?", (active_profile(),))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ----------------------------------------------------------------- boards

@api.route("/boards", methods=["GET"])
@require_token
def list_boards():
    include_archived = request.args.get("archived") == "1"
    conn = connect()
    sql = "SELECT * FROM boards WHERE profile_id=?"
    params = [active_profile()]
    if not include_archived:
        sql += " AND archived = 0"
    sql += " ORDER BY position, id"
    boards = many(conn, sql, params)
    for b in boards:
        b["lists"] = many(
            conn,
            "SELECT * FROM lists WHERE board_id=? AND archived=0 ORDER BY position, id",
            (b["id"],),
        )
        for lst in b["lists"]:
            lst["cards"] = load_cards(conn, lst["id"])
        b["labels"] = many(conn, "SELECT * FROM labels WHERE board_id=? ORDER BY id", (b["id"],))
    conn.close()
    return jsonify(boards)


def load_cards(conn, list_id):
    cards = many(
        conn,
        "SELECT * FROM cards WHERE list_id=? AND archived=0 ORDER BY position, id",
        (list_id,),
    )
    for c in cards:
        c["labels"] = many(
            conn,
            "SELECT l.* FROM labels l JOIN card_labels cl ON cl.label_id=l.id WHERE cl.card_id=?",
            (c["id"],),
        )
        c["checklist"] = many(
            conn,
            "SELECT * FROM checklist_items WHERE card_id=? ORDER BY position, id",
            (c["id"],),
        )
        c["attachments"] = many(
            conn,
            "SELECT id, filename, mime, size FROM uploads WHERE card_id=?",
            (c["id"],),
        )
    return cards


@api.route("/boards", methods=["POST"])
@require_token
def create_board():
    d = body()
    conn = connect()
    pid = active_profile()
    pos = conn.execute(
        "SELECT COALESCE(MAX(position),-1)+1 FROM boards WHERE profile_id=?", (pid,)
    ).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO boards (title, position, profile_id) VALUES (?,?,?)",
        (d.get("title", "Untitled board"), pos, pid),
    )
    bid = cur.lastrowid
    for i, name in enumerate(d.get("lists", ["To do", "In progress", "Done"])):
        conn.execute("INSERT INTO lists (board_id,title,position) VALUES (?,?,?)", (bid, name, i))
    conn.commit()
    out = one(conn, "SELECT * FROM boards WHERE id=?", (bid,))
    conn.close()
    return jsonify(out), 201


@api.route("/boards/<int:bid>", methods=["PATCH"])
@require_token
def update_board(bid):
    d = body()
    conn = connect()
    for field in ("title", "position", "archived"):
        if field in d:
            conn.execute(f"UPDATE boards SET {field}=? WHERE id=?", (d[field], bid))
    conn.commit()
    out = one(conn, "SELECT * FROM boards WHERE id=?", (bid,))
    conn.close()
    return jsonify(out)


@api.route("/boards/<int:bid>", methods=["DELETE"])
@require_token
def delete_board(bid):
    conn = connect()
    conn.execute("DELETE FROM boards WHERE id=?", (bid,))
    conn.commit()
    conn.close()
    return jsonify({"deleted": bid})


# ------------------------------------------------------------------ lists

@api.route("/lists", methods=["POST"])
@require_token
def create_list():
    d = body()
    conn = connect()
    pos = conn.execute(
        "SELECT COALESCE(MAX(position),-1)+1 FROM lists WHERE board_id=?", (d["board_id"],)
    ).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO lists (board_id,title,position) VALUES (?,?,?)",
        (d["board_id"], d.get("title", "New list"), d.get("position", pos)),
    )
    conn.commit()
    out = one(conn, "SELECT * FROM lists WHERE id=?", (cur.lastrowid,))
    conn.close()
    return jsonify(out), 201


@api.route("/lists/<int:lid>", methods=["PATCH"])
@require_token
def update_list(lid):
    d = body()
    conn = connect()
    for field in ("title", "position", "archived"):
        if field in d:
            conn.execute(f"UPDATE lists SET {field}=? WHERE id=?", (d[field], lid))
    conn.commit()
    out = one(conn, "SELECT * FROM lists WHERE id=?", (lid,))
    conn.close()
    return jsonify(out)


@api.route("/lists/<int:lid>", methods=["DELETE"])
@require_token
def delete_list(lid):
    conn = connect()
    conn.execute("DELETE FROM lists WHERE id=?", (lid,))
    conn.commit()
    conn.close()
    return jsonify({"deleted": lid})


# ------------------------------------------------------------------ cards

@api.route("/cards", methods=["POST"])
@require_token
def create_card():
    d = body()
    conn = connect()
    pos = conn.execute(
        "SELECT COALESCE(MAX(position),-1)+1 FROM cards WHERE list_id=?", (d["list_id"],)
    ).fetchone()[0]
    cur = conn.execute(
        """INSERT INTO cards (list_id,title,description,due_at,position)
           VALUES (?,?,?,?,?)""",
        (
            d["list_id"],
            d.get("title", "Untitled"),
            d.get("description", ""),
            d.get("due_at"),
            d.get("position", pos),
        ),
    )
    cid = cur.lastrowid
    for lbl in d.get("label_ids", []):
        conn.execute("INSERT OR IGNORE INTO card_labels VALUES (?,?)", (cid, lbl))
    for i, item in enumerate(d.get("checklist", [])):
        conn.execute(
            "INSERT INTO checklist_items (card_id,text,position) VALUES (?,?,?)", (cid, item, i)
        )
    conn.commit()
    out = load_cards(conn, d["list_id"])
    conn.close()
    return jsonify(next(c for c in out if c["id"] == cid)), 201


@api.route("/cards/<int:cid>", methods=["PATCH"])
@require_token
def update_card(cid):
    d = body()
    conn = connect()
    for field in ("title", "description", "due_at", "completed", "position", "list_id", "archived"):
        if field in d:
            conn.execute(f"UPDATE cards SET {field}=? WHERE id=?", (d[field], cid))
    # XP is derived from when work happened, so completion needs a timestamp.
    # Clearing `completed` clears it too, otherwise un-completing and
    # re-completing a card would mint XP repeatedly.
    if "completed" in d:
        if d["completed"]:
            conn.execute(
                "UPDATE cards SET completed_at=COALESCE(completed_at, datetime('now')) WHERE id=?",
                (cid,),
            )
            # A card completed on the joint board feeds relationship XP.
            owner = one(
                conn,
                "SELECT b.profile_id FROM cards c JOIN lists l ON l.id=c.list_id "
                "JOIN boards b ON b.id=l.board_id WHERE c.id=?",
                (cid,),
            )
            if owner and owner["profile_id"] == "joint":
                social.log_activity(conn, profile_id="joint",
                                    source_type="joint_card_done", source_id=str(cid))
        else:
            conn.execute("UPDATE cards SET completed_at=NULL WHERE id=?", (cid,))
    if "label_ids" in d:
        conn.execute("DELETE FROM card_labels WHERE card_id=?", (cid,))
        for lbl in d["label_ids"]:
            conn.execute("INSERT OR IGNORE INTO card_labels VALUES (?,?)", (cid, lbl))
    conn.commit()
    row = one(conn, "SELECT list_id FROM cards WHERE id=?", (cid,))
    cards = load_cards(conn, row["list_id"]) if row else []
    conn.close()
    return jsonify(next((c for c in cards if c["id"] == cid), {}))


@api.route("/cards/<int:cid>", methods=["DELETE"])
@require_token
def delete_card(cid):
    conn = connect()
    conn.execute("DELETE FROM cards WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    return jsonify({"deleted": cid})


@api.route("/cards/reorder", methods=["POST"])
@require_token
def reorder_cards():
    """Body: {"list_id": N, "card_ids": [3,1,2]} - sets positions in order."""
    d = body()
    conn = connect()
    for pos, cid in enumerate(d["card_ids"]):
        conn.execute("UPDATE cards SET list_id=?, position=? WHERE id=?", (d["list_id"], pos, cid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# -------------------------------------------------------- checklist items

@api.route("/cards/<int:cid>/checklist", methods=["POST"])
@require_token
def add_checklist_item(cid):
    d = body()
    conn = connect()
    pos = conn.execute(
        "SELECT COALESCE(MAX(position),-1)+1 FROM checklist_items WHERE card_id=?", (cid,)
    ).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO checklist_items (card_id,text,position) VALUES (?,?,?)",
        (cid, d.get("text", ""), pos),
    )
    conn.commit()
    out = one(conn, "SELECT * FROM checklist_items WHERE id=?", (cur.lastrowid,))
    conn.close()
    return jsonify(out), 201


@api.route("/checklist/<int:iid>", methods=["PATCH"])
@require_token
def update_checklist_item(iid):
    d = body()
    conn = connect()
    for field in ("text", "done", "position"):
        if field in d:
            conn.execute(f"UPDATE checklist_items SET {field}=? WHERE id=?", (d[field], iid))
    if "done" in d:
        if d["done"]:
            conn.execute(
                "UPDATE checklist_items SET done_at=COALESCE(done_at, datetime('now')) WHERE id=?",
                (iid,),
            )
        else:
            conn.execute("UPDATE checklist_items SET done_at=NULL WHERE id=?", (iid,))
    conn.commit()
    out = one(conn, "SELECT * FROM checklist_items WHERE id=?", (iid,))
    conn.close()
    return jsonify(out)


@api.route("/checklist/<int:iid>", methods=["DELETE"])
@require_token
def delete_checklist_item(iid):
    conn = connect()
    conn.execute("DELETE FROM checklist_items WHERE id=?", (iid,))
    conn.commit()
    conn.close()
    return jsonify({"deleted": iid})


# ----------------------------------------------------------------- labels

@api.route("/labels", methods=["POST"])
@require_token
def create_label():
    d = body()
    conn = connect()
    cur = conn.execute(
        "INSERT INTO labels (board_id,name,color) VALUES (?,?,?)",
        (d["board_id"], d.get("name", "Label"), d.get("color", "gray")),
    )
    conn.commit()
    out = one(conn, "SELECT * FROM labels WHERE id=?", (cur.lastrowid,))
    conn.close()
    return jsonify(out), 201


@api.route("/labels/<int:lid>", methods=["DELETE"])
@require_token
def delete_label(lid):
    conn = connect()
    conn.execute("DELETE FROM labels WHERE id=?", (lid,))
    conn.commit()
    conn.close()
    return jsonify({"deleted": lid})


# ----------------------------------------------------------------- events

@api.route("/events", methods=["GET"])
@require_token
def list_events():
    """
    ?start=YYYY-MM-DD&end=YYYY-MM-DD returns expanded occurrences.
    Without a window, returns raw event rows (the series definitions).
    """
    conn = connect()
    pid = active_profile()
    if "start" not in request.args:
        rows = many(conn, "SELECT * FROM events WHERE profile_id=? ORDER BY start_at", (pid,))
        for r in rows:
            r["rrule_label"] = describe_rrule(r["rrule"])
        conn.close()
        return jsonify(rows)

    win_start = parse_dt(request.args["start"])
    win_end = parse_dt(request.args.get("end", request.args["start"])) + timedelta(days=1)

    events = many(conn, "SELECT * FROM events WHERE profile_id=?", (pid,))
    all_overrides = many(conn, "SELECT * FROM event_overrides")
    conn.close()

    by_event = {}
    for ov in all_overrides:
        by_event.setdefault(ov["event_id"], {})[ov["occurrence"]] = ov

    out = []
    for ev in events:
        out.extend(expand_event(ev, win_start, win_end, by_event.get(ev["id"])))
    out.sort(key=lambda o: o["start_at"])
    return jsonify(out)


@api.route("/events", methods=["POST"])
@require_token
def create_event():
    d = body()
    conn = connect()
    cur = conn.execute(
        """INSERT INTO events (title,description,location,start_at,end_at,all_day,rrule,color,remind_min,profile_id)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            d.get("title", "Untitled"),
            d.get("description", ""),
            d.get("location", ""),
            d["start_at"],
            d.get("end_at"),
            int(d.get("all_day", 0)),
            d.get("rrule") or None,
            d.get("color", "blue"),
            d.get("remind_min"),
            active_profile(),
        ),
    )
    conn.commit()
    out = one(conn, "SELECT * FROM events WHERE id=?", (cur.lastrowid,))
    conn.close()
    return jsonify(out), 201


@api.route("/events/<int:eid>", methods=["PATCH"])
@require_token
def update_event(eid):
    d = body()
    conn = connect()
    for f in ("title", "description", "location", "start_at", "end_at",
              "all_day", "rrule", "color", "remind_min"):
        if f in d:
            conn.execute(f"UPDATE events SET {f}=? WHERE id=?", (d[f] or None, eid))
    conn.commit()
    out = one(conn, "SELECT * FROM events WHERE id=?", (eid,))
    conn.close()
    return jsonify(out)


@api.route("/events/<int:eid>", methods=["DELETE"])
@require_token
def delete_event(eid):
    conn = connect()
    conn.execute("DELETE FROM events WHERE id=?", (eid,))
    conn.commit()
    conn.close()
    return jsonify({"deleted": eid})


@api.route("/events/<int:eid>/occurrences/<occurrence>", methods=["POST", "DELETE"])
@require_token
def override_occurrence(eid, occurrence):
    """
    POST {"action":"skip"} or {"action":"move","new_start_at":...} to
    change a single instance of a recurring series.
    DELETE removes the override, restoring the default occurrence.
    """
    conn = connect()
    if request.method == "DELETE":
        conn.execute(
            "DELETE FROM event_overrides WHERE event_id=? AND occurrence=?", (eid, occurrence)
        )
        conn.commit()
        conn.close()
        return jsonify({"restored": occurrence})

    d = body()
    conn.execute(
        """INSERT INTO event_overrides (event_id,occurrence,action,new_start_at,new_end_at,new_title)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(event_id,occurrence) DO UPDATE SET
             action=excluded.action, new_start_at=excluded.new_start_at,
             new_end_at=excluded.new_end_at, new_title=excluded.new_title""",
        (
            eid, occurrence, d.get("action", "skip"),
            d.get("new_start_at"), d.get("new_end_at"), d.get("new_title"),
        ),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "occurrence": occurrence})


# --------------------------------------------------------------- routines

@api.route("/routines", methods=["GET"])
@require_token
def list_routines():
    date = request.args.get("date", today_local().isoformat())
    conn = connect()
    routines = many(
        conn,
        "SELECT * FROM routines WHERE active=1 AND profile_id=? ORDER BY time_group, position, id",
        (active_profile(),),
    )
    done = {
        r["routine_id"]
        for r in many(conn, "SELECT routine_id FROM routine_completions WHERE local_date=?", (date,))
    }
    for r in routines:
        r["done_today"] = r["id"] in done
        r["streak"] = compute_streak(conn, r["id"], date)
    conn.close()
    return jsonify({"date": date, "routines": routines})


def compute_streak(conn, routine_id, upto_date):
    """Count consecutive days completed, walking backwards from upto_date."""
    rows = conn.execute(
        "SELECT local_date FROM routine_completions WHERE routine_id=? AND local_date<=? "
        "ORDER BY local_date DESC LIMIT 400",
        (routine_id, upto_date),
    ).fetchall()
    dates = {r["local_date"] for r in rows}
    if not dates:
        return 0
    streak = 0
    cursor = datetime.fromisoformat(upto_date).date()
    # Today not being done yet shouldn't zero out a live streak.
    if cursor.isoformat() not in dates:
        cursor -= timedelta(days=1)
    while cursor.isoformat() in dates:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


@api.route("/routines", methods=["POST"])
@require_token
def create_routine():
    d = body()
    conn = connect()
    pid = active_profile()
    pos = conn.execute(
        "SELECT COALESCE(MAX(position),-1)+1 FROM routines WHERE time_group=? AND profile_id=?",
        (d.get("time_group", "anytime"), pid),
    ).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO routines (name,time_group,notes,position,profile_id) VALUES (?,?,?,?,?)",
        (d.get("name", "New routine"), d.get("time_group", "anytime"), d.get("notes", ""), pos, pid),
    )
    conn.commit()
    out = one(conn, "SELECT * FROM routines WHERE id=?", (cur.lastrowid,))
    conn.close()
    return jsonify(out), 201


@api.route("/routines/<int:rid>", methods=["PATCH"])
@require_token
def update_routine(rid):
    d = body()
    conn = connect()
    for f in ("name", "time_group", "notes", "position", "active"):
        if f in d:
            conn.execute(f"UPDATE routines SET {f}=? WHERE id=?", (d[f], rid))
    conn.commit()
    out = one(conn, "SELECT * FROM routines WHERE id=?", (rid,))
    conn.close()
    return jsonify(out)


@api.route("/routines/<int:rid>", methods=["DELETE"])
@require_token
def delete_routine(rid):
    conn = connect()
    conn.execute("DELETE FROM routines WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    return jsonify({"deleted": rid})


@api.route("/routines/<int:rid>/toggle", methods=["POST"])
@require_token
def toggle_routine(rid):
    date = body().get("date", today_local().isoformat())
    conn = connect()
    existing = one(
        conn,
        "SELECT id FROM routine_completions WHERE routine_id=? AND local_date=?",
        (rid, date),
    )
    if existing:
        conn.execute("DELETE FROM routine_completions WHERE id=?", (existing["id"],))
        done = False
    else:
        conn.execute(
            "INSERT INTO routine_completions (routine_id,local_date) VALUES (?,?)", (rid, date)
        )
        done = True
        # Feed the relationship activity log. Completing a routine that lives
        # on the joint profile counts more (joint_card_done weight); a
        # personal routine still nudges it a little. social.log_activity is a
        # no-op-safe helper that also fires milestone/companion updates.
        owner = one(conn, "SELECT profile_id FROM routines WHERE id=?", (rid,))
        social.log_activity(
            conn,
            profile_id=owner["profile_id"] if owner else active_profile(),
            source_type="routine_completion",
            source_id=str(rid),
        )
    conn.commit()
    streak = compute_streak(conn, rid, date)
    conn.close()
    return jsonify({"routine_id": rid, "date": date, "done_today": done, "streak": streak})


@api.route("/routines/history", methods=["GET"])
@require_token
def routine_history():
    """?days=30 - completion counts per day, for the heatmap."""
    days = int(request.args.get("days", 30))
    start = (today_local() - timedelta(days=days - 1)).isoformat()
    conn = connect()
    pid = active_profile()
    total = conn.execute(
        "SELECT COUNT(*) FROM routines WHERE active=1 AND profile_id=?", (pid,)
    ).fetchone()[0]
    rows = many(
        conn,
        "SELECT rc.local_date, COUNT(*) AS done FROM routine_completions rc "
        "JOIN routines r ON r.id=rc.routine_id "
        "WHERE rc.local_date>=? AND r.profile_id=? "
        "GROUP BY rc.local_date ORDER BY rc.local_date",
        (start, pid),
    )
    conn.close()
    return jsonify({"total_routines": total, "days": rows})


# ------------------------------------------------------------------- docs

@api.route("/docs", methods=["GET"])
@require_token
def list_docs():
    conn = connect()
    include_body = request.args.get("body") == "1"
    cols = "*" if include_body else "id,title,kind,folder,created_at,updated_at"
    docs = many(conn, f"SELECT {cols} FROM docs WHERE profile_id=? ORDER BY updated_at DESC",
                (active_profile(),))
    for d in docs:
        d["tags"] = [r["tag"] for r in many(conn, "SELECT tag FROM doc_tags WHERE doc_id=?", (d["id"],))]
    conn.close()
    return jsonify(docs)


@api.route("/docs/<int:did>", methods=["GET"])
@require_token
def get_doc(did):
    conn = connect()
    doc = one(conn, "SELECT * FROM docs WHERE id=?", (did,))
    if not doc:
        conn.close()
        abort(404)
    doc["tags"] = [r["tag"] for r in many(conn, "SELECT tag FROM doc_tags WHERE doc_id=?", (did,))]
    conn.close()
    return jsonify(doc)


@api.route("/docs", methods=["POST"])
@require_token
def create_doc():
    d = body()
    conn = connect()
    cur = conn.execute(
        "INSERT INTO docs (title,kind,body,folder,profile_id) VALUES (?,?,?,?,?)",
        (d.get("title", "Untitled"), d.get("kind", "md"), d.get("body", ""),
         d.get("folder", ""), active_profile()),
    )
    did = cur.lastrowid
    for tag in d.get("tags", []):
        conn.execute("INSERT OR IGNORE INTO doc_tags VALUES (?,?)", (did, tag))
    conn.commit()
    out = one(conn, "SELECT * FROM docs WHERE id=?", (did,))
    conn.close()
    return jsonify(out), 201


@api.route("/docs/<int:did>", methods=["PATCH"])
@require_token
def update_doc(did):
    d = body()
    conn = connect()
    for f in ("title", "kind", "body", "folder"):
        if f in d:
            conn.execute(f"UPDATE docs SET {f}=? WHERE id=?", (d[f], did))
    conn.execute("UPDATE docs SET updated_at=datetime('now') WHERE id=?", (did,))
    if "tags" in d:
        conn.execute("DELETE FROM doc_tags WHERE doc_id=?", (did,))
        for tag in d["tags"]:
            conn.execute("INSERT OR IGNORE INTO doc_tags VALUES (?,?)", (did, tag))
    conn.commit()
    out = one(conn, "SELECT * FROM docs WHERE id=?", (did,))
    conn.close()
    return jsonify(out)


@api.route("/docs/<int:did>", methods=["DELETE"])
@require_token
def delete_doc(did):
    conn = connect()
    conn.execute("DELETE FROM docs WHERE id=?", (did,))
    conn.commit()
    conn.close()
    return jsonify({"deleted": did})


@api.route("/docs/upload", methods=["POST"])
@require_token
def upload_doc():
    """Multipart upload of a .md/.markdown/.html/.txt file into the docs store."""
    if "file" not in request.files:
        return jsonify({"error": "no file part"}), 400
    f = request.files["file"]
    name = secure_filename(f.filename or "untitled")
    ext = Path(name).suffix.lower().lstrip(".")
    if ext not in ("md", "markdown", "html", "htm", "txt"):
        return jsonify({"error": f"unsupported extension .{ext}"}), 400

    raw = f.read()
    if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
        return jsonify({"error": f"file larger than {MAX_UPLOAD_MB}MB"}), 413

    kind = "html" if ext in ("html", "htm") else "md"
    conn = connect()
    cur = conn.execute(
        "INSERT INTO docs (title,kind,body,folder,profile_id) VALUES (?,?,?,?,?)",
        (
            request.form.get("title") or Path(name).stem,
            kind,
            raw.decode("utf-8", errors="replace"),
            request.form.get("folder", ""),
            active_profile(),
        ),
    )
    conn.commit()
    out = one(conn, "SELECT id,title,kind,folder FROM docs WHERE id=?", (cur.lastrowid,))
    conn.close()
    return jsonify(out), 201


# ------------------------------------------------------- card attachments

@api.route("/cards/<int:cid>/attachments", methods=["POST"])
@require_token
def upload_attachment(cid):
    if "file" not in request.files:
        return jsonify({"error": "no file part"}), 400
    f = request.files["file"]
    name = secure_filename(f.filename or "file")
    raw = f.read()
    if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
        return jsonify({"error": f"file larger than {MAX_UPLOAD_MB}MB"}), 413

    stored = f"{uuid.uuid4().hex}_{name}"
    (UPLOAD_DIR / stored).write_bytes(raw)

    conn = connect()
    cur = conn.execute(
        "INSERT INTO uploads (card_id,filename,stored_as,mime,size) VALUES (?,?,?,?,?)",
        (cid, name, stored, f.mimetype or "", len(raw)),
    )
    conn.commit()
    out = one(conn, "SELECT id,filename,mime,size FROM uploads WHERE id=?", (cur.lastrowid,))
    conn.close()
    return jsonify(out), 201


@api.route("/attachments/<int:aid>", methods=["GET"])
@require_token
def get_attachment(aid):
    conn = connect()
    row = one(conn, "SELECT * FROM uploads WHERE id=?", (aid,))
    conn.close()
    if not row:
        abort(404)
    return send_file(
        UPLOAD_DIR / row["stored_as"],
        mimetype=row["mime"] or "application/octet-stream",
        download_name=row["filename"],
    )


@api.route("/attachments/<int:aid>", methods=["DELETE"])
@require_token
def delete_attachment(aid):
    conn = connect()
    row = one(conn, "SELECT * FROM uploads WHERE id=?", (aid,))
    if row:
        (UPLOAD_DIR / row["stored_as"]).unlink(missing_ok=True)
        conn.execute("DELETE FROM uploads WHERE id=?", (aid,))
        conn.commit()
    conn.close()
    return jsonify({"deleted": aid})


# ------------------------------------------------------------------ today

@api.route("/today", methods=["GET"])
@require_token
def today_view():
    """Everything happening today: events, routines, cards due."""
    date = request.args.get("date", today_local().isoformat())
    win_start = parse_dt(date)
    win_end = win_start + timedelta(days=1)

    conn = connect()
    pid = active_profile()
    events = many(conn, "SELECT * FROM events WHERE profile_id=?", (pid,))
    overrides = many(conn, "SELECT * FROM event_overrides")
    by_event = {}
    for ov in overrides:
        by_event.setdefault(ov["event_id"], {})[ov["occurrence"]] = ov

    occurrences = []
    for ev in events:
        occurrences.extend(expand_event(ev, win_start, win_end, by_event.get(ev["id"])))
    occurrences.sort(key=lambda o: o["start_at"])

    routines = many(
        conn,
        "SELECT * FROM routines WHERE active=1 AND profile_id=? ORDER BY time_group, position, id",
        (pid,),
    )
    done = {
        r["routine_id"]
        for r in many(conn, "SELECT routine_id FROM routine_completions WHERE local_date=?", (date,))
    }
    for r in routines:
        r["done_today"] = r["id"] in done
        r["streak"] = compute_streak(conn, r["id"], date)

    due_cards = many(
        conn,
        """SELECT c.*, l.title AS list_title, b.title AS board_title, b.id AS board_id
           FROM cards c
           JOIN lists l  ON l.id = c.list_id
           JOIN boards b ON b.id = l.board_id
           WHERE b.profile_id=? AND c.archived=0 AND c.completed=0
             AND c.due_at IS NOT NULL AND c.due_at < ?
           ORDER BY c.due_at""",
        (pid, win_end.isoformat()),
    )
    conn.close()

    overdue = [c for c in due_cards if c["due_at"] < date]
    today_cards = [c for c in due_cards if c["due_at"] >= date]

    return jsonify({
        "date": date,
        "events": occurrences,
        "routines": routines,
        "cards_due": today_cards,
        "cards_overdue": overdue,
    })


# ------------------------------------------------------------ reminders

@api.route("/reminders/upcoming", methods=["GET"])
@require_token
def upcoming_reminders():
    """
    Events starting within the next `minutes` window that want a reminder,
    plus cards due today. The browser polls this and raises notifications.
    """
    minutes = int(request.args.get("minutes", 120))
    now = now_local()
    win_end = now + timedelta(minutes=minutes)

    conn = connect()
    events = many(conn, "SELECT * FROM events WHERE remind_min IS NOT NULL AND profile_id=?",
                  (active_profile(),))
    overrides = many(conn, "SELECT * FROM event_overrides")
    by_event = {}
    for ov in overrides:
        by_event.setdefault(ov["event_id"], {})[ov["occurrence"]] = ov

    out = []
    for ev in events:
        for occ in expand_event(ev, now - timedelta(days=1), win_end, by_event.get(ev["id"])):
            start = parse_dt(occ["start_at"])
            fire_at = start - timedelta(minutes=occ["remind_min"] or 0)
            if now <= fire_at <= win_end:
                out.append({
                    "key": f"event-{ev['id']}-{occ['occurrence']}",
                    "title": occ["title"],
                    "fire_at": fmt_dt(fire_at),
                    "start_at": occ["start_at"],
                    "kind": "event",
                })
    conn.close()
    out.sort(key=lambda r: r["fire_at"])
    return jsonify(out)


# ----------------------------------------------------------------- search

@api.route("/search", methods=["GET"])
@require_token
def search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"cards": [], "docs": [], "events": []})
    like = f"%{q}%"

    # scope: 'mine' (the active profile only, default), 'joint' (the joint
    # profile's content), or 'all' (span every profile). A search from the
    # joint tab can reach either partner's docs when that's useful.
    scope = request.args.get("scope", "mine")
    if scope == "all":
        prof_clause, prof_params = "", ()
    elif scope == "joint":
        prof_clause, prof_params = " = ?", ("joint",)
    else:
        prof_clause, prof_params = " = ?", (active_profile(),)

    def pf(col):
        return "" if scope == "all" else f" AND {col}{prof_clause}"

    conn = connect()
    result = {
        "cards": many(
            conn,
            f"""SELECT c.id,c.title,c.due_at,l.title AS list_title,b.title AS board_title
               FROM cards c JOIN lists l ON l.id=c.list_id JOIN boards b ON b.id=l.board_id
               WHERE c.archived=0 AND (c.title LIKE ? OR c.description LIKE ?){pf('b.profile_id')}
               LIMIT 25""",
            (like, like, *prof_params),
        ),
        "docs": many(
            conn,
            f"SELECT id,title,kind,folder FROM docs "
            f"WHERE (title LIKE ? OR body LIKE ?){pf('profile_id')} LIMIT 25",
            (like, like, *prof_params),
        ),
        "events": many(
            conn,
            f"SELECT id,title,start_at,rrule FROM events "
            f"WHERE (title LIKE ? OR description LIKE ?){pf('profile_id')} LIMIT 25",
            (like, like, *prof_params),
        ),
    }
    conn.close()
    return jsonify(result)


# ==================================================================
#                      growth system endpoints
# ==================================================================

@api.route("/xp", methods=["GET"])
@require_token
def get_xp():
    """?weeks=12 - weekly XP history with per-source breakdown."""
    weeks = int(request.args.get("weeks", 12))
    conn = connect()
    data = growth.weekly_xp(conn, weeks, active_profile())
    conn.close()
    return jsonify({
        "weeks": data,
        "current": data[-1] if data else None,
        "rates": {
            "routine": growth.XP_ROUTINE,
            "checklist": growth.XP_CHECKLIST,
            "card": growth.XP_CARD,
            "skill_base": growth.XP_SKILL_BASE,
            "skill_per_level": growth.XP_SKILL_PER_LEVEL,
        },
    })


@api.route("/attributes", methods=["GET"])
@require_token
def get_attributes():
    conn = connect()
    out = {
        "current": growth.attribute_values(conn, profile_id=active_profile()),
        "history": growth.attribute_history(conn, int(request.args.get("weeks", 12)), active_profile()),
    }
    conn.close()
    return jsonify(out)


@api.route("/attributes", methods=["POST"])
@require_token
def create_attribute():
    d = body()
    conn = connect()
    pid = active_profile()
    pos = conn.execute(
        "SELECT COALESCE(MAX(position),-1)+1 FROM attributes WHERE profile_id=?", (pid,)
    ).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO attributes (key,name,color,position,profile_id) VALUES (?,?,?,?,?)",
        (d["key"], d.get("name", d["key"]), _safe_color(d.get("color")),
         d.get("position", pos), pid),
    )
    conn.commit()
    out = one(conn, "SELECT * FROM attributes WHERE id=?", (cur.lastrowid,))
    conn.close()
    return jsonify(out), 201


@api.route("/attributes/<int:aid>", methods=["PATCH", "DELETE"])
@require_token
def modify_attribute(aid):
    conn = connect()
    if request.method == "DELETE":
        conn.execute("DELETE FROM attributes WHERE id=?", (aid,))
        conn.commit()
        conn.close()
        return jsonify({"deleted": aid})
    d = body()
    for f in ("key", "name", "color", "position"):
        if f in d:
            conn.execute(f"UPDATE attributes SET {f}=? WHERE id=?", (d[f], aid))
    conn.commit()
    out = one(conn, "SELECT * FROM attributes WHERE id=?", (aid,))
    conn.close()
    return jsonify(out)


# ------------------------------------------------------------ skill tree

@api.route("/tree", methods=["GET"])
@require_token
def get_tree():
    conn = connect()
    out = growth.load_tree(conn, active_profile())
    conn.close()
    return jsonify(out)


@api.route("/tree/nodes", methods=["POST"])
@require_token
def create_node():
    d = body()
    conn = connect()
    cur = conn.execute(
        """INSERT INTO skill_nodes (title,description,domain,x,y,tier,max_level,
                                    unlock_attr,unlock_value,profile_id)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (d.get("title", "New node"), d.get("description", ""), d.get("domain", "general"),
         d.get("x", 0), d.get("y", 0), _safe_tier(d.get("tier", 1)), d.get("max_level", 5),
         d.get("unlock_attr"), d.get("unlock_value"), active_profile()),
    )
    nid = cur.lastrowid
    for w in d.get("weights", []):
        conn.execute(
            "INSERT OR REPLACE INTO node_weights (node_id,attribute_key,weight) VALUES (?,?,?)",
            (nid, w["attribute_key"], w.get("weight", 1.0)),
        )
    for parent in d.get("parents", []):
        conn.execute("INSERT OR IGNORE INTO skill_edges (from_id,to_id) VALUES (?,?)", (parent, nid))
    conn.commit()
    out = one(conn, "SELECT * FROM skill_nodes WHERE id=?", (nid,))
    conn.close()
    return jsonify(out), 201


@api.route("/tree/nodes/<int:nid>", methods=["PATCH"])
@require_token
def update_node(nid):
    d = body()
    conn = connect()
    for f in ("title", "description", "domain", "x", "y", "tier",
              "max_level", "unlock_attr", "unlock_value"):
        if f in d:
            value = _safe_tier(d[f]) if f == "tier" else d[f]
            conn.execute(f"UPDATE skill_nodes SET {f}=? WHERE id=?", (value, nid))
    if "weights" in d:
        conn.execute("DELETE FROM node_weights WHERE node_id=?", (nid,))
        for w in d["weights"]:
            conn.execute(
                "INSERT INTO node_weights (node_id,attribute_key,weight) VALUES (?,?,?)",
                (nid, w["attribute_key"], w.get("weight", 1.0)),
            )
    conn.commit()
    out = one(conn, "SELECT * FROM skill_nodes WHERE id=?", (nid,))
    conn.close()
    return jsonify(out)


@api.route("/tree/nodes/<int:nid>", methods=["DELETE"])
@require_token
def delete_node(nid):
    conn = connect()
    conn.execute("DELETE FROM skill_nodes WHERE id=?", (nid,))
    conn.commit()
    conn.close()
    return jsonify({"deleted": nid})


@api.route("/tree/edges", methods=["POST"])
@require_token
def create_edge():
    d = body()
    conn = connect()
    conn.execute(
        "INSERT OR IGNORE INTO skill_edges (from_id,to_id) VALUES (?,?)",
        (d["from_id"], d["to_id"]),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True}), 201


@api.route("/tree/edges/<int:eid>", methods=["DELETE"])
@require_token
def delete_edge(eid):
    conn = connect()
    conn.execute("DELETE FROM skill_edges WHERE id=?", (eid,))
    conn.commit()
    conn.close()
    return jsonify({"deleted": eid})


# ----------------------------------------------------- level-up flow

def _notes_problem(conn, doc_id):
    """
    The mechanical half of the notes gate. Returns an error string if the
    doc is missing or obviously too thin to bother the mentor with, else
    None. Substance beyond this floor is the mentor's call.
    """
    if not doc_id:
        return ("Notes come first. Write up what you did in Docs and attach "
                "it - no verification starts without a real writeup.")
    doc = one(conn, "SELECT * FROM docs WHERE id=?", (doc_id,))
    if not doc:
        return "That notes doc doesn't exist."
    text = (doc["body"] or "").strip()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(text) < NOTES_MIN_CHARS or len(lines) < NOTES_MIN_LINES:
        return (f"These notes are too thin to verify against "
                f"({len(text)} chars - the floor is {NOTES_MIN_CHARS}). "
                "One-liners don't count: document what you did, the commands "
                "or techniques involved, and why they worked.")
    return None


@api.route("/tree/nodes/<int:nid>/levelup/preview", methods=["GET"])
@require_token
def levelup_preview(nid):
    """
    What the bar looks like before an attempt is opened: the difficulty
    the mentor will grade at, what kind of proof that difficulty implies,
    and the notes requirement. Strictness as a known bar, not a surprise.
    """
    conn = connect()
    node = one(conn, "SELECT * FROM skill_nodes WHERE id=?", (nid,))
    if not node:
        conn.close()
        abort(404)
    difficulty = growth.verification_difficulty(conn, node)
    fails = conn.execute(
        "SELECT COUNT(*) FROM levelup_attempts WHERE node_id=? AND status='rejected'",
        (nid,),
    ).fetchone()[0]
    conn.close()
    return jsonify({
        "node_id": nid,
        "target_level": node["level"] + 1,
        "difficulty": difficulty,
        "expectations": mentor.DIFFICULTY_GUIDE.get(difficulty, ""),
        "notes_required": True,
        "notes_min_chars": NOTES_MIN_CHARS,
        "rejected_attempts": fails,
    })


@api.route("/tree/nodes/<int:nid>/levelup", methods=["POST"])
@require_token
def request_levelup(nid):
    """
    Start a level-up attempt. This never grants a level - it opens a
    verification handshake, and it refuses to open at all without a notes
    doc of real length attached (body: {"evidence_doc": id}). If direct
    mentor mode is configured the questions come back immediately;
    otherwise the attempt sits in 'awaiting_questions' for an external
    agent to pick up. Optional body field "room_code" ties the attempt to
    a TryHackMe completion.
    """
    d = body()
    conn = connect()
    node = one(conn, "SELECT * FROM skill_nodes WHERE id=?", (nid,))
    if not node:
        conn.close()
        abort(404)
    if node["level"] >= node["max_level"]:
        conn.close()
        return jsonify({"error": "node is already at max level"}), 400

    attrs = growth.attribute_values(conn, profile_id=active_profile())
    if growth.node_locked(node, {a["key"]: a["value"] for a in attrs}):
        conn.close()
        return jsonify({
            "error": f"locked until {node['unlock_attr']} reaches {node['unlock_value']}"
        }), 400

    existing = one(
        conn,
        "SELECT * FROM levelup_attempts WHERE node_id=? AND status IN "
        "('awaiting_questions','awaiting_answer','grading')",
        (nid,),
    )
    if existing:
        conn.close()
        return jsonify(existing)

    # The notes gate. No doc, or a thin one -> no attempt row, no questions.
    evidence_doc = d.get("evidence_doc")
    problem = _notes_problem(conn, evidence_doc)
    if problem:
        conn.close()
        return jsonify({"error": problem, "notes_gate": True}), 400
    notes = one(conn, "SELECT body FROM docs WHERE id=?", (evidence_doc,))["body"]

    difficulty = growth.verification_difficulty(conn, node)
    cur = conn.execute(
        "INSERT INTO levelup_attempts (node_id,target_level,difficulty,evidence_doc,room_code,profile_id) "
        "VALUES (?,?,?,?,?,?)",
        (nid, node["level"] + 1, difficulty, evidence_doc, d.get("room_code"),
         node["profile_id"]),
    )
    aid = cur.lastrowid
    conn.commit()

    context = growth.build_context(conn, node, difficulty)

    if mentor.available():
        try:
            questions = mentor.generate_questions(context, notes)
            conn.execute(
                "UPDATE levelup_attempts SET questions=?, status='awaiting_answer' WHERE id=?",
                (json.dumps(questions), aid),
            )
            conn.commit()
        except Exception as exc:
            # Fall back to queue mode rather than failing the request.
            conn.execute(
                "UPDATE levelup_attempts SET feedback=? WHERE id=?",
                (f"mentor unavailable: {exc}", aid),
            )
            conn.commit()

    out = one(conn, "SELECT * FROM levelup_attempts WHERE id=?", (aid,))
    out["context"] = context
    conn.close()
    return jsonify(out), 201


@api.route("/attempts", methods=["GET"])
@require_token
def list_attempts():
    """?status=awaiting_questions - what an external mentor should work on."""
    conn = connect()
    sql = ("SELECT a.*, n.title AS node_title, n.domain, n.tier "
           "FROM levelup_attempts a JOIN skill_nodes n ON n.id=a.node_id "
           "WHERE a.profile_id=?")
    params = [active_profile()]
    # An agent working the queue can ask for every profile's attempts at once.
    if request.args.get("scope") == "all":
        sql = ("SELECT a.*, n.title AS node_title, n.domain, n.tier "
               "FROM levelup_attempts a JOIN skill_nodes n ON n.id=a.node_id WHERE 1=1")
        params = []
    if "status" in request.args:
        sql += " AND a.status=?"
        params.append(request.args["status"])
    sql += " ORDER BY a.created_at DESC LIMIT 50"
    rows = many(conn, sql, params)
    for r in rows:
        r["questions"] = json.loads(r["questions"] or "[]")
        r["answers"] = json.loads(r["answers"] or "[]")
    conn.close()
    return jsonify(rows)


@api.route("/attempts/<int:aid>", methods=["GET"])
@require_token
def get_attempt(aid):
    conn = connect()
    row = one(conn, "SELECT * FROM levelup_attempts WHERE id=?", (aid,))
    if not row:
        conn.close()
        abort(404)
    node = one(conn, "SELECT * FROM skill_nodes WHERE id=?", (row["node_id"],))
    row["questions"] = json.loads(row["questions"] or "[]")
    row["answers"] = json.loads(row["answers"] or "[]")
    row["context"] = growth.build_context(conn, node, row["difficulty"])
    conn.close()
    return jsonify(row)


@api.route("/attempts/<int:aid>/questions", methods=["POST"])
@require_token
def post_questions(aid):
    """Queue mode: an external mentor supplies the questions."""
    d = body()
    conn = connect()
    conn.execute(
        "UPDATE levelup_attempts SET questions=?, status='awaiting_answer' WHERE id=?",
        (json.dumps(d.get("questions", [])), aid),
    )
    conn.commit()
    out = one(conn, "SELECT * FROM levelup_attempts WHERE id=?", (aid,))
    conn.close()
    return jsonify(out)


@api.route("/attempts/<int:aid>/answer", methods=["POST"])
@require_token
def answer_attempt(aid):
    """
    Submit answers. In direct mode the mentor grades immediately; otherwise
    the attempt moves to 'grading' for an external agent to judge.
    """
    d = body()
    conn = connect()
    attempt = one(conn, "SELECT * FROM levelup_attempts WHERE id=?", (aid,))
    if not attempt:
        conn.close()
        abort(404)

    answers = d.get("answers", [])
    # Notes are attached when the attempt opens; the body field remains as a
    # queue-mode override for older clients.
    evidence_doc = d.get("evidence_doc") or attempt["evidence_doc"]
    conn.execute(
        "UPDATE levelup_attempts SET answers=?, evidence_doc=?, status='grading' WHERE id=?",
        (json.dumps(answers), evidence_doc, aid),
    )
    conn.commit()

    if not mentor.available():
        out = one(conn, "SELECT * FROM levelup_attempts WHERE id=?", (aid,))
        conn.close()
        return jsonify(out)

    node = one(conn, "SELECT * FROM skill_nodes WHERE id=?", (attempt["node_id"],))
    context = growth.build_context(conn, node, attempt["difficulty"])
    evidence = None
    if evidence_doc:
        doc = one(conn, "SELECT body FROM docs WHERE id=?", (evidence_doc,))
        evidence = doc["body"] if doc else None

    try:
        verdict = mentor.grade_answers(
            context, json.loads(attempt["questions"] or "[]"), answers, evidence
        )
    except Exception as exc:
        conn.close()
        return jsonify({"error": f"grading failed: {exc}", "status": "grading"}), 502

    result = _resolve_attempt(conn, aid, verdict)
    conn.close()
    return jsonify(result)


@api.route("/attempts/<int:aid>/verdict", methods=["POST"])
@require_token
def post_verdict(aid):
    """
    Queue mode: an external mentor grants or refuses.
    Body: {"granted": bool, "feedback": "...", "suggested_nodes": [...]}
    """
    conn = connect()
    result = _resolve_attempt(conn, aid, body())
    conn.close()
    return jsonify(result)


def _resolve_attempt(conn, aid, verdict):
    """Shared tail for both modes: grant or refuse, then report what changed."""
    attempt = one(conn, "SELECT * FROM levelup_attempts WHERE id=?", (aid,))
    granted = bool(verdict.get("granted"))
    feedback = verdict.get("feedback", "")

    # Attribute movement is measured against the attempt's own owner, not
    # whoever happens to be the active profile on this request - an agent
    # can grade someone else's attempt.
    owner_pid = attempt["profile_id"] if attempt else "primary"

    before = growth.attribute_values(conn, profile_id=owner_pid)
    new_level = None
    unlocked = []

    if granted:
        new_level = growth.grant_level(conn, attempt["node_id"], aid)
        conn.commit()
        after = growth.attribute_values(conn, profile_id=owner_pid)
        unlocked = growth.newly_unlocked(conn, before, after, owner_pid)
        # A verified level changes the gap picture, so the stored TryHackMe
        # recommendation is stale. Flag it rather than regenerating inline -
        # grading shouldn't wait on a second model call.
        _set_setting(conn, "thm_recommendation_stale", "1")
    else:
        after = before

    conn.execute(
        "UPDATE levelup_attempts SET status=?, feedback=?, resolved_at=datetime('now') WHERE id=?",
        ("granted" if granted else "rejected", feedback, aid),
    )

    # Suggested follow-up nodes become a proposal, not a silent write.
    proposal_id = None
    suggestions = verdict.get("suggested_nodes") or []
    if granted and suggestions:
        node = one(conn, "SELECT * FROM skill_nodes WHERE id=?", (attempt["node_id"],))
        actions = []
        for i, s in enumerate(suggestions[:3]):
            actions.append({
                "op": "create_node",
                "title": s.get("title", "New node"),
                "description": s.get("rationale", ""),
                "domain": s.get("domain", node["domain"]),
                "tier": s.get("tier", node["tier"]),
                "x": node["x"] + 120 + i * 60,
                "y": node["y"] + 90 + i * 40,
                "weights": s.get("weights", []),
                "parents": [node["id"]],
            })
        cur = conn.execute(
            "INSERT INTO ai_proposals (kind,title,rationale,actions,profile_id) VALUES (?,?,?,?,?)",
            ("tree_expansion",
             f"New nodes after {node['title']}",
             "; ".join(s.get("rationale", "") for s in suggestions[:3]),
             json.dumps(actions)),
        )
        proposal_id = cur.lastrowid

    conn.commit()
    return {
        "attempt_id": aid,
        "granted": granted,
        "feedback": feedback,
        "new_level": new_level,
        "node_id": attempt["node_id"],
        "unlocked_nodes": unlocked,
        "proposal_id": proposal_id,
        "attributes": after,
    }


@api.route("/attempts/<int:aid>", methods=["DELETE"])
@require_token
def cancel_attempt(aid):
    conn = connect()
    conn.execute(
        "UPDATE levelup_attempts SET status='cancelled', resolved_at=datetime('now') WHERE id=?",
        (aid,),
    )
    conn.commit()
    conn.close()
    return jsonify({"cancelled": aid})


# -------------------------------------------------------- TryHackMe

def _setting(conn, key, default=""):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key,value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _thm_completions(conn):
    rows = many(
        conn,
        "SELECT c.*, r.title, r.difficulty, r.tags "
        "FROM thm_completions c JOIN thm_rooms r ON r.code = c.room_code "
        "WHERE c.profile_id = ? "
        "ORDER BY c.local_date DESC, c.id DESC",
        (active_profile(),),
    )
    for r in rows:
        r["tags"] = json.loads(r["tags"] or "[]")
        r["nodes"] = many(
            conn,
            "SELECT n.id, n.title, n.level, n.max_level, n.domain, "
            "  (SELECT a.id FROM levelup_attempts a WHERE a.node_id=n.id AND a.status IN "
            "   ('awaiting_questions','awaiting_answer','grading') LIMIT 1) AS pending_attempt, "
            "  EXISTS(SELECT 1 FROM levelup_attempts a WHERE a.node_id=n.id "
            "         AND a.room_code=? AND a.status='granted') AS verified "
            "FROM thm_room_nodes rn JOIN skill_nodes n ON n.id = rn.node_id "
            "WHERE rn.room_code=?",
            (r["room_code"], r["room_code"]),
        )
    return rows


@api.route("/thm", methods=["GET"])
@require_token
def thm_overview():
    """Username, every logged completion (with node mappings and their
    verification state), and the last stored recommendation."""
    conn = connect()
    rec_raw = _setting(conn, "thm_recommendation", "")
    out = {
        "username": _setting(conn, "thm_username"),
        "completions": _thm_completions(conn),
        "recommendation": json.loads(rec_raw) if rec_raw else None,
        "recommendation_stale": _setting(conn, "thm_recommendation_stale") == "1",
        "direct_mode": mentor.available(),
    }
    conn.close()
    return jsonify(out)


@api.route("/thm/settings", methods=["PATCH"])
@require_token
def thm_settings():
    d = body()
    conn = connect()
    if "username" in d:
        _set_setting(conn, "thm_username", (d["username"] or "").strip())
    conn.commit()
    username = _setting(conn, "thm_username")
    conn.close()
    return jsonify({"username": username})


@api.route("/thm/sync", methods=["POST"])
@require_token
def thm_sync():
    """
    Best-effort pull of completed rooms from the public profile. TryHackMe
    has no official personal API, so this scrapes unofficial endpoints and
    degrades gracefully: if they're unreachable or have changed shape, the
    response says so and manual logging still works.
    """
    conn = connect()
    username = _setting(conn, "thm_username")
    if not username:
        conn.close()
        return jsonify({"error": "set a TryHackMe username first"}), 400

    codes = thm.fetch_completed_codes(username)
    if codes is None:
        conn.close()
        return jsonify({
            "synced": False,
            "added": 0,
            "note": "TryHackMe's unofficial endpoints didn't respond - they may "
                    "have changed again. Log completions manually; nothing else "
                    "is affected.",
        })

    known = {r["room_code"] for r in conn.execute("SELECT room_code FROM thm_completions")}
    new_codes = [c for c in codes if c not in known]
    meta = thm.fetch_room_meta(new_codes) if new_codes else {}

    today = today_local().isoformat()
    for code in new_codes:
        m = meta.get(code, {})
        conn.execute(
            "INSERT INTO thm_rooms (code,title,difficulty,tags,description,fetched_at) "
            "VALUES (?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(code) DO UPDATE SET title=excluded.title, "
            "  difficulty=excluded.difficulty, tags=excluded.tags, "
            "  description=excluded.description, fetched_at=excluded.fetched_at",
            (code, m.get("title", code), m.get("difficulty", ""),
             json.dumps(m.get("tags", [])), m.get("description", "")),
        )
        # Synced completions land on today's date - the profile doesn't say
        # when a room was finished, only that it was.
        conn.execute(
            "INSERT OR IGNORE INTO thm_completions (room_code, local_date, source, profile_id) "
            "VALUES (?,?, 'sync', ?)",
            (code, today, active_profile()),
        )
    conn.commit()
    out = {"synced": True, "added": len(new_codes), "total_on_profile": len(codes)}
    conn.close()
    return jsonify(out)


@api.route("/thm/completions", methods=["POST"])
@require_token
def thm_log_completion():
    """
    Manual completion logging - the reliable path. Body:
    {"room_code": "vulnversity", "title": "...", "date": "YYYY-MM-DD"}.
    Metadata is fetched best-effort if not supplied. Carries no XP: it
    surfaces the mapped nodes so verification can start, nothing more.
    """
    d = body()
    code = (d.get("room_code") or "").strip().strip("/").split("/")[-1]
    if not code:
        return jsonify({"error": "room_code is required (the room's URL slug)"}), 400

    conn = connect()
    existing_room = one(conn, "SELECT * FROM thm_rooms WHERE code=?", (code,))
    title = d.get("title", "").strip()
    if not existing_room:
        meta = thm.fetch_room_meta([code]).get(code, {})
        conn.execute(
            "INSERT INTO thm_rooms (code,title,difficulty,tags,description,fetched_at) "
            "VALUES (?,?,?,?,?,datetime('now'))",
            (code, title or meta.get("title", code), meta.get("difficulty", ""),
             json.dumps(meta.get("tags", [])), meta.get("description", "")),
        )
    elif title:
        conn.execute("UPDATE thm_rooms SET title=? WHERE code=?", (title, code))

    date = d.get("date") or today_local().isoformat()
    try:
        conn.execute(
            "INSERT INTO thm_completions (room_code, local_date, source, profile_id) VALUES (?,?, 'manual', ?)",
            (code, date, active_profile()),
        )
    except Exception:
        conn.close()
        return jsonify({"error": f"'{code}' is already logged as completed"}), 400

    conn.commit()
    rows = [c for c in _thm_completions(conn) if c["room_code"] == code]
    conn.close()
    return jsonify(rows[0] if rows else {"room_code": code}), 201


@api.route("/thm/completions/<int:cid>", methods=["DELETE"])
@require_token
def thm_delete_completion(cid):
    conn = connect()
    conn.execute("DELETE FROM thm_completions WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    return jsonify({"deleted": cid})


@api.route("/thm/rooms/<code>/nodes", methods=["POST", "DELETE"])
@require_token
def thm_map_room(code):
    """Map (or unmap) a room to a skill-tree node. Body: {"node_id": N}."""
    d = body()
    nid = d.get("node_id")
    conn = connect()
    if not one(conn, "SELECT code FROM thm_rooms WHERE code=?", (code,)):
        conn.close()
        return jsonify({"error": "unknown room - log or sync it first"}), 404
    if request.method == "POST":
        if not one(conn, "SELECT id FROM skill_nodes WHERE id=?", (nid,)):
            conn.close()
            return jsonify({"error": "no such node"}), 404
        conn.execute(
            "INSERT OR IGNORE INTO thm_room_nodes (room_code, node_id) VALUES (?,?)",
            (code, nid),
        )
    else:
        conn.execute(
            "DELETE FROM thm_room_nodes WHERE room_code=? AND node_id=?", (code, nid)
        )
    conn.commit()
    nodes = many(
        conn,
        "SELECT n.id, n.title FROM thm_room_nodes rn "
        "JOIN skill_nodes n ON n.id=rn.node_id WHERE rn.room_code=?",
        (code,),
    )
    conn.close()
    return jsonify({"room_code": code, "nodes": nodes})


@api.route("/thm/recommend", methods=["GET", "POST"])
@require_token
def thm_recommend():
    """
    GET returns the stored recommendation plus the context an external
    agent would need to produce one (queue mode). POST refreshes it: in
    direct mode the mentor generates it now; in queue mode, POST a body of
    {"summary": ..., "recommendations": [...]} to store one produced
    elsewhere (e.g. by Claude Code).
    """
    conn = connect()
    context = thm.build_recommend_context(conn, growth)

    if request.method == "GET":
        raw = _setting(conn, "thm_recommendation", "")
        conn.close()
        return jsonify({
            "recommendation": json.loads(raw) if raw else None,
            "context": context,
            "direct_mode": mentor.available(),
        })

    d = body()
    if d.get("recommendations"):
        rec = {"summary": d.get("summary", ""),
               "recommendations": d["recommendations"],
               "generated_at": datetime.utcnow().isoformat() + "Z",
               "source": "queue"}
    elif mentor.available():
        try:
            out = mentor.recommend_room(context)
        except Exception as exc:
            conn.close()
            return jsonify({"error": f"recommendation failed: {exc}"}), 502
        rec = {**out, "generated_at": datetime.utcnow().isoformat() + "Z",
               "source": "mentor"}
    else:
        conn.close()
        return jsonify({
            "error": "no ANTHROPIC_API_KEY - POST a recommendation body from an "
                     "external agent, or use GET for the context it needs"
        }), 400

    _set_setting(conn, "thm_recommendation", json.dumps(rec))
    _set_setting(conn, "thm_recommendation_stale", "0")
    conn.commit()
    conn.close()
    return jsonify({"recommendation": rec})


# -------------------------------------------------------- proposals

@api.route("/proposals", methods=["GET"])
@require_token
def list_proposals():
    conn = connect()
    status = request.args.get("status", "pending")
    rows = many(
        conn,
        "SELECT * FROM ai_proposals WHERE status=? AND profile_id=? ORDER BY created_at DESC LIMIT 50",
        (status, active_profile()),
    )
    for r in rows:
        r["actions"] = json.loads(r["actions"] or "[]")
    conn.close()
    return jsonify(rows)


@api.route("/proposals", methods=["POST"])
@require_token
def create_proposal():
    """
    The AI files a change here rather than applying it. Nothing happens
    until the user approves - autonomy without an approval loop is how an
    app starts feeling like it has a mind of its own.
    """
    d = body()
    conn = connect()
    cur = conn.execute(
        "INSERT INTO ai_proposals (kind,title,rationale,actions) VALUES (?,?,?,?)",
        (d.get("kind", "general"), d.get("title", "Proposed change"),
         d.get("rationale", ""), json.dumps(d.get("actions", []))),
    )
    conn.commit()
    out = one(conn, "SELECT * FROM ai_proposals WHERE id=?", (cur.lastrowid,))
    out["actions"] = json.loads(out["actions"] or "[]")
    conn.close()
    return jsonify(out), 201


@api.route("/proposals/<int:pid>/approve", methods=["POST"])
@require_token
def approve_proposal(pid):
    conn = connect()
    prop = one(conn, "SELECT * FROM ai_proposals WHERE id=?", (pid,))
    if not prop:
        conn.close()
        abort(404)
    if prop["status"] != "pending":
        conn.close()
        return jsonify({"error": f"already {prop['status']}"}), 400

    result = mentor.apply_proposal(conn, prop)
    conn.execute(
        "UPDATE ai_proposals SET status='approved', resolved_at=datetime('now') WHERE id=?",
        (pid,),
    )
    conn.commit()
    conn.close()
    return jsonify({"proposal_id": pid, **result})


@api.route("/proposals/<int:pid>/reject", methods=["POST"])
@require_token
def reject_proposal(pid):
    conn = connect()
    conn.execute(
        "UPDATE ai_proposals SET status='rejected', resolved_at=datetime('now') WHERE id=?",
        (pid,),
    )
    conn.commit()
    conn.close()
    return jsonify({"rejected": pid})


@api.route("/mentor/status", methods=["GET"])
@require_token
def mentor_status():
    """Whether direct inference is configured, and what's waiting."""
    conn = connect()
    out = {
        "direct_mode": mentor.available(),
        "model": mentor.MODEL if mentor.available() else None,
        "pending_attempts": conn.execute(
            "SELECT COUNT(*) FROM levelup_attempts WHERE profile_id=? AND status IN "
            "('awaiting_questions','awaiting_answer','grading')", (active_profile(),)
        ).fetchone()[0],
        "pending_proposals": conn.execute(
            "SELECT COUNT(*) FROM ai_proposals WHERE status='pending' AND profile_id=?", (active_profile(),)
        ).fetchone()[0],
    }
    conn.close()
    return jsonify(out)


@api.route("/context", methods=["GET"])
@require_token
def full_context():
    """
    One call returning everything an external agent needs to reason about
    the user's state: boards, tree, attributes, XP trend, routines, docs
    index. Saves an agent five round trips before it can say anything useful.
    """
    conn = connect()
    pid = active_profile()
    boards_data = []
    # Every one of these is profile-scoped. They were not before v6/v7, which
    # meant an agent asking for context got all three profiles' boards,
    # routines and docs blended into one list with no way to tell them apart.
    for b in many(conn, "SELECT * FROM boards WHERE archived=0 AND profile_id=? "
                        "ORDER BY position, id", (pid,)):
        b["lists"] = many(
            conn, "SELECT * FROM lists WHERE board_id=? AND archived=0 ORDER BY position, id",
            (b["id"],),
        )
        for lst in b["lists"]:
            lst["cards"] = load_cards(conn, lst["id"])
        boards_data.append(b)

    tree = growth.load_tree(conn, pid)
    xp = growth.weekly_xp(conn, 8, pid)
    routines = many(conn, "SELECT * FROM routines WHERE active=1 AND profile_id=? "
                          "ORDER BY time_group, position", (pid,))
    docs_index = many(conn, "SELECT id,title,kind,folder,updated_at FROM docs "
                            "WHERE profile_id=? ORDER BY updated_at DESC LIMIT 50", (pid,))
    health_block = {
        "summary": health.summary(conn, pid, 7),
        "stats_30d": {m: health.stats(conn, pid, m, 30)
                      for m in health.tracked_metrics(conn, pid)},
    }

    conn.close()
    return jsonify({
        "boards": boards_data,
        "tree": tree,
        "attributes": tree["attributes"],
        "xp_recent": xp,
        "routines": routines,
        "docs": docs_index,
        "health": health_block,
        "profile_id": pid,
    })
