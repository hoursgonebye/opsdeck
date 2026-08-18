"""
Academics - the transcript, and every GPA derived from it, under
/api/academics.

Ground rules, carried through every endpoint here:

- **No GPA is ever stored.** A term GPA, a cumulative GPA, a goal's progress
  and a forecast are all one pass over acad_courses joined to the grade
  scale. Fix a grade - or the scale itself - and every number in the section
  re-derives with no backfill. Same discipline as the XP and money ledgers.

- **The grade scale is data, not code.** Institutions genuinely differ, and a
  wrong scale is the one input that silently corrupts everything downstream
  while still looking plausible. Entries proven against a real transcript are
  marked verified; the UI flags the rest until the user confirms them.

- **`points IS NULL` is not zero.** A W carries no quality points and is not
  in the average at all; an F is a real 0.0 that drags the average down.
  Conflating them inflates a GPA, which is the error that matters most here.

- **Imports never write on upload.** /import/preview parses and reports;
  /import/commit re-parses the same text and writes in one transaction. The
  preview also checks its own arithmetic against the term GPAs the transcript
  prints - if they disagree, the grade scale is wrong and the import says so
  instead of quietly seeding bad data.

- **Scope inherits through the term.** acad_courses carries no profile_id;
  every course query joins acad_terms, exactly as transactions join accounts.

Mounted as its own blueprint but profile-scoped, borrowing api.py's
resolve_profile so there is still exactly one place scoping happens.
"""
import json
import re

from flask import Blueprint, jsonify, request

from api import require_token, resolve_profile, active_profile, body, one, many
from db import connect

academics = Blueprint("academics", __name__, url_prefix="/api/academics")
academics.before_request(resolve_profile)

SEASONS = ("winter", "spring", "summer", "fall")
SEASON_ORDER = {s: i for i, s in enumerate(SEASONS)}
TERM_STATUSES = ("completed", "in_progress", "planned")

# Credits are whole numbers or halves in every real catalog; the ceiling is a
# guard against a typo (a "30 credit" course) skewing a cumulative GPA.
MAX_CREDITS = 24.0


# ------------------------------------------------------------------ helpers

def _round(value, places=3):
    return None if value is None else round(value + 0.0, places)


