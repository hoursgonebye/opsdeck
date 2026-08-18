"""
Tests for the academics module. Run inside the app image:

    docker run --rm -v /root/opstest:/app -w /app opsdeck-opsdeck:latest \
        python test_academics.py

Uses a real WCC transcript as the fixture on purpose (identifiers
redacted - the grades are the part under test). The transcript
prints its own term and cumulative GPAs, so every number here is checked
against a figure a registrar produced rather than one I decided was right -
which is the only way to know the grade scale is correct.
"""
import os
import shutil
import sys
import tempfile

os.environ["OPSDECK_TOKEN"] = "testtoken"

# Point the DB at a scratch dir before importing db, so a test run can never
# touch real data even if it is copied next to a live deployment.
_tmp = tempfile.mkdtemp(prefix="opstest-")
import db  # noqa: E402
db.DATA_DIR = __import__("pathlib").Path(_tmp)
db.DB_PATH = db.DATA_DIR / "opsdeck.db"
db.UPLOAD_DIR = db.DATA_DIR / "uploads"

import academics  # noqa: E402

TRANSCRIPT = """Undergraduate Unofficial Transcript
Name:           STUDENT, EXAMPLE A
Student ID:   000000000
Print Date: 2026-08-11
Institution Info: Example Community College

Academic Program History
Program: School-Business & Prof Careers
2025-06-17: Active in Program
2025-06-17: Cybersecurity Major

Beginning of Undergraduate Record

Fall 2025
Course Description Attempted Earned Grade Points
CIS  110 Computer Info Systems 3.000 0.000 W 0.000
CIS  120 Object Oriented Prog Logic 3.000 0.000 W 0.000
CIS  135 PC Operating Systems 3.000 0.000 W 0.000
ENG  101 Writing and Research 3.000 3.000 B 9.000
MATH  131 College Algebra 4.000 4.000 B 12.000
POL  203 Principles of Investigation 3.000 3.000 A 12.000

Attempted Earned GPA Units Points
Term GPA 3.300 Term Totals 19.000 10.000 10.000 33.000
Transfer Term GPA Transfer Totals 0.000 0.000 0.000 0.000
Combined GPA 3.300 Comb Totals 19.000 10.000 10.000 33.000

Cum GPA 3.300 Cum Totals 19.000 10.000 10.000 33.000
Academic Standing Effective 2026-01-21: Good Standing

Spring 2026
Course Description Attempted Earned Grade Points
CIS  110 Computer Info Systems 3.000 3.000 B 9.000
CIS  120 Object Oriented Prog Logic 3.000 3.000 A 12.000
CIS  130 Computer Hardware 3.000 3.000 A 12.000
ENG  102 Writing and Literature 3.000 3.000 A 12.000
SOC  101 Introduction to Sociology 3.000 3.000 B+ 10.500

Attempted Earned GPA Units Points
Term GPA 3.700 Term Totals 15.000 15.000 15.000 55.500
Combined GPA 3.700 Comb Totals 15.000 15.000 15.000 55.500

Cum GPA 3.540 Cum Totals 34.000 25.000 25.000 88.500
Term Honor: Dean's List

Summer 2026
Course Description Attempted Earned Grade Points
ECON  101 Macroeconomics 3.000 3.000 D 3.000
HIS  112 20th Century U.S. History 3.000 3.000 B 9.000

Attempted Earned GPA Units Points
Term GPA 2.000 Term Totals 6.000 6.000 6.000 12.000

Cum GPA 3.240 Cum Totals 40.000 31.000 31.000 100.500

Fall 2026
Course Description Attempted Earned Grade Points
BTECH  240 Business Communications 3.000 0.000 0.000
CIS  135 PC Operating Systems 3.000 0.000 0.000
CIS  140 Networking For Business 3.000 0.000 0.000
MATH  140 Statistics 4.000 0.000 0.000
PHYSC  143 Earth Science Lec/Lab 4.000 0.000 0.000

Attempted Earned GPA Units Points
Term GPA 0.000 Term Totals 17.000 0.000 0.000 0.000

Cum GPA 3.240 Cum Totals 57.000 31.000 31.000 100.500
Undergraduate Career Totals
Cum GPA: 3.240 Cum Totals 57.000 31.000 31.000 100.500
End of Undergraduate Unofficial Transcript
"""

