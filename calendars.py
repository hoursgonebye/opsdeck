"""
Subscribed calendar feeds (iCalendar / .ics over HTTP).

Pulls a read-only external calendar - a work roster, a class timetable, a
shared family calendar - into the events table so it shows up alongside
everything else, including on the merged Us view.

Two things about real-world feeds shape this module:

  1. **UIDs cannot be trusted to be stable.** The Kronos roster this was
     built against regenerates every UID on every fetch, embedding the
     request timestamp. Deduplicating on UID would therefore create a
     complete set of duplicates on every single sync. Identity is instead a
     hash of the content that actually defines the event (start, end,
     title), which is stable across fetches by construction.

  2. **Feeds are the source of truth, and they change.** Shifts get moved
     and cancelled, not just added. So a sync replaces the feed's events
     within the window the feed covers, rather than merging into them - a
     cancelled shift has to be able to disappear.

Feed events are read-only in the UI: editing one would be silently undone
by the next sync, which is worse than not offering it.

Syncing runs on a timer (see start_auto_sync at the bottom) as well as on
demand, because a roster that only refreshes when you remember to press a
button is a roster you cannot trust.
"""
import hashlib
import os
import re
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from recurrence import TZ, fmt_dt

USER_AGENT = "OpsDeck/1.0 (+self-hosted calendar subscriber)"
MAX_BYTES = 5 * 1024 * 1024      # a roster feed is kilobytes; refuse a firehose
FETCH_TIMEOUT = 45

# How stale a feed may get before the sweeper refetches it. 0 disables the
# sweeper entirely and leaves feeds manual-only.
AUTO_SYNC_MINUTES = int(os.environ.get("OPSDECK_FEED_SYNC_MINUTES", "60"))

_TICK_SECONDS = 60          # how often the sweeper looks for due feeds
_FIRST_TICK_SECONDS = 15    # let the app finish booting before any network I/O
_stop = threading.Event()