def _num(value, fallback=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _tags(raw):
    try:
        parsed = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return [str(t) for t in parsed if isinstance(t, (str, int, float))]


def _clean_credits(value, fallback=3.0):
    n = _num(value, None)
    if n is None:
        return fallback
    return max(0.0, min(MAX_CREDITS, n))


def _season_of(name, fallback="fall"):
    low = (name or "").lower()
    for s in SEASONS:
        if s in low:
            return s
    return fallback


def _year_of(name, fallback=0):
    m = re.search(r"(19|20)\d{2}", name or "")
    return int(m.group(0)) if m else fallback


# ------------------------------------------------------------- grade scale

def grade_scale(conn, profile_id):
    """{GRADE: {points, counts_gpa, earns_credit, verified, sort_order}}."""
    out = {}
    for r in conn.execute(
        "SELECT * FROM acad_grade_scale WHERE profile_id=? ORDER BY sort_order, grade",
        (profile_id,),
    ):
        out[r["grade"].upper()] = {
            "grade": r["grade"].upper(),
            "points": r["points"],
            "counts_gpa": bool(r["counts_gpa"]),
            "earns_credit": bool(r["earns_credit"]),
            "verified": bool(r["verified"]),
            "sort_order": r["sort_order"],
        }
    return out


def max_points(scale):
    """The best grade available, used for 'is this target still reachable'."""
    values = [g["points"] for g in scale.values()
              if g["counts_gpa"] and g["points"] is not None]
    return max(values) if values else 4.0


# ----------------------------------------------------------------- the math
#
# One function does all of it. Everything else in this module - term rows,
# cumulative totals, goal progress, the forecast - is this called over a
# different slice of courses.

def effective_grade(course, projected=False):
    grade = (course.get("grade") or "").upper()
    if not grade and projected:
        grade = (course.get("projected_grade") or "").upper()
    return grade


def _counts(course, scale, projected=False):
    """Does this course currently contribute to a GPA at all?"""
    rule = scale.get(effective_grade(course, projected))
    return bool(rule and rule["counts_gpa"] and rule["points"] is not None
                and not course.get("exclude_from_gpa"))


def _contribution(course, scale, projected=False):
    """(gpa_units, quality_points) this course puts into an average."""
    if not _counts(course, scale, projected):
        return 0.0, 0.0
    credits = _clean_credits(course.get("credits"))
    return credits, credits * scale[effective_grade(course, projected)]["points"]


def replacements(courses, scale, projected=False):
    """
    Work out what the repeat links mean right now.

    Returns (superseded, pending):
      superseded - ids of attempts whose retake is already graded, so they
                   have stopped counting
      pending    - the course dicts a *scheduled but ungraded* retake will
                   retire once its grade posts

    The split matters. A pending replacement must not touch today's GPA - the
    registrar has not applied it yet - but it absolutely must be in the
    forecast, because finishing that retake both adds credits and removes an
    old grade. Ignoring the removal is how you tell someone they need a 3.65
    when the real answer is 3.29.
    """
    by_id = {c["id"]: c for c in courses if c.get("id") is not None}
    superseded, pending = set(), []

    for c in courses:
        target = c.get("replaces_course_id")
        old = by_id.get(target) if target else None
        if old is None:
            continue
        if effective_grade(c, projected):
            superseded.add(old["id"])
        elif _counts(old, scale):
            # Scheduled, not yet graded: still counting today, but on its way out.
            pending.append(old)
    return superseded, pending


def tally(courses, scale, projected=False, superseded=None):
    """
    Roll a list of course dicts into transcript totals.

    projected=True substitutes projected_grade wherever a real grade is
    missing, which is what turns "this term so far" into "this term if
    nothing changes". A course with neither is still counted as attempted -
    credits you are sitting in are credits you attempted - but contributes
    nothing to the average, which is exactly how an in-progress term prints.

    `superseded` is a set of course ids retired by a graded retake. They keep
    their attempted credits (the attempt happened and stays on the record) but
    stop earning credit and stop counting toward the average - a repeated
    course is only ever worth its credits once.
    """
    superseded = superseded or set()
    attempted = earned = gpa_units = points = 0.0
    graded = 0

    for c in courses:
        credits = _clean_credits(c.get("credits"))
        attempted += credits

        grade = effective_grade(c, projected)
        if not grade:
            continue

        rule = scale.get(grade)
        if rule is None:
            # An unknown grade is attempted-only. Never guess a value for it:
            # a silent 0.0 would read as an F the student never got.
            continue

        graded += 1
        if c.get("id") is not None and c["id"] in superseded:
            continue
        if rule["earns_credit"]:
            earned += credits
        if rule["counts_gpa"] and rule["points"] is not None and not c.get("exclude_from_gpa"):
            gpa_units += credits
            points += credits * rule["points"]

    return {
        "attempted": _round(attempted),
        "earned": _round(earned),
        "gpa_units": _round(gpa_units),
        "points": _round(points),
        "gpa": _round(points / gpa_units, 3) if gpa_units else None,
        "graded_courses": graded,
        "course_count": len(courses),
    }


def needed_gpa(points, gpa_units, remaining_units, target):
    """
    The average needed across `remaining_units` more credits to land the
    cumulative GPA on `target`.

        (points + needed * remaining) / (units + remaining) = target

    Returns None when there is nothing left to average over - a closed
    transcript has no "needed", only a result.
    """
    if not remaining_units or remaining_units <= 0:
        return None
    return _round((target * (gpa_units + remaining_units) - points) / remaining_units)


def credits_to_reach(points, gpa_units, target, best):
    """
    How many additional credits at the best available grade it would take to
    reach `target`, ignoring what is already scheduled.

        (points + best * x) / (units + x) >= target

    Returns 0.0 if already there, None if the target is at or above the best
    possible grade (no finite amount of coursework gets you to a 4.0 average
    once anything below one is on the record).
    """
    if gpa_units and points / gpa_units >= target:
        return 0.0
    if target >= best:
        return None
    return _round(max(0.0, (target * gpa_units - points) / (best - target)), 1)


# --------------------------------------------------------------- reading it

def load_terms(conn, profile_id):
    """Every term for a profile, each with its courses, in transcript order."""
    terms = many(
        conn,
        "SELECT * FROM acad_terms WHERE profile_id=? ORDER BY year, "
        "CASE season WHEN 'winter' THEN 0 WHEN 'spring' THEN 1 "
        "WHEN 'summer' THEN 2 ELSE 3 END, id",
        (profile_id,),
    )
    if not terms:
        return []

    by_id = {t["id"]: t for t in terms}
    for t in terms:
        t["courses"] = []
    placeholders = ",".join("?" * len(by_id))
    for c in many(
        conn,
        f"SELECT * FROM acad_courses WHERE term_id IN ({placeholders}) "
        f"ORDER BY position, id",
        tuple(by_id),
    ):
        c["tags"] = _tags(c["tags"])
        c["exclude_from_gpa"] = bool(c["exclude_from_gpa"])
        by_id[c["term_id"]]["courses"].append(c)
    return terms


def all_courses(terms):
    return [c for t in terms for c in t["courses"]]


def scoped_courses(terms, tag=None):
    courses = all_courses(terms)
    if not tag:
        return courses
    return [c for c in courses if tag in c["tags"]]


def _remaining(courses):
    """Courses with no final grade yet - the credits still in play."""
    return [c for c in courses if not (c.get("grade") or "").strip()]


def goal_progress(goal, terms, scale):
    """
    Where one GPA floor stands: what the average is now over the goal's
    scope, what the remaining scheduled credits would have to average to
    land on it, and - if that is impossible - how much more coursework it
    would take at straight top grades.
    """
    scoped = scoped_courses(terms, goal["scope_tag"])
    sup_now, pending = replacements(scoped, scale)
    sup_proj, _ = replacements(scoped, scale, projected=True)
    now = tally(scoped, scale, superseded=sup_now)
    projected = tally(scoped, scale, projected=True, superseded=sup_proj)

    remaining = _remaining(scoped)
    remaining_units = sum(
        _clean_credits(c["credits"]) for c in remaining
        if not c["exclude_from_gpa"]
        and (scale.get((c.get("projected_grade") or "").upper()) or {}).get("counts_gpa", True)
    )

    # Credits and points that are counted today but will drop out when the
    # retakes already scheduled are graded.
    pending_units = pending_points = 0.0
    for old in pending:
        u, p = _contribution(old, scale)
        pending_units += u
        pending_points += p

    target = float(goal["target_gpa"])
    best = max_points(scale)
    need = needed_gpa((now["points"] or 0.0) - pending_points,
                      (now["gpa_units"] or 0.0) - pending_units,
                      remaining_units, target)

    return {
        **{k: goal[k] for k in ("id", "name", "target_gpa", "scope_tag", "note", "position")},
        "current_gpa": now["gpa"],
        "projected_gpa": projected["gpa"],
        "gpa_units": now["gpa_units"],
        "points": now["points"],
        "met": now["gpa"] is not None and now["gpa"] >= target,
        "projected_met": projected["gpa"] is not None and projected["gpa"] >= target,
        "remaining_units": _round(remaining_units),
        "needed_gpa": need,
        # A "needed" above the best grade on the scale is arithmetic, not
        # encouragement: the target cannot be reached inside the credits
        # already scheduled, whatever effort goes in.
        "reachable_in_scheduled": need is not None and need <= best,
        "extra_credits_needed": credits_to_reach(
            now["points"] or 0.0, now["gpa_units"] or 0.0, target, best),
        "best_grade_points": best,
    }


def overview(conn, profile_id):
    scale = grade_scale(conn, profile_id)
    terms = load_terms(conn, profile_id)
    courses = all_courses(terms)
    by_id = {c["id"]: c for c in courses}

    sup_now, pending = replacements(courses, scale)
    sup_proj, _ = replacements(courses, scale, projected=True)

    # Annotate both ends of every repeat link so the UI can show the pairing
    # without re-deriving it.
    pending_ids = {c["id"] for c in pending}
    for c in courses:
        old = by_id.get(c.get("replaces_course_id")) if c.get("replaces_course_id") else None
        c["replaces"] = {"id": old["id"], "code": old["code"], "grade": old["grade"]} if old else None
        c["superseded"] = c["id"] in sup_now
        c["superseded_pending"] = c["id"] in pending_ids
    for c in courses:
        old = by_id.get(c.get("replaces_course_id")) if c.get("replaces_course_id") else None
        if old is not None:
            old["replaced_by"] = {"id": c["id"], "code": c["code"],
                                  "grade": c["grade"], "term_id": c["term_id"]}
    for c in courses:
        c.setdefault("replaced_by", None)

    out_terms = []
    running_points = running_units = 0.0
    for t in terms:
        # Term totals stay historical - unadjusted for any later repeat. That
        # is both how a transcript prints and what the import check compares
        # against, so adjusting them here would make every re-import look like
        # a broken grade scale.
        term = tally(t["courses"], scale)
        term_proj = tally(t["courses"], scale, projected=True)
        running_points += term["points"] or 0.0
        running_units += term["gpa_units"] or 0.0

        # A retake graded in *this* term retires the attempt it replaced, from
        # this point in the timeline forward. Earlier terms keep the average
        # they actually had at the time.
        for c in t["courses"]:
            old = by_id.get(c.get("replaces_course_id")) if c.get("replaces_course_id") else None
            if old is not None and old["id"] in sup_now:
                u, p = _contribution(old, scale)
                running_units -= u
                running_points -= p

        out_terms.append({
            **{k: t[k] for k in ("id", "name", "season", "year", "status",
                                 "institution", "notes")},
            "courses": t["courses"],
            "totals": term,
            "projected_totals": term_proj,
            # The cumulative GPA *as of the end of this term*, which is the
            # column a real transcript prints and the one that shows the
            # shape of a trend rather than a single endpoint.
            "cumulative_gpa": _round(running_points / running_units, 3) if running_units else None,
        })

    cumulative = tally(courses, scale, superseded=sup_now)
    projected = tally(courses, scale, projected=True, superseded=sup_proj)

    goals = [goal_progress(g, terms, scale) for g in many(
        conn, "SELECT * FROM acad_goals WHERE profile_id=? ORDER BY position, id",
        (profile_id,))]

    # Which unverified grades are actually load-bearing right now. An
    # unconfirmed C+ nobody has been given is noise; one sitting in the
    # transcript is a number the user should check before trusting any of it.
    used = {(c.get("grade") or "").upper() for c in courses}
    used |= {(c.get("projected_grade") or "").upper() for c in courses}
    unverified = sorted(g for g in used if g and g in scale and not scale[g]["verified"])
    unknown = sorted(g for g in used if g and g not in scale)

    return {
        "terms": out_terms,
        "cumulative": cumulative,
        "projected": projected,
        "goals": goals,
        "scale": [scale[g] for g in sorted(scale, key=lambda k: scale[k]["sort_order"])],
        "tags": sorted({t for c in courses for t in c["tags"]}),
        "unverified_grades_in_use": unverified,
        "unknown_grades_in_use": unknown,
        # Repeats in flight, so the UI can explain why the term totals above
        # no longer sum to the cumulative underneath them.
        "repeats": {
            "applied": sorted(sup_now),
            "pending": [{"id": c["id"], "code": c["code"], "grade": c["grade"],
                         "credits": c["credits"]} for c in pending],
        },
    }


# ------------------------------------------------------------------ reading

@academics.route("", methods=["GET"])
@academics.route("/", methods=["GET"])
@require_token
def get_overview():
    conn = connect()
    try:
        return jsonify(overview(conn, active_profile()))
    finally:
        conn.close()


@academics.route("/forecast", methods=["GET"])
@require_token
def get_forecast():
    """
    A standalone what-if: given a target and an optional block of credits at
    a hypothetical average, where does the cumulative GPA land?

    Query: ?target=3.4&tag=&credits=17&at=3.7
    """
    conn = connect()
    try:
        profile = active_profile()
        scale = grade_scale(conn, profile)
        terms = load_terms(conn, profile)
        scoped = scoped_courses(terms, request.args.get("tag") or None)

        sup_now, pending = replacements(scoped, scale)
        now = tally(scoped, scale, superseded=sup_now)
        # Scheduled retakes take their originals with them; the forecast has to
        # net that out or it asks for a GPA higher than the situation requires.
        pending_units = pending_points = 0.0
        for old in pending:
            u, p = _contribution(old, scale)
            pending_units += u
            pending_points += p

        points = (now["points"] or 0.0) - pending_points
        units = (now["gpa_units"] or 0.0) - pending_units
        best = max_points(scale)

        target = _num(request.args.get("target"), 3.4)
        credits = max(0.0, _num(request.args.get("credits"), 0.0) or 0.0)
        at = _num(request.args.get("at"), None)

        # Credits already scheduled but ungraded, as the natural default for
        # "the window I have left".
        scheduled = sum(_clean_credits(c["credits"]) for c in _remaining(scoped)
                        if not c["exclude_from_gpa"])
        if not credits:
            credits = scheduled

        result = {
            "current_gpa": now["gpa"],
            "gpa_units": _round(units),
            "points": _round(points),
            "pending_replacement_units": _round(pending_units),
            "pending_replacement_points": _round(pending_points),
            "scheduled_ungraded_credits": _round(scheduled),
            "credits": _round(credits),
            "target": target,
            "needed_gpa": needed_gpa(points, units, credits, target),
            "best_grade_points": best,
            "max_possible_gpa": _round((points + best * credits) / (units + credits), 3)
                                if units + credits else None,
            "extra_credits_needed": credits_to_reach(points, units, target, best),
        }
        if at is not None and units + credits:
            result["resulting_gpa"] = _round((points + at * credits) / (units + credits), 3)
        return jsonify(result)
    finally:
        conn.close()


# ------------------------------------------------------------------- terms

def _term_payload(d, existing=None):
    name = (d.get("name") or (existing or {}).get("name") or "").strip()
    season = (d.get("season") or "").lower()
    if season not in SEASONS:
        season = _season_of(name, (existing or {}).get("season", "fall"))
    year = d.get("year")
    year = int(year) if str(year or "").isdigit() else _year_of(
        name, (existing or {}).get("year", 0))
    status = d.get("status") or (existing or {}).get("status") or "completed"
    if status not in TERM_STATUSES:
        status = "completed"
    return name, season, year, status


@academics.route("/terms", methods=["POST"])
@require_token
def create_term():
    d = body()
    name, season, year, status = _term_payload(d)
    if not name:
        return jsonify({"error": "term name required"}), 400

    conn = connect()
    try:
        dup = one(conn, "SELECT id FROM acad_terms WHERE profile_id=? AND name=?",
                  (active_profile(), name))
        if dup:
            return jsonify({"error": f"term {name!r} already exists",
                            "term_id": dup["id"]}), 409
        cur = conn.execute(
            "INSERT INTO acad_terms (profile_id,name,season,year,status,institution,notes) "
            "VALUES (?,?,?,?,?,?,?)",
            (active_profile(), name, season, year, status,
             (d.get("institution") or "").strip(), (d.get("notes") or "").strip()),
        )
        conn.commit()
        return jsonify(one(conn, "SELECT * FROM acad_terms WHERE id=?", (cur.lastrowid,))), 201
    finally:
        conn.close()


@academics.route("/terms/<int:term_id>", methods=["PATCH"])
@require_token
def update_term(term_id):
    d = body()
    conn = connect()
    try:
        row = one(conn, "SELECT * FROM acad_terms WHERE id=? AND profile_id=?",
                  (term_id, active_profile()))
        if not row:
            return jsonify({"error": "term not found"}), 404
        name, season, year, status = _term_payload(d, row)
        conn.execute(
            "UPDATE acad_terms SET name=?, season=?, year=?, status=?, "
            "institution=?, notes=? WHERE id=?",
            (name or row["name"], season, year, status,
             d.get("institution", row["institution"]), d.get("notes", row["notes"]),
             term_id),
        )
        conn.commit()
        return jsonify(one(conn, "SELECT * FROM acad_terms WHERE id=?", (term_id,)))
    finally:
        conn.close()


@academics.route("/terms/<int:term_id>", methods=["DELETE"])
@require_token
def delete_term(term_id):
    conn = connect()
    try:
        row = one(conn, "SELECT id FROM acad_terms WHERE id=? AND profile_id=?",
                  (term_id, active_profile()))
        if not row:
            return jsonify({"error": "term not found"}), 404
        conn.execute("DELETE FROM acad_terms WHERE id=?", (term_id,))
        conn.commit()
        return "", 204
    finally:
        conn.close()


# ----------------------------------------------------------------- courses

def _owned_term(conn, term_id):
    return one(conn, "SELECT * FROM acad_terms WHERE id=? AND profile_id=?",
               (term_id, active_profile()))


def _resolve_replaces(conn, value, self_id):
    """
    Validate a repeat link. Returns (course_id_or_None, error_or_None).

    Rejects linking a course to itself and to anything outside the profile;
    a two-course cycle would make each attempt retire the other and the pair
    would vanish from the GPA entirely.
    """
    if value in (None, "", 0):
        return None, None
    try:
        target = int(value)
    except (TypeError, ValueError):
        return None, "replaces_course_id must be a course id"
    if self_id is not None and target == self_id:
        return None, "a course cannot replace itself"
    owned = one(conn,
                "SELECT c.id FROM acad_courses c JOIN acad_terms t ON t.id=c.term_id "
                "WHERE c.id=? AND t.profile_id=?", (target, active_profile()))
    if not owned:
        return None, "the course being replaced was not found"
    if self_id is not None:
        back = one(conn, "SELECT replaces_course_id FROM acad_courses WHERE id=?", (target,))
        if back and back["replaces_course_id"] == self_id:
            return None, "those two courses would replace each other"
    return target, None


@academics.route("/courses", methods=["POST"])
@require_token
def create_course():
    d = body()
    try:
        term_id = int(d.get("term_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "term_id required"}), 400

    conn = connect()
    try:
        if not _owned_term(conn, term_id):
            return jsonify({"error": "term not found"}), 404
        pos = conn.execute(
            "SELECT COALESCE(MAX(position),-1)+1 FROM acad_courses WHERE term_id=?",
            (term_id,)).fetchone()[0]
        replaces, err = _resolve_replaces(conn, d.get("replaces_course_id"), None)
        if err:
            return jsonify({"error": err}), 400

        cur = conn.execute(
            "INSERT INTO acad_courses "
            "(term_id,code,title,credits,grade,projected_grade,tags,"
            " exclude_from_gpa,position,notes,replaces_course_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (term_id, (d.get("code") or "").strip(), (d.get("title") or "").strip(),
             _clean_credits(d.get("credits")), (d.get("grade") or "").strip().upper(),
             (d.get("projected_grade") or "").strip().upper(),
             json.dumps(_tags(json.dumps(d.get("tags") or []))),
             1 if d.get("exclude_from_gpa") else 0, pos, (d.get("notes") or "").strip(),
             replaces),
        )
        conn.commit()
        return jsonify(one(conn, "SELECT * FROM acad_courses WHERE id=?", (cur.lastrowid,))), 201
    finally:
        conn.close()


@academics.route("/courses/<int:course_id>", methods=["PATCH"])
@require_token
def update_course(course_id):
    d = body()
    conn = connect()
    try:
        row = one(conn,
                  "SELECT c.* FROM acad_courses c JOIN acad_terms t ON t.id=c.term_id "
                  "WHERE c.id=? AND t.profile_id=?", (course_id, active_profile()))
        if not row:
            return jsonify({"error": "course not found"}), 404

        fields, params = [], []
        for key in ("code", "title", "notes"):
            if key in d:
                fields.append(f"{key}=?")
                params.append((d.get(key) or "").strip())
        for key in ("grade", "projected_grade"):
            if key in d:
                fields.append(f"{key}=?")
                params.append((d.get(key) or "").strip().upper())
        if "credits" in d:
            fields.append("credits=?")
            params.append(_clean_credits(d.get("credits"), row["credits"]))
        if "tags" in d:
            fields.append("tags=?")
            params.append(json.dumps(_tags(json.dumps(d.get("tags") or []))))
        if "exclude_from_gpa" in d:
            fields.append("exclude_from_gpa=?")
            params.append(1 if d.get("exclude_from_gpa") else 0)
        if "position" in d:
            fields.append("position=?")
            params.append(int(d.get("position") or 0))
        if "term_id" in d:
            if not _owned_term(conn, int(d["term_id"])):
                return jsonify({"error": "term not found"}), 404
            fields.append("term_id=?")
            params.append(int(d["term_id"]))
        if "replaces_course_id" in d:
            replaces, err = _resolve_replaces(conn, d.get("replaces_course_id"), course_id)
            if err:
                return jsonify({"error": err}), 400
            fields.append("replaces_course_id=?")
            params.append(replaces)

        if fields:
            params.append(course_id)
            conn.execute(f"UPDATE acad_courses SET {', '.join(fields)} WHERE id=?", params)
            conn.commit()
        return jsonify(one(conn, "SELECT * FROM acad_courses WHERE id=?", (course_id,)))
    finally:
        conn.close()


@academics.route("/courses/<int:course_id>", methods=["DELETE"])
@require_token
def delete_course(course_id):
    conn = connect()
    try:
        row = one(conn,
                  "SELECT c.id FROM acad_courses c JOIN acad_terms t ON t.id=c.term_id "
                  "WHERE c.id=? AND t.profile_id=?", (course_id, active_profile()))
        if not row:
            return jsonify({"error": "course not found"}), 404
        conn.execute("DELETE FROM acad_courses WHERE id=?", (course_id,))
        conn.commit()
        return "", 204
    finally:
        conn.close()


# ------------------------------------------------------------- grade scale

@academics.route("/scale", methods=["GET"])
@require_token
def get_scale():
    conn = connect()
    try:
        scale = grade_scale(conn, active_profile())
        return jsonify(sorted(scale.values(), key=lambda g: g["sort_order"]))
    finally:
        conn.close()


@academics.route("/scale", methods=["PUT"])
@require_token
def put_scale():
    """
    Upsert one or more grades. Editing a grade re-derives every GPA that used
    it, which is the point of never storing one.

    Body: {"grades": [{"grade":"C+", "points":2.5, "counts_gpa":true,
                       "earns_credit":true, "verified":true}]}
    """
    entries = body().get("grades")
    if not isinstance(entries, list) or not entries:
        return jsonify({"error": "grades must be a non-empty list"}), 400

    conn = connect()
    try:
        profile = active_profile()
        for i, e in enumerate(entries):
            grade = (e.get("grade") or "").strip().upper()
            if not grade:
                return jsonify({"error": f"entry {i}: grade required"}), 400
            points = e.get("points")
            points = None if points in (None, "") else _num(points, None)
            if points is not None and not (0.0 <= points <= 10.0):
                return jsonify({"error": f"{grade}: points out of range"}), 400
            existing = one(conn,
                           "SELECT sort_order FROM acad_grade_scale WHERE profile_id=? AND grade=?",
                           (profile, grade))
            conn.execute(
                "INSERT INTO acad_grade_scale "
                "(profile_id,grade,points,counts_gpa,earns_credit,sort_order,verified) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(profile_id,grade) DO UPDATE SET "
                "points=excluded.points, counts_gpa=excluded.counts_gpa, "
                "earns_credit=excluded.earns_credit, verified=excluded.verified",
                (profile, grade, points,
                 1 if e.get("counts_gpa", points is not None) else 0,
                 1 if e.get("earns_credit", True) else 0,
                 existing["sort_order"] if existing else int(e.get("sort_order") or 99),
                 1 if e.get("verified") else 0),
            )
        conn.commit()
        scale = grade_scale(conn, profile)
        return jsonify(sorted(scale.values(), key=lambda g: g["sort_order"]))
    finally:
        conn.close()


@academics.route("/scale/<grade>", methods=["DELETE"])
@require_token
def delete_grade(grade):
    conn = connect()
    try:
        conn.execute("DELETE FROM acad_grade_scale WHERE profile_id=? AND grade=?",
                     (active_profile(), grade.upper()))
        conn.commit()
        return "", 204
    finally:
        conn.close()


# -------------------------------------------------------------------- goals

@academics.route("/goals", methods=["GET"])
@require_token
def get_goals():
    conn = connect()
    try:
        profile = active_profile()
        scale = grade_scale(conn, profile)
        terms = load_terms(conn, profile)
        return jsonify([goal_progress(g, terms, scale) for g in many(
            conn, "SELECT * FROM acad_goals WHERE profile_id=? ORDER BY position, id",
            (profile,))])
    finally:
        conn.close()


@academics.route("/goals", methods=["POST"])
@require_token
def create_goal():
    d = body()
    name = (d.get("name") or "").strip()
    target = _num(d.get("target_gpa"), None)
    if not name:
        return jsonify({"error": "name required"}), 400
    if target is None or not (0.0 < target <= 10.0):
        return jsonify({"error": "target_gpa must be a positive number"}), 400

    conn = connect()
    try:
        profile = active_profile()
        pos = conn.execute(
            "SELECT COALESCE(MAX(position),-1)+1 FROM acad_goals WHERE profile_id=?",
            (profile,)).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO acad_goals (profile_id,name,target_gpa,scope_tag,note,position) "
            "VALUES (?,?,?,?,?,?)",
            (profile, name, target, (d.get("scope_tag") or "").strip() or None,
             (d.get("note") or "").strip(), pos),
        )
        conn.commit()
        return jsonify(one(conn, "SELECT * FROM acad_goals WHERE id=?", (cur.lastrowid,))), 201
    finally:
        conn.close()


@academics.route("/goals/<int:goal_id>", methods=["PATCH"])
@require_token
def update_goal(goal_id):
    d = body()
    conn = connect()
    try:
        row = one(conn, "SELECT * FROM acad_goals WHERE id=? AND profile_id=?",
                  (goal_id, active_profile()))
        if not row:
            return jsonify({"error": "goal not found"}), 404
        target = _num(d.get("target_gpa"), row["target_gpa"])
        if not (0.0 < target <= 10.0):
            return jsonify({"error": "target_gpa out of range"}), 400
        conn.execute(
            "UPDATE acad_goals SET name=?, target_gpa=?, scope_tag=?, note=? WHERE id=?",
            ((d.get("name") or row["name"]).strip(), target,
             (d.get("scope_tag", row["scope_tag"]) or "").strip() or None,
             d.get("note", row["note"]), goal_id),
        )
        conn.commit()
        return jsonify(one(conn, "SELECT * FROM acad_goals WHERE id=?", (goal_id,)))
    finally:
        conn.close()


@academics.route("/goals/<int:goal_id>", methods=["DELETE"])
@require_token
def delete_goal(goal_id):
    conn = connect()
    try:
        row = one(conn, "SELECT id FROM acad_goals WHERE id=? AND profile_id=?",
                  (goal_id, active_profile()))
        if not row:
            return jsonify({"error": "goal not found"}), 404
        conn.execute("DELETE FROM acad_goals WHERE id=?", (goal_id,))
        conn.commit()
        return "", 204
    finally:
        conn.close()


# ------------------------------------------------------------ the importer
#
# Written against the PeopleSoft-style unofficial transcript WCC hands out,
# which is what a student can actually copy out of the portal. It is loose
# about whitespace and strict about shape: anything it cannot read with
# confidence goes in `unparsed` for the user to see, never silently dropped.

TERM_RE = re.compile(r"^\s*(Winter|Spring|Summer|Fall)\s+((?:19|20)\d{2})\s*$", re.I)

# 'CIS  110 Object Oriented Prog Logic 3.000 3.000 A 12.000' and the
# in-progress form with no grade at all:
# 'MATH  140 Statistics 4.000 0.000 0.000'
COURSE_RE = re.compile(
    r"^\s*(?P<subject>[A-Z]{2,8})\s+(?P<number>\d{2,4}[A-Z]?)\s+"
    r"(?P<title>.*?)\s+"
    r"(?P<attempted>\d+(?:\.\d+)?)\s+(?P<earned>\d+(?:\.\d+)?)\s+"
    r"(?:(?P<grade>[A-Z]{1,3}[+-]?)\s+)?"
    r"(?P<points>\d+(?:\.\d+)?)\s*$"
)

TERM_GPA_RE = re.compile(
    r"Term\s+GPA\s+(?P<gpa>\d+(?:\.\d+)?)\s+Term\s+Totals\s+"
    r"(?P<attempted>\d+(?:\.\d+)?)\s+(?P<earned>\d+(?:\.\d+)?)\s+"
    r"(?P<units>\d+(?:\.\d+)?)\s+(?P<points>\d+(?:\.\d+)?)", re.I
)

CUM_GPA_RE = re.compile(
    r"^\s*Cum\s+GPA[:\s]+(?P<gpa>\d+(?:\.\d+)?)\s+Cum\s+Totals\s+"
    r"(?P<attempted>\d+(?:\.\d+)?)\s+(?P<earned>\d+(?:\.\d+)?)\s+"
    r"(?P<units>\d+(?:\.\d+)?)\s+(?P<points>\d+(?:\.\d+)?)", re.I
)

# Header and boilerplate rows that are neither courses nor totals. Matching
# these keeps `unparsed` meaningful - a list of 40 header fragments would
# train the user to ignore it, and then it would stop catching real misses.
NOISE_RE = re.compile(
    r"^\s*(course\s+description|attempted\s+earned|transfer|combined|"
    r"academic\s+(standing|program)|beginning\s+of|end\s+of|undergraduate|"
    r"name:|student\s+id|print\s+date|institution|ssn|dob|program:|"
    r"\d{4}-\d{2}-\d{2}:|term\s+honor|[\d\s./-]*)\s*$", re.I
)


def parse_transcript(text):
    """
    Pasted transcript text into terms and courses.

    Also captures the totals the transcript prints for itself. Those are the
    check that makes an import trustworthy: if our arithmetic over the parsed
    courses disagrees with the GPA the registrar printed, the grade scale is
    wrong, and saying so is far more useful than importing numbers that look
    fine and quietly aren't.
    """
    terms, unparsed = [], []
    current = None

    for raw in (text or "").splitlines():
        # PDF and portal copy-paste is full of non-breaking and narrow
        # spaces, which the \s+ in every pattern below would otherwise
        # refuse to match. Written as escapes because the literal
        # characters are invisible in a diff.
        line = raw.replace(" ", " ").replace(" ", " ").rstrip()
        if not line.strip():
            continue

        m = TERM_RE.match(line)
        if m:
            season = m.group(1).lower()
            year = int(m.group(2))
            current = {
                "name": f"{m.group(1).capitalize()} {year}",
                "season": season,
                "year": year,
                "courses": [],
                "stated": None,
            }
            terms.append(current)
            continue

        m = TERM_GPA_RE.search(line)
        if m and current:
            current["stated"] = {
                "gpa": float(m.group("gpa")),
                "attempted": float(m.group("attempted")),
                "earned": float(m.group("earned")),
                "gpa_units": float(m.group("units")),
                "points": float(m.group("points")),
            }
            continue

        if CUM_GPA_RE.match(line):
            continue

        m = COURSE_RE.match(line)
        if m and current:
            grade = (m.group("grade") or "").upper()
            current["courses"].append({
                "code": f"{m.group('subject')} {m.group('number')}",
                "title": m.group("title").strip(),
                "credits": float(m.group("attempted")),
                "grade": grade,
                "stated_points": float(m.group("points")),
                "stated_earned": float(m.group("earned")),
            })
            continue

        if not NOISE_RE.match(line):
            unparsed.append(line.strip())

    # A term header with nothing under it is a parse artifact, not a term.
    terms = [t for t in terms if t["courses"]]
    for t in terms:
        t["status"] = "completed" if all(c["grade"] for c in t["courses"]) else "in_progress"
    return terms, unparsed


def _check_against_stated(term, scale):
    """
    Compare our arithmetic to the transcript's own printed totals.
    Returns (ok, list_of_discrepancies). No stated totals -> nothing to check.
    """
    stated = term.get("stated")
    computed = tally(term["courses"], scale)
    if not stated:
        return None, []

    problems = []
    for key, label in (("gpa", "term GPA"), ("attempted", "credits attempted"),
                       ("earned", "credits earned"), ("gpa_units", "GPA units"),
                       ("points", "quality points")):
        ours = computed.get(key)
        theirs = stated.get(key)
        if theirs is None:
            continue
        # A term with no graded work has no GPA at all. The report still
        # prints "Term GPA 0.000" for it, but that is a placeholder, not a
        # zero average - reading it as a real value flags every in-progress
        # term as a scale error, which is precisely the false alarm that
        # would train the user to click past this check.
        if key == "gpa" and not stated.get("gpa_units"):
            continue
        if ours is None:
            problems.append(f"{label}: transcript says {theirs:g}, we computed nothing")
            continue
        # The GPA is compared at two decimals because that is the precision it
        # is actually printed at - WCC pads to three ("3.240") but the value
        # behind it is 3.2419. Comparing at full precision would flag a
        # rounding artifact as a broken grade scale on almost every import.
        differs = (round(ours, 2) != round(theirs, 2)) if key == "gpa" \
            else abs(ours - theirs) > 0.005
        if differs:
            problems.append(f"{label}: transcript says {theirs:g}, we computed {ours:g}")
    return not problems, problems


@academics.route("/import/preview", methods=["POST"])
@require_token
def import_preview():
    """
    Parse pasted transcript text and report exactly what a commit would do.
    Writes nothing.
    """
    text = body().get("text") or ""
    if not text.strip():
        return jsonify({"error": "no text supplied"}), 400

    conn = connect()
    try:
        profile = active_profile()
        scale = grade_scale(conn, profile)
        terms, unparsed = parse_transcript(text)
        if not terms:
            return jsonify({"error": "no terms found in that text",
                            "unparsed": unparsed[:40]}), 400

        existing = {r["name"]: r["id"] for r in conn.execute(
            "SELECT name, id FROM acad_terms WHERE profile_id=?", (profile,))}

        out, mismatches, unknown = [], 0, set()
        for t in terms:
            ok, problems = _check_against_stated(t, scale)
            if ok is False:
                mismatches += 1
            for c in t["courses"]:
                if c["grade"] and c["grade"] not in scale:
                    unknown.add(c["grade"])
            out.append({
                "name": t["name"], "season": t["season"], "year": t["year"],
                "status": t["status"], "courses": t["courses"],
                "computed": tally(t["courses"], scale),
                "stated": t["stated"],
                "matches_transcript": ok,
                "discrepancies": problems,
                "duplicate": t["name"] in existing,
                "existing_term_id": existing.get(t["name"]),
            })

        return jsonify({
            "terms": out,
            "unparsed": unparsed,
            "unknown_grades": sorted(unknown),
            "mismatched_terms": mismatches,
            # The single sentence worth reading. If any term's arithmetic
            # disagrees with the registrar's, importing is not the next step -
            # fixing the scale is.
            "scale_looks_right": mismatches == 0 and not unknown,
            "new_terms": sum(1 for t in out if not t["duplicate"]),
            "duplicate_terms": sum(1 for t in out if t["duplicate"]),
        })
    finally:
        conn.close()


@academics.route("/import/commit", methods=["POST"])
@require_token
def import_commit():
    """
    Write a previewed transcript. Re-parses the same text rather than
    trusting a structure round-tripped through the browser.

    A term that already exists is skipped unless replace=true, in which case
    its courses are replaced wholesale - the term row (and any notes on it)
    survives. Duplicates are never merged silently.
    """
    d = body()
    text = d.get("text") or ""
    replace = bool(d.get("replace"))
    only = d.get("terms")           # optional whitelist of term names
    if not text.strip():
        return jsonify({"error": "no text supplied"}), 400

    terms, _ = parse_transcript(text)
    if only:
        wanted = {str(n) for n in only}
        terms = [t for t in terms if t["name"] in wanted]
    if not terms:
        return jsonify({"error": "nothing to import"}), 400

    conn = connect()
    try:
        profile = active_profile()
        added_terms = added_courses = replaced = skipped = 0

        for t in terms:
            row = one(conn, "SELECT id FROM acad_terms WHERE profile_id=? AND name=?",
                      (profile, t["name"]))
            if row and not replace:
                skipped += 1
                continue
            if row:
                conn.execute("DELETE FROM acad_courses WHERE term_id=?", (row["id"],))
                conn.execute("UPDATE acad_terms SET status=? WHERE id=?",
                             (t["status"], row["id"]))
                term_id = row["id"]
                replaced += 1
            else:
                term_id = conn.execute(
                    "INSERT INTO acad_terms (profile_id,name,season,year,status,institution) "
                    "VALUES (?,?,?,?,?,?)",
                    (profile, t["name"], t["season"], t["year"], t["status"],
                     (d.get("institution") or "").strip()),
                ).lastrowid
                added_terms += 1

            for pos, c in enumerate(t["courses"]):
                conn.execute(
                    "INSERT INTO acad_courses (term_id,code,title,credits,grade,position) "
                    "VALUES (?,?,?,?,?,?)",
                    (term_id, c["code"], c["title"], c["credits"], c["grade"], pos),
                )
                added_courses += 1

        conn.commit()
        return jsonify({
            "added_terms": added_terms,
            "replaced_terms": replaced,
            "skipped_terms": skipped,
            "added_courses": added_courses,
            "overview": overview(conn, profile),
        })
    finally:
        conn.close()