FAILS = []


def check(label, got, want, tol=1e-6):
    ok = (got is None and want is None) or (
        got is not None and want is not None and abs(got - want) <= tol)
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        FAILS.append(label)


def check_eq(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        FAILS.append(label)


def main():
    db.init_db()
    conn = db.connect()
    scale = academics.grade_scale(conn, "primary")

    print("\n== grade scale seeded ==")
    check("A points", scale["A"]["points"], 4.0)
    check("B+ points", scale["B+"]["points"], 3.5)
    check("B points", scale["B"]["points"], 3.0)
    check("D points", scale["D"]["points"], 1.0)
    check_eq("W has no points", scale["W"]["points"], None)
    check_eq("W not in GPA", scale["W"]["counts_gpa"], False)
    check_eq("W earns no credit", scale["W"]["earns_credit"], False)
    check_eq("F counts in GPA", scale["F"]["counts_gpa"], True)
    check("F is a real zero", scale["F"]["points"], 0.0)
    check("C+ points", scale["C+"]["points"], 2.5)
    check_eq("A is verified", scale["A"]["verified"], True)
    # The whole letter scale is confirmed against WCC's published quality-point
    # table, so nothing load-bearing should be flagged as an assumption.
    check_eq("every letter grade is verified",
             [g for g in ("A", "B+", "B", "C+", "C", "D", "F")
              if not scale[g]["verified"]], [])
    # WCC's table has no D+ and no minus grades - seeding one would put a grade
    # the college does not award into the dropdown.
    check_eq("no D+ in the scale", "D+" in scale, False)
    check_eq("no minus grades", [g for g in scale if g.endswith("-")], [])
    check("best grade", academics.max_points(scale), 4.0)

    print("\n== parsing ==")
    terms, unparsed = academics.parse_transcript(TRANSCRIPT)
    check_eq("term count", len(terms), 4)
    check_eq("term names", [t["name"] for t in terms],
             ["Fall 2025", "Spring 2026", "Summer 2026", "Fall 2026"])
    check_eq("course counts", [len(t["courses"]) for t in terms], [6, 5, 2, 5])
    check_eq("statuses", [t["status"] for t in terms],
             ["completed", "completed", "completed", "in_progress"])
    check_eq("a W parsed as a grade", terms[0]["courses"][0]["grade"], "W")
    check_eq("title with digits", terms[2]["courses"][1]["title"],
             "20th Century U.S. History")
    check_eq("code joined", terms[2]["courses"][1]["code"], "HIS 112")
    check_eq("4-credit course", terms[0]["courses"][4]["credits"], 4.0)
    check_eq("ungraded course has no grade", terms[3]["courses"][0]["grade"], "")
    check_eq("slash in title", terms[3]["courses"][4]["title"], "Earth Science Lec/Lab")
    print(f"  info  unparsed lines: {unparsed}")

    print("\n== our arithmetic vs the registrar's ==")
    for t in terms:
        ok, problems = academics._check_against_stated(t, scale)
        label = f"{t['name']} matches printed totals"
        print(f"  {'ok  ' if ok is not False else 'FAIL'}  {label}"
              f"{'' if not problems else ': ' + '; '.join(problems)}")
        if ok is False:
            FAILS.append(label)

    print("\n== cumulative ==")
    every = [c for t in terms for c in t["courses"]]
    cum = academics.tally(every, scale)
    check("attempted", cum["attempted"], 57.0)
    check("earned", cum["earned"], 31.0)
    check("gpa units", cum["gpa_units"], 31.0)
    check("quality points", cum["points"], 100.5)
    check("cumulative GPA (2dp, as printed)", round(cum["gpa"], 2), 3.24)

    print("\n== the questions the section exists to answer ==")
    # 17 scheduled credits this fall, a 3.4 floor for SFS.
    check("GPA needed this fall for a 3.4 cum",
          academics.needed_gpa(100.5, 31.0, 17.0, 3.4), 3.688, tol=0.001)
    # (2.8 * 48 - 100.5) / 17
    check("GPA needed for the 2.8 UB threshold",
          academics.needed_gpa(100.5, 31.0, 17.0, 2.8), 1.994, tol=0.001)
    check("credits of straight A to reach 3.4",
          academics.credits_to_reach(100.5, 31.0, 3.4, 4.0), 8.2, tol=0.05)
    check_eq("already past 2.8, so zero more needed",
             academics.credits_to_reach(100.5, 31.0, 2.8, 4.0), 0.0)
    check_eq("a 4.0 target is unreachable once anything is below it",
             academics.credits_to_reach(100.5, 31.0, 4.0, 4.0), None)
    check_eq("no credits left means no 'needed'",
             academics.needed_gpa(100.5, 31.0, 0.0, 3.4), None)

    print("\n== projections ==")
    # Every fall course at a B would put the term at 3.0.
    fall = [dict(c, projected_grade="B") for c in terms[3]["courses"]]
    proj = academics.tally(fall, scale, projected=True)
    check("projected term GPA at straight B", proj["gpa"], 3.0)
    check("without projections the term has no GPA",
          academics.tally(fall, scale)["gpa"], None)
    check("but the credits still count as attempted",
          academics.tally(fall, scale)["attempted"], 17.0)

    print("\n== an unknown grade is attempted-only, never a zero ==")
    weird = [{"credits": 3.0, "grade": "Z"}]
    check("no GPA from an unknown grade", academics.tally(weird, scale)["gpa"], None)
    check("still attempted", academics.tally(weird, scale)["attempted"], 3.0)
    check("earns nothing", academics.tally(weird, scale)["earned"], 0.0)

    print("\n== exclude_from_gpa ==")
    excluded = [{"credits": 3.0, "grade": "F", "exclude_from_gpa": True},
                {"credits": 3.0, "grade": "A"}]
    check("an excluded F does not drag the average",
          academics.tally(excluded, scale)["gpa"], 4.0)

    conn.close()

    # --------------------------------------------------------- HTTP surface
    print("\n== endpoints ==")
    import app as appmod
    client = appmod.app.test_client()
    H = {"X-API-Token": "testtoken", "X-Profile-Id": "primary"}

    r = client.get("/api/academics", headers=H)
    check_eq("GET /academics", r.status_code, 200)
    check_eq("starts empty", r.get_json()["terms"], [])

    r = client.post("/api/academics/import/preview",
                    json={"text": TRANSCRIPT}, headers=H)
    check_eq("preview 200", r.status_code, 200)
    prev = r.get_json()
    check_eq("preview says the scale is right", prev["scale_looks_right"], True)
    check_eq("preview finds 4 new terms", prev["new_terms"], 4)
    check_eq("preview finds no duplicates", prev["duplicate_terms"], 0)
    check_eq("preview writes nothing",
             client.get("/api/academics", headers=H).get_json()["terms"], [])

    r = client.post("/api/academics/import/commit",
                    json={"text": TRANSCRIPT}, headers=H)
    check_eq("commit 200", r.status_code, 200)
    res = r.get_json()
    check_eq("terms written", res["added_terms"], 4)
    check_eq("courses written", res["added_courses"], 18)

    ov = client.get("/api/academics", headers=H).get_json()
    check("cumulative after import", round(ov["cumulative"]["gpa"], 2), 3.24)
    check_eq("term order is chronological", [t["name"] for t in ov["terms"]],
             ["Fall 2025", "Spring 2026", "Summer 2026", "Fall 2026"])
    check("running cumulative after Spring 2026",
          round(ov["terms"][1]["cumulative_gpa"], 2), 3.54)
    check_eq("C+ is not flagged - nothing uses it",
             ov["unverified_grades_in_use"], [])

    # Re-importing the same text must not silently double the transcript.
    r = client.post("/api/academics/import/commit", json={"text": TRANSCRIPT}, headers=H)
    check_eq("re-import skips duplicates", r.get_json()["skipped_terms"], 4)
    check_eq("re-import adds nothing", r.get_json()["added_courses"], 0)
    check_eq("still 4 terms",
             len(client.get("/api/academics", headers=H).get_json()["terms"]), 4)

    print("\n== goals ==")
    goals = {g["name"]: g for g in ov["goals"]}
    sfs = goals["a scholarship programme"]
    check_eq("SFS not met", sfs["met"], False)
    check("SFS needs this fall", sfs["needed_gpa"], 3.688, tol=0.002)
    check_eq("SFS is reachable this fall", sfs["reachable_in_scheduled"], True)
    check("SFS remaining units", sfs["remaining_units"], 17.0)
    ub = goals["UB CSE transfer"]
    check_eq("UB threshold already met", ub["met"], True)
    core = goals["UB core courses"]
    check_eq("core goal has no tagged courses yet", core["current_gpa"], None)

    print("\n== forecast ==")
    r = client.get("/api/academics/forecast?credits=17&at=3.7&target=3.4", headers=H)
    f = r.get_json()
    # (100.5 + 3.7 * 17) / 48 - just short of the 3.4 floor, which is the
    # whole point of the section: a strong term still does not quite get there.
    check("17 credits at 3.7 lands at", f["resulting_gpa"], 3.404, tol=0.002)
    check("needed for 3.4", f["needed_gpa"], 3.688, tol=0.002)
    check("best possible over 17 credits", f["max_possible_gpa"], 3.510, tol=0.002)
    check("defaults to the scheduled credits",
          client.get("/api/academics/forecast?target=3.4",
                     headers=H).get_json()["credits"], 17.0)

    print("\n== editing a grade re-derives everything ==")
    fall_term = [t for t in ov["terms"] if t["name"] == "Fall 2026"][0]
    course = fall_term["courses"][0]
    r = client.patch(f"/api/academics/courses/{course['id']}",
                     json={"grade": "A"}, headers=H)
    check_eq("patch 200", r.status_code, 200)
    after = client.get("/api/academics", headers=H).get_json()
    check("cumulative moved after one A", round(after["cumulative"]["gpa"], 3),
          round((100.5 + 12) / 34, 3), tol=0.001)
    check("remaining units dropped by 3",
          [g for g in after["goals"] if g["name"] == "a scholarship programme"][0]["remaining_units"],
          14.0)
    client.patch(f"/api/academics/courses/{course['id']}", json={"grade": ""}, headers=H)

    print("\n== tags scope a goal ==")
    for c in fall_term["courses"]:
        if c["code"] == "MATH 140":
            client.patch(f"/api/academics/courses/{c['id']}",
                         json={"tags": ["ub-core"], "projected_grade": "A"}, headers=H)
    after = client.get("/api/academics", headers=H).get_json()
    core = [g for g in after["goals"] if g["name"] == "UB core courses"][0]
    check("core goal now sees 4 credits", core["remaining_units"], 4.0)
    check("core projected GPA", core["projected_gpa"], 4.0)
    check_eq("tag surfaced", "ub-core" in after["tags"], True)

    print("\n== the scale is editable and everything follows ==")
    r = client.put("/api/academics/scale",
                   json={"grades": [{"grade": "B", "points": 3.0, "counts_gpa": True,
                                     "earns_credit": True, "verified": True},
                                    {"grade": "A-", "points": 3.7, "counts_gpa": True,
                                     "earns_credit": True, "verified": True}]},
                   headers=H)
    check_eq("scale PUT 200", r.status_code, 200)
    check_eq("A- added", any(g["grade"] == "A-" for g in r.get_json()), True)
    # Break the scale on purpose: the import check must now object.
    client.put("/api/academics/scale",
               json={"grades": [{"grade": "B+", "points": 3.3, "counts_gpa": True,
                                 "earns_credit": True, "verified": True}]}, headers=H)
    prev = client.post("/api/academics/import/preview",
                       json={"text": TRANSCRIPT}, headers=H).get_json()
    check_eq("a wrong B+ is caught by the transcript check",
             prev["scale_looks_right"], False)
    check_eq("and named", prev["mismatched_terms"], 1)
    print(f"  info  reported: {[t['discrepancies'] for t in prev['terms'] if t['discrepancies']]}")

    print("\n== profile scoping ==")
    ph = {"X-API-Token": "testtoken", "X-Profile-Id": "partner"}
    check_eq("partner sees no terms",
             client.get("/api/academics", headers=ph).get_json()["terms"], [])
    check_eq("partner has a scale",
             len(client.get("/api/academics/scale", headers=ph).get_json()) > 0, True)
    check_eq("partner has no seeded goals",
             client.get("/api/academics/goals", headers=ph).get_json(), [])
    check_eq("partner cannot patch his course",
             client.patch(f"/api/academics/courses/{course['id']}",
                          json={"grade": "F"}, headers=ph).status_code, 404)

    print("\n== repeats: the ECON 101 retake ==")
    # Restore the scale we deliberately broke above before measuring anything.
    client.put("/api/academics/scale",
               json={"grades": [{"grade": "B+", "points": 3.5, "counts_gpa": True,
                                 "earns_credit": True, "verified": True}]}, headers=H)
    ov = client.get("/api/academics", headers=H).get_json()
    summer = [t for t in ov["terms"] if t["name"] == "Summer 2026"][0]
    fall = [t for t in ov["terms"] if t["name"] == "Fall 2026"][0]
    econ_d = [c for c in summer["courses"] if c["code"] == "ECON 101"][0]
    check_eq("the D is on record", econ_d["grade"], "D")

    r = client.post("/api/academics/courses",
                    json={"term_id": fall["id"], "code": "ECON 101",
                          "title": "Macroeconomics", "credits": 3,
                          "replaces_course_id": econ_d["id"]}, headers=H)
    check_eq("retake created", r.status_code, 201)
    retake_id = r.get_json()["id"]

    ov = client.get("/api/academics", headers=H).get_json()
    # Nothing about today's record may move: the retake is not graded.
    check("cumulative unchanged while the retake is ungraded",
          round(ov["cumulative"]["gpa"], 3), 3.242, tol=0.001)
    check("Summer 2026 term GPA unchanged",
          [t for t in ov["terms"] if t["name"] == "Summer 2026"][0]["totals"]["gpa"], 2.0)
    check_eq("the D is flagged as pending replacement",
             [c for c in ov["repeats"]["pending"]][0]["code"], "ECON 101")
    check_eq("nothing retired yet", ov["repeats"]["applied"], [])

    # ...but the forecast must already net out the pending removal.
    sfs = [g for g in ov["goals"] if g["name"] == "a scholarship programme"][0]
    check("fall is now 20 credits", sfs["remaining_units"], 20.0)
    # (3.4 * (31 - 3 + 20) - (100.5 - 3)) / 20
    check("needed drops because the D leaves with the retake",
          sfs["needed_gpa"], 3.285, tol=0.002)
    check_eq("still reachable", sfs["reachable_in_scheduled"], True)

    f = client.get("/api/academics/forecast?target=3.4", headers=H).get_json()
    check("forecast sees 20 scheduled credits", f["scheduled_ungraded_credits"], 20.0)
    check("forecast nets out the pending D - units", f["pending_replacement_units"], 3.0)
    check("forecast nets out the pending D - points", f["pending_replacement_points"], 3.0)
    check("forecast needed matches the goal", f["needed_gpa"], 3.285, tol=0.002)
    check("best case with the retake", f["max_possible_gpa"], 3.698, tol=0.002)

    print("\n-- now grade the retake an A --")
    client.patch(f"/api/academics/courses/{retake_id}", json={"grade": "A"}, headers=H)
    ov = client.get("/api/academics", headers=H).get_json()
    # points 100.5 - 3 (D out) + 12 (A in) = 109.5 over 31 - 3 + 3 = 31 units
    check("cumulative after the replacement lands",
          ov["cumulative"]["gpa"], round(109.5 / 31, 3), tol=0.001)
    # Earned holds at 31: the D surrenders its 3 credits exactly as the retake
    # gains 3, which is what "a repeated course is worth its credits once"
    # means. Attempted goes 57 -> 60 because both sittings did happen.
    check("credits counted once, not twice", ov["cumulative"]["earned"], 31.0)
    check("both attempts still count as attempted", ov["cumulative"]["attempted"], 60.0)
    check_eq("the D is now retired", econ_d["id"] in ov["repeats"]["applied"], True)
    check_eq("no longer pending", ov["repeats"]["pending"], [])

    summer = [t for t in ov["terms"] if t["name"] == "Summer 2026"][0]
    check("Summer 2026 keeps the GPA it actually had", summer["totals"]["gpa"], 2.0)
    check_eq("but the D is marked replaced",
             [c for c in summer["courses"] if c["code"] == "ECON 101"][0]["superseded"], True)
    check("running cumulative retires it from Fall 2026 onward",
          [t for t in ov["terms"] if t["name"] == "Fall 2026"][0]["cumulative_gpa"],
          round(109.5 / 31, 3), tol=0.001)
    check("Summer's running cumulative is untouched history",
          summer["cumulative_gpa"], 3.242, tol=0.001)

    print("\n-- and if the college averages instead of replacing --")
    client.patch(f"/api/academics/courses/{retake_id}",
                 json={"replaces_course_id": None}, headers=H)
    ov = client.get("/api/academics", headers=H).get_json()
    # Both count: (100.5 + 12) / (31 + 3)
    check("both attempts average together",
          ov["cumulative"]["gpa"], round(112.5 / 34, 3), tol=0.001)
    client.patch(f"/api/academics/courses/{retake_id}",
                 json={"replaces_course_id": econ_d["id"], "grade": ""}, headers=H)

    print("\n-- repeat links are validated --")
    check_eq("a course cannot replace itself",
             client.patch(f"/api/academics/courses/{retake_id}",
                          json={"replaces_course_id": retake_id},
                          headers=H).status_code, 400)
    check_eq("cannot replace a course in another profile",
             client.post("/api/academics/courses",
                         json={"term_id": fall["id"], "code": "X 1",
                               "replaces_course_id": 999999}, headers=H).status_code, 400)
    check_eq("mutual replacement is refused",
             client.patch(f"/api/academics/courses/{econ_d['id']}",
                          json={"replaces_course_id": retake_id},
                          headers=H).status_code, 400)

    print("\n-- deleting the old attempt un-links rather than cascading --")
    ov_before = client.get("/api/academics", headers=H).get_json()
    n_before = sum(len(t["courses"]) for t in ov_before["terms"])
    client.delete(f"/api/academics/courses/{econ_d['id']}", headers=H)
    ov = client.get("/api/academics", headers=H).get_json()
    check_eq("only the deleted course is gone",
             sum(len(t["courses"]) for t in ov["terms"]), n_before - 1)
    check_eq("the retake survives",
             any(c["id"] == retake_id for t in ov["terms"] for c in t["courses"]), True)

    print("\n== auth ==")
    check_eq("no token is refused",
             client.get("/api/academics").status_code, 401)

    print("\n" + "=" * 58)
    if FAILS:
        print(f"{len(FAILS)} FAILURES:")
        for f in FAILS:
            print("  -", f)
    else:
        print("all checks passed")
    print("=" * 58)
    return 1 if FAILS else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(_tmp, ignore_errors=True)