def fetch(url):
    """Download a feed. Returns text; raises urllib errors to the caller."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/calendar, text/plain;q=0.8, */*;q=0.5",
    })
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as res:
        raw = res.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError(f"feed larger than {MAX_BYTES // 1024 // 1024}MB")
    return raw.decode("utf-8", errors="replace")


def _unfold(text):
    """
    RFC 5545 folds long lines by starting the continuation with a space or
    tab. Rejoin them before parsing, or a long DESCRIPTION silently
    truncates at the fold.
    """
    return re.sub(r"\r?\n[ \t]", "", text.replace("\r\n", "\n"))


def _unescape(value):
    """RFC 5545 text escaping: \\n \\, \\; \\\\ ."""
    return (value.replace("\\n", "\n").replace("\\N", "\n")
                 .replace("\\,", ",").replace("\\;", ";")
                 .replace("\\\\", "\\"))


def _parse_dt(raw, params):
    """
    An ICS datetime into naive local time (what the events table stores).

    Three forms in the wild: UTC with a Z suffix, a floating local time, and
    a time carrying TZID. The first is converted; the other two are taken at
    face value, which is what a floating time means and the best available
    guess for a TZID this app has no library to resolve.

    Returns (datetime, is_all_day).
    """
    value = (raw or "").strip()
    if not value:
        return None, False

    # DATE form: 20260814 -> an all-day event
    if re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value, "%Y%m%d"), True

    m = re.fullmatch(r"(\d{8}T\d{6})(Z?)", value)
    if not m:
        return None, False
    stamp, zulu = m.groups()
    dt = datetime.strptime(stamp, "%Y%m%dT%H%M%S")

    if zulu:
        # UTC -> local wall-clock, which is what the rest of the app stores.
        dt = dt.replace(tzinfo=timezone.utc).astimezone(TZ).replace(tzinfo=None)
    elif params.get("TZID"):
        try:
            from zoneinfo import ZoneInfo
            src = ZoneInfo(params["TZID"])
            dt = dt.replace(tzinfo=src).astimezone(TZ).replace(tzinfo=None)
        except Exception:
            pass          # unknown zone: treat as floating
    return dt, False


def parse(text):
    """
    Extract VEVENTs from an ICS document.

    Deliberately a small hand-rolled parser rather than a dependency: the
    app ships with two Python packages and this needs six properties out of
    a line-oriented format. VTIMEZONE blocks are skipped - they contain
    their own DTSTART/RRULE lines which would otherwise be read as events.
    """
    events = []
    current = None
    depth_other = 0          # inside VTIMEZONE/VALARM etc.

    for line in _unfold(text).split("\n"):
        line = line.strip()
        if not line:
            continue

        if line.startswith("BEGIN:VEVENT"):
            current = {}
            continue
        if line.startswith("END:VEVENT"):
            if current:
                events.append(current)
            current = None
            continue
        if line.startswith("BEGIN:") and not line.startswith("BEGIN:VCALENDAR"):
            if current is None:
                depth_other += 1
            continue
        if line.startswith("END:") and not line.startswith("END:VCALENDAR"):
            if current is None and depth_other:
                depth_other -= 1
            continue
        if current is None or depth_other:
            continue

        if ":" not in line:
            continue
        name_part, _, value = line.partition(":")
        bits = name_part.split(";")
        name = bits[0].upper()
        params = {}
        for p in bits[1:]:
            k, _, v = p.partition("=")
            params[k.upper()] = v

        if name in ("SUMMARY", "DESCRIPTION", "LOCATION", "UID", "STATUS", "RRULE"):
            current[name] = _unescape(value)
        elif name in ("DTSTART", "DTEND"):
            dt, all_day = _parse_dt(value, params)
            current[name] = dt
            if all_day:
                current["ALL_DAY"] = True

    return [e for e in events if e.get("DTSTART")]


def event_key(feed_id, ev):
    """
    Stable identity for a feed event.

    Not the UID: feeds exist that regenerate it per request (see module
    docstring), which would defeat the whole point. Start, end and title are
    what actually identify a shift, and they hash the same on every fetch.
    """
    basis = "|".join([
        str(feed_id),
        fmt_dt(ev["DTSTART"]) if ev.get("DTSTART") else "",
        fmt_dt(ev["DTEND"]) if ev.get("DTEND") else "",
        (ev.get("SUMMARY") or "").strip(),
    ])
    return hashlib.sha256(basis.encode()).hexdigest()[:32]


def sync_feed(conn, feed):
    """
    Fetch one feed and replace its events.

    Returns a dict describing the outcome. Cancelled events must be able to
    vanish, so this clears the feed's rows across the span the feed covers
    and reinserts - a merge would leave a cancelled shift on the calendar
    forever.
    """
    try:
        text = fetch(feed["url"])
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code} from feed"}
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"could not reach feed: {e.reason}"}
    except (ValueError, TimeoutError) as e:
        return {"ok": False, "error": str(e)}

    if "BEGIN:VCALENDAR" not in text:
        return {"ok": False, "error": "not an iCalendar feed (no VCALENDAR)"}

    try:
        parsed = parse(text)
    except Exception as e:
        return {"ok": False, "error": f"parse failed: {e}"}

    # Skip anything the feed marks cancelled rather than importing it.
    parsed = [e for e in parsed if (e.get("STATUS") or "").upper() != "CANCELLED"]
    if not parsed:
        conn.execute("DELETE FROM events WHERE feed_id=?", (feed["id"],))
        return {"ok": True, "imported": 0, "removed": 0,
                "note": "feed contained no events"}

    starts = [e["DTSTART"] for e in parsed]
    win_start = min(starts) - timedelta(days=1)
    win_end = max(starts) + timedelta(days=1)

    removed = conn.execute(
        "DELETE FROM events WHERE feed_id=? AND start_at>=? AND start_at<=?",
        (feed["id"], fmt_dt(win_start), fmt_dt(win_end)),
    ).rowcount

    imported = 0
    for ev in parsed:
        start = ev["DTSTART"]
        end = ev.get("DTEND")
        all_day = bool(ev.get("ALL_DAY"))
        title = (ev.get("SUMMARY") or feed["name"] or "Busy").strip()
        conn.execute(
            """INSERT INTO events
               (title, description, location, start_at, end_at, all_day, color,
                profile_id, feed_id, external_uid)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (title,
             (ev.get("DESCRIPTION") or "").strip(),
             (ev.get("LOCATION") or "").strip(),
             fmt_dt(start),
             fmt_dt(end) if end else None,
             1 if all_day else 0,
             feed.get("color") or "blue",
             feed["profile_id"],
             feed["id"],
             event_key(feed["id"], ev)),
        )
        imported += 1

    conn.execute(
        "UPDATE calendar_feeds SET last_synced_at=datetime('now'), "
        "last_status=?, last_count=? WHERE id=?",
        ("ok", imported, feed["id"]),
    )
    return {"ok": True, "imported": imported, "removed": removed,
            "window": [fmt_dt(win_start)[:10], fmt_dt(win_end)[:10]]}


