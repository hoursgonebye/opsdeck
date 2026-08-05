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
from datetime import datetime, timedelta

from db import connect
from recurrence import today_local, now_local

CLIENT_ID = os.environ.get("OPSDECK_GOOGLE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("OPSDECK_GOOGLE_CLIENT_SECRET", "")
# Must exactly match an Authorised redirect URI on the OAuth client.
REDIRECT_URI = os.environ.get(
    "OPSDECK_GOOGLE_REDIRECT_URI",
    "https://opsdeck.example.ts.net/api/health/callback",
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
    "calories": {"label": "Calories", "unit": "kcal", "kind": "sum"},
    "resting_hr": {"label": "Resting HR", "unit": "bpm", "kind": "avg"},
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

# Google Health data type -> (our metric, converter). Kept as a table so a
# renamed upstream type is a one-line change.
DATA_TYPES = {
    "com.google.step_count.delta": ("steps", lambda v: v),
    "com.google.distance.delta": ("distance_km", lambda v: v / 1000.0),
    "com.google.active_minutes": ("active_minutes", lambda v: v),
    "com.google.calories.expended": ("calories", lambda v: v),
    "com.google.sleep.segment": ("sleep_minutes", lambda v: v),
    "com.google.heart_rate.resting": ("resting_hr", lambda v: v),
    "com.google.weight": ("weight_kg", lambda v: v),
}


def _get(url, token):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=45) as res:
        return json.loads(res.read().decode())


def sync(conn, profile_id, days=7):
    """
    Pull the last `days` days of daily roll-ups into health_metrics.

    Returns a dict describing what happened. Never raises for an individual
    data type: one unavailable metric should not abort the whole sync, so
    failures are collected and reported.
    """
    if not configured():
        return {"ok": False, "error": "no Google OAuth client configured"}
    token = access_token(conn, profile_id)
    if not token:
        return {"ok": False, "error": "not connected - authorise first"}

    start = (today_local() - timedelta(days=days - 1)).isoformat()
    end = today_local().isoformat()
    written, errors = 0, []

    for dtype, (metric, convert) in DATA_TYPES.items():
        url = (f"{HEALTH_BASE}/users/me/dataTypes/{urllib.parse.quote(dtype)}"
               f"/dataPoints:dailyRollUp"
               f"?startDate={start}&endDate={end}")
        try:
            payload = _get(url, token)
        except urllib.error.HTTPError as e:
            # 403/404 usually means this scope was not granted or the device
            # does not produce that type - not fatal, just unavailable.
            errors.append(f"{metric}: HTTP {e.code}")
            continue
        except (urllib.error.URLError, ValueError) as e:
            errors.append(f"{metric}: {e}")
            continue

        for point in payload.get("dataPoints", payload.get("rollUps", [])):
            day = (point.get("date") or point.get("localDate") or "")[:10]
            raw = point.get("value", point.get("total"))
            if isinstance(raw, dict):
                raw = raw.get("doubleValue", raw.get("intValue", raw.get("value")))
            if not day or raw is None:
                continue
            try:
                record(conn, profile_id, metric, convert(float(raw)),
                       local_date=day, source=PROVIDER)
                written += 1
            except (ValueError, TypeError) as e:
                errors.append(f"{metric} {day}: {e}")

    conn.commit()
    return {"ok": True, "written": written, "days": days,
            "errors": errors, "synced_at": now_local().isoformat()}
