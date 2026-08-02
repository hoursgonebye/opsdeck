"""
TryHackMe integration.

TryHackMe has no official personal-stats API - their official API is for
Business/Classroom instructors. What exists instead is a set of unofficial
endpoints that back the public profile page. They are undocumented, have
been reorganised before (v1 -> v2), and can change again without notice.

So everything here is BEST-EFFORT by design:

  - Each fetch tries a short list of known endpoint shapes and returns
    None (never raises) when all of them miss. A sync that can't reach
    TryHackMe degrades to "nothing new", not to an error page.
  - Manual completion logging in the API is the reliable path and works
    with zero network access. Sync is a convenience on top of it.
  - If TryHackMe moves things again, ENDPOINTS below is the only place
    to update.

None of this touches XP or the tree directly. A completion (synced or
manual) only ever *offers* verification on the mapped nodes - the strict
mentor flow decides whether anything is actually earned.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 12
USER_AGENT = "OpsDeck/1.0 (personal dashboard; single user)"

# Known unofficial endpoint shapes, newest first. {u} = username, {c} = codes.
ENDPOINTS = {
    "completed_rooms": [
        "https://tryhackme.com/api/v2/public-profile/completed-rooms?user={u}&limit=200",
        "https://tryhackme.com/api/all-completed-rooms?username={u}&limit=200",
    ],
    "room_details": [
        "https://tryhackme.com/api/v2/rooms/details?codes={c}",
        "https://tryhackme.com/api/room/details?codes={c}",
    ],
    "user_exists": [
        "https://tryhackme.com/api/v2/public-profile?user={u}",
        "https://tryhackme.com/api/user/exists/{u}",
    ],
}


def _get_json(url):
    """One GET, parsed as JSON. Returns None on any failure."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            return json.loads(res.read().decode())
    except Exception:
        return None


def _first_hit(kind, **kw):
    """Try each endpoint shape for `kind` until one returns something."""
    for tmpl in ENDPOINTS[kind]:
        data = _get_json(tmpl.format(**{k: urllib.parse.quote(str(v)) for k, v in kw.items()}))
        if data:
            return data
    return None


def _walk_rooms(data):
    """
    Pull room entries out of whatever wrapper the endpoint used.
    Accepts a bare list, or a dict with the list under data/rooms/docs.
    """
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "rooms", "docs", "completedRooms"):
            inner = data.get(key)
            if isinstance(inner, list):
                return inner
            if isinstance(inner, dict):  # one more level (v2 wraps twice)
                got = _walk_rooms(inner)
                if got:
                    return got
    return []


def _room_code(entry):
    for key in ("code", "roomCode", "room_code", "slug"):
        if isinstance(entry, dict) and entry.get(key):
            return str(entry[key])
    if isinstance(entry, str):
        return entry
    return None


def fetch_completed_codes(username):
    """
    Room codes from the user's public profile.
    Returns a list (possibly empty), or None if TryHackMe was unreachable /
    the endpoints have changed - the caller should treat None as "sync
    unavailable, fall back to manual logging", not as "zero rooms".
    """
    data = _first_hit("completed_rooms", u=username)
    if data is None:
        return None
    codes = []
    for entry in _walk_rooms(data):
        code = _room_code(entry)
        if code and code not in codes:
            codes.append(code)
    return codes


def fetch_room_meta(codes):
    """
    Metadata for up to ~20 room codes at a time.
    Returns {code: {title, difficulty, tags, description}} for whatever was
    found; missing rooms are simply absent. Never raises.
    """
    out = {}
    for i in range(0, len(codes), 20):
        chunk = codes[i:i + 20]
        data = _first_hit("room_details", c=",".join(chunk))
        if not data:
            continue
        entries = data.get("data", data) if isinstance(data, dict) else data
        if isinstance(entries, dict):
            entries = list(entries.values())
        for e in entries or []:
            if not isinstance(e, dict):
                continue
            code = _room_code(e)
            if not code:
                continue
            out[code] = {
                "title": e.get("title") or code,
                "difficulty": e.get("difficulty", ""),
                "tags": [t for t in (e.get("tags") or []) if isinstance(t, str)],
                "description": e.get("description", ""),
            }
    return out


def user_exists(username):
    """Loose existence check; None means 'could not check'."""
    data = _first_hit("user_exists", u=username)
    if data is None:
        return None
    if isinstance(data, dict):
        if "success" in data:
            return bool(data["success"])
        return bool(data)
    return bool(data)


# ------------------------------------------------ recommendation context

def build_recommend_context(conn, growth):
    """
    Everything the mentor needs to point the user at their next room:
    attribute shape (with which are weakest and which have gone stale),
    the tree with levels, and every room already completed so it never
    recommends redundant material.
    """
    attrs = growth.attribute_values(conn)

    # Staleness: attributes with no ledger contribution in the last 28 days.
    recent = set()
    for r in conn.execute(
        "SELECT DISTINCT w.attribute_key FROM skill_levels sl "
        "JOIN node_weights w ON w.node_id = sl.node_id "
        "WHERE sl.local_date >= date('now', '-28 days')"
    ):
        recent.add(r["attribute_key"])

    nodes = [
        {"id": r["id"], "title": r["title"], "domain": r["domain"],
         "tier": r["tier"], "level": r["level"], "max_level": r["max_level"]}
        for r in conn.execute("SELECT * FROM skill_nodes ORDER BY domain, tier")
    ]

    completed = [
        {"code": r["room_code"], "title": r["title"], "tags": json.loads(r["tags"] or "[]"),
         "date": r["local_date"]}
        for r in conn.execute(
            "SELECT c.room_code, c.local_date, r.title, r.tags "
            "FROM thm_completions c JOIN thm_rooms r ON r.code = c.room_code "
            "ORDER BY c.local_date DESC"
        )
    ]

    ranked = sorted(attrs, key=lambda a: a["value"])
    return {
        "attributes": attrs,
        "weakest_attributes": [a["key"] for a in ranked[:3]],
        "stale_attributes": [a["key"] for a in attrs if a["key"] not in recent],
        "tree_nodes": nodes,
        "completed_rooms": completed,
    }
