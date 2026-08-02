"""
XP, attributes, and the skill tree's derived state.

Nothing here is a stored counter. Every number is computed from timestamped
source rows: routine_completions, card/checklist completion times, and the
skill_levels ledger. That means:

  - editing a node's attribute weights retroactively corrects history
    (which is what you want - the weights are a model of reality, and
    fixing the model should fix the picture)
  - there is no counter to drift, repair, or migrate
  - "what did week X look like" is a query, not an archived snapshot

The cost is that a very long history eventually makes these queries slower.
At one user writing a few rows a day, that's years away.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta

from db import connect
from recurrence import today_local

# XP weights. Deliberately modest for routine work and steep for verified
# skill levels - the point of the system is to make real skill growth the
# thing that moves the number, not checkbox volume.
XP_ROUTINE = 5
XP_CHECKLIST = 3
XP_CARD = 15
XP_SKILL_BASE = 60      # multiplied by node tier
XP_SKILL_PER_LEVEL = 20  # extra per level already held, so depth pays more


def week_start(d):
    """Monday of the week containing d. Accepts date or ISO string."""
    if isinstance(d, str):
        d = date.fromisoformat(d[:10])
    return d - timedelta(days=d.weekday())


def skill_xp(tier, level):
    """XP for reaching `level` on a node of `tier`."""
    return XP_SKILL_BASE * tier + XP_SKILL_PER_LEVEL * (level - 1)


# ------------------------------------------------------------------ XP

def xp_for_range(conn, start_iso, end_iso, profile_id="primary"):
    """
    XP earned in [start, end). Returns a per-source breakdown so you can see
    what actually drove a good week rather than just that it was good.

    Scoped per profile (v6): routines and boards join up to their owner, and
    skill levels join through skill_nodes.
    """
    routines = conn.execute(
        "SELECT COUNT(*) FROM routine_completions rc JOIN routines r ON r.id = rc.routine_id "
        "WHERE rc.local_date >= ? AND rc.local_date < ? AND r.profile_id = ?",
        (start_iso, end_iso, profile_id),
    ).fetchone()[0]

    cards = conn.execute(
        "SELECT COUNT(*) FROM cards c "
        "JOIN lists l ON l.id = c.list_id JOIN boards b ON b.id = l.board_id "
        "WHERE c.completed = 1 AND c.completed_at IS NOT NULL "
        "AND date(c.completed_at) >= ? AND date(c.completed_at) < ? AND b.profile_id = ?",
        (start_iso, end_iso, profile_id),
    ).fetchone()[0]

    checklist = conn.execute(
        "SELECT COUNT(*) FROM checklist_items ci JOIN cards c ON c.id = ci.card_id "
        "JOIN lists l ON l.id = c.list_id JOIN boards b ON b.id = l.board_id "
        "WHERE ci.done = 1 AND ci.done_at IS NOT NULL "
        "AND date(ci.done_at) >= ? AND date(ci.done_at) < ? AND b.profile_id = ?",
        (start_iso, end_iso, profile_id),
    ).fetchone()[0]

    skill_rows = conn.execute(
        "SELECT sl.level, n.tier FROM skill_levels sl "
        "JOIN skill_nodes n ON n.id = sl.node_id "
        "WHERE sl.local_date >= ? AND sl.local_date < ? AND n.profile_id = ?",
        (start_iso, end_iso, profile_id),
    ).fetchall()
    skills_xp = sum(skill_xp(r["tier"], r["level"]) for r in skill_rows)

    sources = {
        "routines": routines * XP_ROUTINE,
        "cards": cards * XP_CARD,
        "checklist": checklist * XP_CHECKLIST,
        "skills": skills_xp,
    }
    return {
        "sources": sources,
        "counts": {
            "routines": routines,
            "cards": cards,
            "checklist": checklist,
            "skills": len(skill_rows),
        },
        "total": sum(sources.values()),
    }


def weekly_xp(conn, weeks=12, profile_id="primary"):
    """
    The last `weeks` weeks, oldest first, each with a source breakdown and a
    trailing average so one bad week doesn't read as collapse.
    """
    this_monday = week_start(today_local())
    out = []
    for i in range(weeks - 1, -1, -1):
        start = this_monday - timedelta(weeks=i)
        end = start + timedelta(days=7)
        row = xp_for_range(conn, start.isoformat(), end.isoformat(), profile_id)
        row["week_start"] = start.isoformat()
        row["is_current"] = i == 0
        out.append(row)

    # Trailing average over completed weeks only - including the in-progress
    # week would drag the baseline down every Monday morning.
    for idx, row in enumerate(out):
        prior = [r["total"] for r in out[max(0, idx - 4):idx]]
        row["trailing_avg"] = round(sum(prior) / len(prior)) if prior else None
        if row["trailing_avg"]:
            row["delta_pct"] = round((row["total"] - row["trailing_avg"]) / row["trailing_avg"] * 100)
        else:
            row["delta_pct"] = None
    return out


# ------------------------------------------------------- attributes

def attribute_values(conn, as_of=None, profile_id="primary"):
    """
    Current (or historical) attribute totals.

    A node at level 3 with weight 0.7 toward Pentest has contributed three
    ledger rows, each worth 0.7 x tier-scale. Passing `as_of` simply ignores
    ledger rows after that date, which is the whole history mechanism.
    """
    sql = (
        "SELECT sl.node_id, sl.level, n.tier, w.attribute_key, w.weight "
        "FROM skill_levels sl "
        "JOIN skill_nodes n ON n.id = sl.node_id "
        "JOIN node_weights w ON w.node_id = sl.node_id "
        "WHERE n.profile_id = ?"
    )
    params = [profile_id]
    if as_of:
        sql += " AND sl.local_date <= ?"
        params.append(as_of)

    totals = defaultdict(float)
    for r in conn.execute(sql, params):
        # Deeper nodes are worth more per level than foundational ones.
        totals[r["attribute_key"]] += r["weight"] * (1 + 0.5 * (r["tier"] - 1))

    attrs = []
    for a in conn.execute(
        "SELECT * FROM attributes WHERE profile_id=? ORDER BY position, id", (profile_id,)
    ):
        attrs.append({
            "key": a["key"],
            "name": a["name"],
            "color": a["color"],
            "value": round(totals.get(a["key"], 0.0), 2),
        })
    return attrs


def attribute_history(conn, weeks=12, profile_id="primary"):
    """Attribute shape at the end of each of the last `weeks` weeks."""
    this_monday = week_start(today_local())
    out = []
    for i in range(weeks - 1, -1, -1):
        start = this_monday - timedelta(weeks=i)
        as_of = (start + timedelta(days=6)).isoformat()
        out.append({
            "week_start": start.isoformat(),
            "attributes": {a["key"]: a["value"]
                           for a in attribute_values(conn, as_of, profile_id)},
        })
    return out


# ------------------------------------------------------- skill tree

def node_locked(node, attr_map):
    """A node is locked until its gating attribute crosses the threshold."""
    if not node.get("unlock_attr") or node.get("unlock_value") is None:
        return False
    return attr_map.get(node["unlock_attr"], 0.0) < node["unlock_value"]


def load_tree(conn, profile_id="primary"):
    """
    The full tree with derived state: per-node weights, lock status, and
    whether each node is currently mid-attempt. Scoped to one profile - each
    person's tree, attributes and attempt queue are their own.
    """
    attrs = attribute_values(conn, profile_id=profile_id)
    attr_map = {a["key"]: a["value"] for a in attrs}

    weights = defaultdict(list)
    for r in conn.execute(
        "SELECT w.* FROM node_weights w JOIN skill_nodes n ON n.id = w.node_id "
        "WHERE n.profile_id = ?", (profile_id,)
    ):
        weights[r["node_id"]].append({"attribute_key": r["attribute_key"], "weight": r["weight"]})

    pending = {
        r["node_id"]: r["id"]
        for r in conn.execute(
            "SELECT id, node_id FROM levelup_attempts "
            "WHERE status IN ('awaiting_questions','awaiting_answer','grading') "
            "AND profile_id = ?", (profile_id,)
        )
    }

    # Failed attempts are part of the record, not something to hide - the
    # system is built so "not yet" is a real outcome.
    rejections = {
        r["node_id"]: r["c"]
        for r in conn.execute(
            "SELECT node_id, COUNT(*) AS c FROM levelup_attempts "
            "WHERE status='rejected' AND profile_id=? GROUP BY node_id", (profile_id,)
        )
    }

    nodes = []
    for r in conn.execute(
        "SELECT * FROM skill_nodes WHERE profile_id=? ORDER BY id", (profile_id,)
    ):
        n = dict(r)
        n["weights"] = weights.get(n["id"], [])
        n["locked"] = node_locked(n, attr_map)
        n["pending_attempt"] = pending.get(n["id"])
        n["rejected_attempts"] = rejections.get(n["id"], 0)
        n["xp_next"] = skill_xp(n["tier"], n["level"] + 1) if n["level"] < n["max_level"] else None
        nodes.append(n)

    # Edges inherit scope from their endpoints: only keep an edge if both
    # ends belong to this profile.
    edges = [dict(r) for r in conn.execute(
        "SELECT e.* FROM skill_edges e "
        "JOIN skill_nodes a ON a.id = e.from_id "
        "JOIN skill_nodes b ON b.id = e.to_id "
        "WHERE a.profile_id = ? AND b.profile_id = ?", (profile_id, profile_id)
    )]

    return {
        "nodes": nodes,
        "edges": edges,
        "attributes": attrs,
        "totals": {
            "nodes": len(nodes),
            "levels": sum(n["level"] for n in nodes),
            "max_levels": sum(n["max_level"] for n in nodes),
        },
    }


def grant_level(conn, node_id, attempt_id=None):
    """
    Write one level to the ledger and bump the node. Returns the new level,
    or None if the node is already maxed.
    """
    node = conn.execute("SELECT * FROM skill_nodes WHERE id=?", (node_id,)).fetchone()
    if not node or node["level"] >= node["max_level"]:
        return None

    new_level = node["level"] + 1
    conn.execute(
        "INSERT INTO skill_levels (node_id, level, attempt_id, local_date) VALUES (?,?,?,?)",
        (node_id, new_level, attempt_id, today_local().isoformat()),
    )
    conn.execute("UPDATE skill_nodes SET level=? WHERE id=?", (new_level, node_id))
    return new_level


def newly_unlocked(conn, before_attrs, after_attrs, profile_id="primary"):
    """Nodes that crossed their unlock threshold between two attribute states."""
    before = {a["key"]: a["value"] for a in before_attrs}
    after = {a["key"]: a["value"] for a in after_attrs}
    out = []
    for r in conn.execute(
        "SELECT * FROM skill_nodes WHERE unlock_attr IS NOT NULL "
        "AND unlock_value IS NOT NULL AND profile_id = ?", (profile_id,)
    ):
        n = dict(r)
        if node_locked(n, before) and not node_locked(n, after):
            out.append({"id": n["id"], "title": n["title"], "domain": n["domain"]})
    return out


# ------------------------------------------------------ verification

def verification_difficulty(conn, node):
    """
    How hard the mentor should push, 1-5.

    Scales with both tree depth and how strong the user already is in the
    attributes this node feeds - so a Pentest node gets harder to level as
    Pentest grows, rather than verification flattening out over time.
    """
    # The node knows which profile it belongs to, so difficulty is measured
    # against that person's attributes - not a shared pool.
    owner = node["profile_id"] if "profile_id" in node.keys() else "primary"
    attr_map = {a["key"]: a["value"] for a in attribute_values(conn, profile_id=owner)}
    weights = conn.execute(
        "SELECT attribute_key, weight FROM node_weights WHERE node_id=?", (node["id"],)
    ).fetchall()

    if weights:
        total_w = sum(w["weight"] for w in weights) or 1
        weighted_attr = sum(attr_map.get(w["attribute_key"], 0) * w["weight"] for w in weights) / total_w
    else:
        weighted_attr = 0

    # tier 1-4 -> 0-3, attribute strength -> 0-2, target level nudges up
    depth_part = min(node["tier"] - 1, 3)
    attr_part = min(weighted_attr / 6.0, 2.0)
    level_part = min(node["level"] * 0.4, 1.5)

    return max(1, min(5, round(1 + depth_part * 0.7 + attr_part + level_part * 0.5)))


def build_context(conn, node, difficulty):
    """Everything the mentor needs to write good questions for this node."""
    owner = node["profile_id"] if "profile_id" in node.keys() else "primary"
    attrs = attribute_values(conn, profile_id=owner)
    weights = [
        dict(r) for r in conn.execute(
            "SELECT attribute_key, weight FROM node_weights WHERE node_id=?", (node["id"],)
        )
    ]
    parents = [
        dict(r) for r in conn.execute(
            "SELECT n.title, n.level FROM skill_edges e "
            "JOIN skill_nodes n ON n.id = e.from_id WHERE e.to_id = ?",
            (node["id"],),
        )
    ]
    return {
        "node": {
            "id": node["id"],
            "title": node["title"],
            "description": node["description"],
            "domain": node["domain"],
            "tier": node["tier"],
            "current_level": node["level"],
            "target_level": node["level"] + 1,
            "max_level": node["max_level"],
        },
        "feeds_attributes": weights,
        "prerequisite_nodes": parents,
        "user_attributes": {a["key"]: a["value"] for a in attrs},
        "difficulty": difficulty,
    }
