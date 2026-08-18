"""
Health metrics: ingest, storage, and the Google Health API connector.

Two deliberately separate halves.

The ingest half is provider-agnostic. `record()` takes a metric name, a
value and a date, and upserts one row. Anything can call it - the Google
connector below, a Tasker task, a Home Assistant automation, an iOS
Shortcut, curl. That matters because the health API landscape keeps moving:
Google Fit's REST API is being retired, Health Connect is on-device only
with no cloud API at all, and the legacy Fitbit Web API sunsets on
2026-09-30. Whatever survives, the storage layer does not have to change.

The connector half targets the Google Health API (health.googleapis.com,
v4), which is what Fitbit/Pixel data moves to. It is standard Google OAuth:
one-hour access tokens plus a long-lived refresh token, so the refresh token
is the thing worth persisting.

Nothing here runs unless OPSDECK_GOOGLE_CLIENT_ID / _SECRET are set - the
app works fine with only manual or pushed data.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

from db import connect
from recurrence import today_local, now_local

CLIENT_ID = os.environ.get("OPSDECK_GOOGLE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("OPSDECK_GOOGLE_CLIENT_SECRET", "")
# Must exactly match an Authorised redirect URI on the OAuth client.
REDIRECT_URI = os.environ.get(
    "OPSDECK_GOOGLE_REDIRECT_URI",
    "https://your-host.example.com/api/health/callback",
)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
HEALTH_BASE = "https://health.googleapis.com/v4"

# Exact scope strings from https://developers.google.com/health/scopes.
# The prefix is `googlehealth.`, not `health.` - the latter is rejected with
# Error 400: invalid_scope before the consent screen ever renders.
#
# Read-only throughout: this app displays health data, it never writes back,
# so requesting a writeonly scope would be asking for permission we have no
# use for. Steps/activity and heart rate both live under
# activity_and_fitness and health_metrics_and_measurements respectively.
SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
]

PROVIDER = "google_health"

# The metrics we surface. Everything else the API returns is ignored rather
# than stored speculatively - an empty column is worse than no column.
METRICS = {
    "steps": {"label": "Steps", "unit": "steps", "kind": "sum"},
    "distance_km": {"label": "Distance", "unit": "km", "kind": "sum"},
    "active_minutes": {"label": "Active", "unit": "min", "kind": "sum"},
    "exercise_minutes": {"label": "Exercise", "unit": "min", "kind": "sum"},
    "sleep_minutes": {"label": "Sleep", "unit": "min", "kind": "sum"},
    # Kept alongside sleep so the gap between them is visible - that gap is
    # time awake in bed, and its ratio is sleep efficiency.
    "time_in_bed_minutes": {"label": "In bed", "unit": "min", "kind": "sum"},
    "calories": {"label": "Calories", "unit": "kcal", "kind": "sum"},
    "workout_hr": {"label": "Workout HR", "unit": "bpm", "kind": "avg"},
    "weight_kg": {"label": "Weight", "unit": "kg", "kind": "last"},
}


def configured():
    """Whether a Google OAuth client has been supplied."""
    return bool(CLIENT_ID and CLIENT_SECRET)


# ------------------------------------------------------------- storage

def record(conn, profile_id, metric, value, local_date=None, unit=None,
           source="manual"):
    """
    Upsert one metric for one day. Idempotent by (profile, metric, date,
    source): re-syncing a date range overwrites rather than duplicating,
    which is required because a day's step count keeps changing until the
    day is over.
    """
    if metric not in METRICS and not metric.startswith("custom_"):
        raise ValueError(f"unknown metric '{metric}'")
    local_date = local_date or today_local().isoformat()
    unit = unit if unit is not None else METRICS.get(metric, {}).get("unit", "")
    conn.execute(
        "INSERT INTO health_metrics (profile_id,metric,value,unit,local_date,source) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(profile_id,metric,local_date,source) DO UPDATE SET "
        "  value=excluded.value, unit=excluded.unit, recorded_at=datetime('now')",
        (profile_id, metric, float(value), unit, local_date, source),
    )


def series(conn, profile_id, days=30, metric=None):
    """Rows for the last `days` days, newest last."""
    start = (today_local() - timedelta(days=days - 1)).isoformat()
    sql = ("SELECT metric,value,unit,local_date,source FROM health_metrics "
           "WHERE profile_id=? AND local_date>=?")
    params = [profile_id, start]
    if metric:
        sql += " AND metric=?"
        params.append(metric)
    sql += " ORDER BY local_date, metric"
    return [dict(r) for r in conn.execute(sql, params)]


def summary(conn, profile_id, days=7):
    """
    Today's value per metric plus a trailing average over the window, so the
    UI can say "8,400 steps, a bit under your usual" rather than just a
    number with no reference point.
    """
    today = today_local().isoformat()
    start = (today_local() - timedelta(days=days)).isoformat()
    out = {}
    for m, meta in METRICS.items():
        row = conn.execute(
            "SELECT value,unit FROM health_metrics "
            "WHERE profile_id=? AND metric=? AND local_date=? "
            "ORDER BY recorded_at DESC LIMIT 1",
            (profile_id, m, today),
        ).fetchone()
        avg = conn.execute(
            "SELECT AVG(value) FROM health_metrics "
            "WHERE profile_id=? AND metric=? AND local_date>=? AND local_date<?",
            (profile_id, m, start, today),
        ).fetchone()[0]
        if row is None and avg is None:
            continue          # never recorded - don't show an empty tile
        out[m] = {
            "label": meta["label"],
            "unit": meta["unit"],
            "today": round(row["value"], 2) if row else None,
            "avg": round(avg, 2) if avg is not None else None,
        }
    return out


# ------------------------------------------------------------- analytics

def _median(values):
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def stats(conn, profile_id, metric, days=30):
    """
    Descriptive stats for one metric over a window.

    Includes the two halves of the window compared against each other, which
    answers "am I trending up or down" far better than a single average -
    and a coverage count, because an average over 3 of 30 days deserves less
    trust than one over 28.
    """
    start = (today_local() - timedelta(days=days - 1)).isoformat()
    rows = [dict(r) for r in conn.execute(
        "SELECT local_date, value FROM health_metrics "
        "WHERE profile_id=? AND metric=? AND local_date>=? ORDER BY local_date",
        (profile_id, metric, start),
    )]
    if not rows:
        return {"metric": metric, "days": days, "count": 0}

    values = [r["value"] for r in rows]
    best = max(rows, key=lambda r: r["value"])
    worst = min(rows, key=lambda r: r["value"])

    # Split the window in half and compare, for a direction of travel.
    half = len(rows) // 2
    older = [r["value"] for r in rows[:half]]
    newer = [r["value"] for r in rows[half:]]
    trend = None
    if older and newer:
        a, b = sum(older) / len(older), sum(newer) / len(newer)
        if a:
            trend = round((b - a) / a * 100, 1)

    return {
        "metric": metric,
        "label": METRICS.get(metric, {}).get("label", metric),
        "unit": METRICS.get(metric, {}).get("unit", ""),
        "days": days,
        "count": len(rows),
        "coverage_pct": round(len(rows) / days * 100),
        "total": round(sum(values), 2),
        "avg": round(sum(values) / len(values), 2),
        "median": round(_median(values), 2),
        "min": round(min(values), 2),
        "max": round(max(values), 2),
        "best_day": {"date": best["local_date"], "value": round(best["value"], 2)},
        "worst_day": {"date": worst["local_date"], "value": round(worst["value"], 2)},
        "trend_pct": trend,
        "first_seen": rows[0]["local_date"],
        "last_seen": rows[-1]["local_date"],
    }


def by_weekday(conn, profile_id, metric, days=90):
    """
    Average per day of the week. Surfaces patterns a flat timeline hides -
    that Saturdays are the big step days, or that Sunday sleep is the
    outlier.
    """
    start = (today_local() - timedelta(days=days - 1)).isoformat()
    buckets = {i: [] for i in range(7)}
    for r in conn.execute(
        "SELECT local_date, value FROM health_metrics "
        "WHERE profile_id=? AND metric=? AND local_date>=?",
        (profile_id, metric, start),
    ):
        try:
            wd = date.fromisoformat(r["local_date"]).weekday()   # Mon=0
        except ValueError:
            continue
        buckets[wd].append(r["value"])

    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return [
        {"weekday": names[i], "n": len(v),
         "avg": round(sum(v) / len(v), 2) if v else None}
        for i, v in buckets.items()
    ]


def by_source(conn, profile_id, metric=None, days=90):
    """Which platform actually supplied the readings."""
    start = (today_local() - timedelta(days=days - 1)).isoformat()
    sql = ("SELECT source, COUNT(*) AS n, MIN(local_date) AS first, MAX(local_date) AS last "
           "FROM health_metrics WHERE profile_id=? AND local_date>=?")
    params = [profile_id, start]
    if metric:
        sql += " AND metric=?"
        params.append(metric)
    sql += " GROUP BY source ORDER BY n DESC"
    return [dict(r) for r in conn.execute(sql, params)]


def raw(conn, profile_id, metric=None, source=None, start=None, end=None, limit=500):
    """Every stored row, filterable. The escape hatch for 'let me see it all'."""
    sql = ("SELECT id, metric, value, unit, local_date, source, recorded_at "
           "FROM health_metrics WHERE profile_id=?")
    params = [profile_id]
    if metric:
        sql += " AND metric=?"; params.append(metric)
    if source:
        sql += " AND source=?"; params.append(source)
    if start:
        sql += " AND local_date>=?"; params.append(start)
    if end:
        sql += " AND local_date<=?"; params.append(end)
    sql += " ORDER BY local_date DESC, metric LIMIT ?"
    params.append(min(int(limit), 5000))
    return [dict(r) for r in conn.execute(sql, params)]


def tracked_metrics(conn, profile_id):
    """Which metrics actually have data, so the UI never offers a dead tab."""
    return [r["metric"] for r in conn.execute(
        "SELECT DISTINCT metric FROM health_metrics WHERE profile_id=? ORDER BY metric",
        (profile_id,),
    )]


def detail(conn, profile_id, metric, days=30):
    """Everything about one metric in a single call."""
    return {
        "stats": stats(conn, profile_id, metric, days),
        "series": series(conn, profile_id, days, metric),
        "weekday": by_weekday(conn, profile_id, metric, max(days, 28)),
        "sources": by_source(conn, profile_id, metric, days),
    }


# ------------------------------------------------------------- oauth

def _save_tokens(conn, profile_id, data, keep_refresh=None):
    expires_at = (now_local() + timedelta(seconds=int(data.get("expires_in", 3600)) - 60))
    conn.execute(
        "INSERT INTO oauth_tokens (provider,profile_id,access_token,refresh_token,expires_at,scope,updated_at) "
        "VALUES (?,?,?,?,?,?,datetime('now')) "
        "ON CONFLICT(provider,profile_id) DO UPDATE SET "
        "  access_token=excluded.access_token, "
        "  refresh_token=CASE WHEN excluded.refresh_token != '' THEN excluded.refresh_token "
        "                     ELSE oauth_tokens.refresh_token END, "
        "  expires_at=excluded.expires_at, scope=excluded.scope, updated_at=datetime('now')",
        (PROVIDER, profile_id, data.get("access_token", ""),
         data.get("refresh_token", keep_refresh or ""),
         expires_at.strftime("%Y-%m-%d %H:%M:%S"), data.get("scope", "")),
    )


def auth_url(profile_id, state):
    """
    Consent URL. access_type=offline + prompt=consent is what actually
    returns a refresh token - without both, a re-authorisation silently
    yields only a one-hour access token and sync dies an hour later.
    """
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def _post_form(url, fields):
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.loads(res.read().decode())


def exchange_code(conn, profile_id, code):
    data = _post_form(TOKEN_URL, {
        "code": code, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI, "grant_type": "authorization_code",
    })
    _save_tokens(conn, profile_id, data)
    return data


def access_token(conn, profile_id):
    """Return a valid access token, refreshing if it has expired."""
    row = conn.execute(
        "SELECT * FROM oauth_tokens WHERE provider=? AND profile_id=?",
        (PROVIDER, profile_id),
    ).fetchone()
    if not row:
        return None

    if row["expires_at"]:
        try:
            if datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S") > now_local().replace(tzinfo=None):
                return row["access_token"]
        except ValueError:
            pass

    if not row["refresh_token"]:
        return None
    data = _post_form(TOKEN_URL, {
        "refresh_token": row["refresh_token"], "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET, "grant_type": "refresh_token",
    })
    _save_tokens(conn, profile_id, data, keep_refresh=row["refresh_token"])
    conn.commit()
    return data.get("access_token")


def connected(conn, profile_id):
    row = conn.execute(
        "SELECT refresh_token FROM oauth_tokens WHERE provider=? AND profile_id=?",
        (PROVIDER, profile_id),
    ).fetchone()
    return bool(row and row["refresh_token"])


def disconnect(conn, profile_id):
    conn.execute("DELETE FROM oauth_tokens WHERE provider=? AND profile_id=?",
                 (PROVIDER, profile_id))


# ------------------------------------------------------------- sync

# The v4 data types that actually exist and support list(). Verified
# against a live account - `calories`, `active_minutes`, `heart_rate` and
# friends are rejected with "Invalid data type ID", and the daily roll-up
# methods 404 on GET, so everything below is derived by paginating raw
# dataPoints and bucketing them by their own civil (local) date.
DATA_TYPES = ("steps", "distance", "sleep", "weight", "exercise")

MAX_PAGES = 40          # ~4000 points; a hard stop so a bad window can't spin
PAGE_SIZE = 200


def _get(url, token):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=45) as res:
        return json.loads(res.read().decode())


def _civil_date(obj):
    """
    The point's own local date, as the device recorded it.

    Preferred over converting startTime, because a workout at 00:30 local
    belongs to that day even though its UTC timestamp says otherwise.
    Falls back to offsetting the UTC instant when no civil time is given.
    """
    for holder in ("civilStartTime", "civilTime"):
        civil = obj.get(holder) or {}
        d = civil.get("date") or {}
        if d.get("year"):
            return f"{d['year']:04d}-{d['month']:02d}-{d['day']:02d}"

    stamp = obj.get("startTime") or obj.get("physicalTime")
    if not stamp:
        return None
    try:
        dt = datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    offset = obj.get("startUtcOffset") or obj.get("utcOffset") or "0s"
    try:
        dt += timedelta(seconds=int(str(offset).rstrip("s")))
    except ValueError:
        pass
    return dt.date().isoformat()


def _wake_date(interval):
    """
    The local date a sleep session *ended*. Mirrors _civil_date but reads the
    end of the interval, because sleep is credited to the morning you wake.
    """
    civil = interval.get("civilEndTime") or {}
    d = civil.get("date") or {}
    if d.get("year"):
        return f"{d['year']:04d}-{d['month']:02d}-{d['day']:02d}"

    stamp = interval.get("endTime")
    if not stamp:
        return _civil_date(interval)
    try:
        dt = datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return _civil_date(interval)
    offset = interval.get("endUtcOffset") or interval.get("startUtcOffset") or "0s"
    try:
        dt += timedelta(seconds=int(str(offset).rstrip("s")))
    except ValueError:
        pass
    return dt.date().isoformat()


# Stage types that count as actually asleep. An allowlist rather than a
# denylist: an unrecognised future stage should not silently inflate the
# total, which is the failure mode that started this.
#   FITBIT     reports STAGES  -> LIGHT / DEEP / REM / AWAKE
#   HEALTH_KIT reports CLASSIC -> ASLEEP / AWAKE
ASLEEP_STAGES = {"ASLEEP", "LIGHT", "DEEP", "REM", "SLEEPING",
                 "ASLEEP_CORE", "ASLEEP_DEEP", "ASLEEP_REM", "ASLEEP_UNSPECIFIED"}


def _asleep_minutes(sleep_obj):
    """
    Minutes actually asleep, not minutes in bed.

    The session interval spans lights-out to getting up, which includes time
    awake - 56 minutes of it on one measured night, which is the difference
    between the watch saying 7h42m and this app saying 8h38m. When the
    provider breaks the session into stages, sum only the sleeping ones.

    Falls back to the raw interval when no stages are given, because a
    session with no breakdown is still better information than nothing.
    """
    stages = sleep_obj.get("stages") or []
    if not stages:
        return _duration_seconds(sleep_obj.get("interval") or {}) / 60.0

    asleep = sum(
        _duration_seconds(st) for st in stages
        if str(st.get("type", "")).upper() in ASLEEP_STAGES
    )
    # A session whose stages are all unrecognised would otherwise report
    # zero sleep; the interval is the safer answer there.
    if asleep <= 0:
        return _duration_seconds(sleep_obj.get("interval") or {}) / 60.0
    return asleep / 60.0


def _duration_seconds(interval):
    """Length of an interval in seconds, from its UTC endpoints."""
    try:
        a = datetime.strptime(interval["startTime"][:19], "%Y-%m-%dT%H:%M:%S")
        b = datetime.strptime(interval["endTime"][:19], "%Y-%m-%dT%H:%M:%S")
        return max(0.0, (b - a).total_seconds())
    except (KeyError, ValueError, TypeError):
        return 0.0


def _num(value):
    """Values arrive as strings ("7"), numbers, or durations ("3480s")."""
    if value is None:
        return None
    try:
        return float(str(value).rstrip("s"))
    except (ValueError, TypeError):
        return None


def _fold(point, dtype, totals, lasts):
    """
    Reduce one raw data point into per-(metric, day, platform) accumulators.

    Steps and distance arrive as per-minute deltas, sleep and exercise as
    sessions, weight as instantaneous samples - so each needs its own rule
    rather than a single generic sum.

    Platform is part of the key because a phone commonly feeds Google Health
    from more than one source at once (a Fitbit watch AND Apple HealthKit,
    say). Those sources describe the *same* day, so summing them together
    double-counts: it produced 35,000-step days and a 24-hour night's sleep
    before this was keyed apart. Reconciliation happens in _reduce().
    """
    obj = point.get(dtype) or {}
    interval = obj.get("interval") or {}
    plat = (point.get("dataSource") or {}).get("platform") or "UNKNOWN"

    if dtype == "sleep":
        # A night belongs to the morning you wake, not the evening you lay
        # down - the convention every sleep tracker uses. Bucketing by start
        # puts a 23:47 bedtime on the same day as that morning's wake-up,
        # stacking two separate nights onto one date (seen live: 17h41m).
        day = _wake_date(interval)
    else:
        day = _civil_date(interval) or _civil_date(obj.get("sampleTime") or {})
    if not day:
        return

    def add(metric, amount):
        if amount:
            key = (metric, day, plat)
            totals[key] = totals.get(key, 0.0) + amount

    if dtype == "steps":
        add("steps", _num(obj.get("count")))

    elif dtype == "distance":
        metres = _num(obj.get("distanceMeters") or obj.get("distance"))
        add("distance_km", metres / 1000.0 if metres else None)

    elif dtype == "sleep":
        # Time asleep, not time in bed - see _asleep_minutes.
        add("sleep_minutes", _asleep_minutes(obj))
        add("time_in_bed_minutes", _duration_seconds(interval) / 60.0)

    elif dtype == "weight":
        grams = _num(obj.get("weightGrams"))
        if grams:
            # Points arrive newest-first, so the first one seen for a day is
            # the latest reading of that day.
            lasts.setdefault(("weight_kg", day, plat), grams / 1000.0)

    elif dtype == "exercise":
        secs = _num(obj.get("activeDuration")) or _duration_seconds(interval)
        add("exercise_minutes", secs / 60.0 if secs else None)
        summary = obj.get("metricsSummary") or {}
        add("calories", _num(summary.get("caloriesKcal")))
        add("active_minutes", _num(summary.get("activeZoneMinutes")))
        hr = _num(summary.get("averageHeartRateBeatsPerMinute"))
        if hr:
            lasts.setdefault(("workout_hr", day, plat), hr)


def _reduce(totals, lasts):
    """
    Collapse per-platform figures into one value per (metric, day).

    Takes the maximum rather than the sum. Each platform reports its own
    complete view of the day, so the largest is the most complete one -
    adding them would count the same steps or the same night twice. Max also
    degrades sensibly when only one source is present, which is the common
    case.
    """
    out = {}
    for (metric, day, _plat), value in totals.items():
        key = (metric, day)
        out[key] = max(out.get(key, 0.0), value)
    for (metric, day, _plat), value in lasts.items():
        key = (metric, day)
        out.setdefault(key, value)
    return out


def sync(conn, profile_id, days=7):
    """
    Pull the last `days` days into health_metrics.

    There is no server-side date filter on dataPoints.list - `filter` exists
    but rejects every time-based syntax, and the roll-up methods 404 - so
    this pages through newest-first and stops once a type's points fall
    before the window. Aggregation happens here.

    One failing data type never aborts the run: errors are collected and
    reported so a partial sync still writes what it could.
    """
    if not configured():
        return {"ok": False, "error": "no Google OAuth client configured"}
    token = access_token(conn, profile_id)
    if not token:
        return {"ok": False, "error": "not connected - authorise first"}

    window_start = (today_local() - timedelta(days=days - 1)).isoformat()
    totals, lasts, errors, scanned = {}, {}, [], 0

    for dtype in DATA_TYPES:
        page_token, pages = None, 0
        while pages < MAX_PAGES:
            url = (f"{HEALTH_BASE}/users/me/dataTypes/{dtype}"
                   f"/dataPoints?pageSize={PAGE_SIZE}")
            if page_token:
                url += "&pageToken=" + urllib.parse.quote(page_token)
            try:
                payload = _get(url, token)
            except urllib.error.HTTPError as e:
                errors.append(f"{dtype}: HTTP {e.code}")
                break
            except (urllib.error.URLError, ValueError) as e:
                errors.append(f"{dtype}: {e}")
                break

            points = payload.get("dataPoints", [])
            scanned += len(points)
            oldest = None
            for point in points:
                _fold(point, dtype, totals, lasts)
                obj = point.get(dtype) or {}
                day = (_civil_date(obj.get("interval") or {})
                       or _civil_date(obj.get("sampleTime") or {}))
                if day and (oldest is None or day < oldest):
                    oldest = day

            page_token = payload.get("nextPageToken")
            pages += 1
            # Newest-first: once a whole page predates the window there is
            # nothing left worth paging for.
            if not page_token or (oldest and oldest < window_start):
                break

    written = 0
    for (metric, day), value in _reduce(totals, lasts).items():
        if day < window_start:
            continue
        try:
            record(conn, profile_id, metric, round(value, 2),
                   local_date=day, source=PROVIDER)
            written += 1
        except (ValueError, TypeError) as e:
            errors.append(f"{metric} {day}: {e}")

    conn.commit()
    return {"ok": True, "written": written, "days": days, "scanned": scanned,
            "errors": errors, "synced_at": now_local().isoformat()}