# ------------------------------------------------------------ auto-sync
#
# The mailbox gets away with delivering lazily on read (ARCHITECTURE 8)
# because delivery is a local UPDATE. Syncing a feed is an HTTP fetch with a
# 45-second timeout, so the same trick in the path of GET /api/events would
# mean an unreachable roster host hanging the calendar. Hence a timer.


def due_feeds(conn, minutes=None):
    """
    Enabled feeds whose last sync is older than the interval.

    The staleness comparison is left to SQLite rather than done in Python:
    last_synced_at is written with datetime('now') and is therefore UTC,
    while the rest of the app works in local wall-clock time. Comparing a
    stored value against the same clock that wrote it keeps those two
    conventions from ever meeting.
    """
    minutes = AUTO_SYNC_MINUTES if minutes is None else minutes
    rows = conn.execute(
        "SELECT * FROM calendar_feeds WHERE enabled=1 "
        "AND (last_synced_at IS NULL OR last_synced_at <= datetime('now', ?)) "
        "ORDER BY id",
        (f"-{int(minutes)} minutes",),
    ).fetchall()
    return [dict(r) for r in rows]


def sync_due(connect, minutes=None):
    """
    Sync every feed that is due, across all profiles.

    Deliberately not "call sync-all in a loop": that endpoint is scoped to
    the active profile, and the partner's timetable should refresh without
    anyone having to be looking at her tab.

    A connection per feed rather than one for the whole sweep, so a slow
    feed does not sit on an open handle while the rest of the app writes.
    """
    conn = connect()
    try:
        feeds = due_feeds(conn, minutes)
    finally:
        conn.close()

    results = []
    for feed in feeds:
        conn = connect()
        try:
            result = sync_feed(conn, feed)
            if not result.get("ok"):
                # Stamp the attempt even though it failed. Without this a
                # feed whose host is down would be retried every tick
                # instead of once per interval.
                conn.execute(
                    "UPDATE calendar_feeds SET last_status=?, "
                    "last_synced_at=datetime('now') WHERE id=?",
                    (result.get("error", "failed")[:200], feed["id"]),
                )
            conn.commit()
        except Exception as e:            # a bad feed must not stop the rest
            result = {"ok": False, "error": f"sync crashed: {e}"}
        finally:
            conn.close()
        results.append({"id": feed["id"], "name": feed["name"], **result})
    return results


def start_auto_sync(connect):
    """
    Start the background sweeper. Returns the thread, or None if disabled.

    Daemon, so it dies with the process and needs no shutdown handling. It
    is started from app.py's __main__ block rather than at import, so that
    importing the app for a test never fires network requests.
    """
    if AUTO_SYNC_MINUTES <= 0:
        return None

    def loop():
        delay = _FIRST_TICK_SECONDS
        while not _stop.wait(delay):
            delay = _TICK_SECONDS
            try:
                for r in sync_due(connect):
                    if not r.get("ok"):
                        print(f"  [feeds] {r['name']}: {r.get('error')}", flush=True)
            except Exception as e:
                # One bad tick must never kill the sweeper for good.
                print(f"  [feeds] sweep failed: {e}", flush=True)

    thread = threading.Thread(target=loop, name="feed-autosync", daemon=True)
    thread.start()
    return thread


def stop_auto_sync():
    """Ask the sweeper to exit. Only used by tests; the daemon handles prod."""
    _stop.set()
