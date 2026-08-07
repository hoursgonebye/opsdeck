"""
Turning stored events into concrete calendar occurrences.

An event row holds a start time plus an optional RRULE string (RFC 5545,
the same format Google Calendar and iCal use). This module expands that
into real datetimes inside a window, then applies per-occurrence
overrides so you can skip or move a single instance without breaking the
rest of the series.

Everything is stored as naive local time in TZ_NAME. That keeps the
comparisons simple; the tradeoff is a DST-crossing recurring event can
drift by an hour, which is the standard tradeoff calendar apps make in
one direction or the other.
"""
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr

TZ_NAME = os.environ.get("OPSDECK_TZ", "America/New_York")
TZ = ZoneInfo(TZ_NAME)

ISO = "%Y-%m-%dT%H:%M:%S"


def now_local():
    return datetime.now(TZ).replace(tzinfo=None)


def today_local():
    return now_local().date()


def parse_dt(value):
    """Accept both 'YYYY-MM-DDTHH:MM:SS' and 'YYYY-MM-DDTHH:MM'."""
    if not value:
        return None
    value = value.replace("Z", "").strip()
    for fmt in (ISO, "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(f"unparseable datetime: {value!r}")


def fmt_dt(dt):
    return dt.strftime(ISO)


def expand_event(event, window_start, window_end, overrides=None):
    """
    Return a list of occurrence dicts for one event inside a window.

    Non-recurring events yield at most one occurrence. Recurring events
    are expanded via their RRULE, then filtered/rewritten by overrides
    keyed on the original occurrence date.
    """
    overrides = overrides or {}
    start = parse_dt(event["start_at"])
    end = parse_dt(event["end_at"]) if event["end_at"] else None
    duration = (end - start) if end else timedelta(0)

    if not event["rrule"]:
        starts = [start] if window_start <= start <= window_end else []
    else:
        # dtstart anchors the series; between() is inclusive on both ends.
        rule = rrulestr(event["rrule"], dtstart=start)
        starts = list(rule.between(window_start, window_end, inc=True))

    out = []
    for occ_start in starts:
        key = occ_start.strftime("%Y-%m-%d")
        ov = overrides.get(key)

        if ov and ov["action"] == "skip":
            continue

        occ = {
            "event_id": event["id"],
            "occurrence": key,
            "title": event["title"],
            "description": event["description"],
            "location": event["location"],
            "all_day": bool(event["all_day"]),
            "color": event["color"],
            "rrule": event["rrule"],
            "remind_min": event["remind_min"],
            "start_at": fmt_dt(occ_start),
            "end_at": fmt_dt(occ_start + duration) if end else None,
            "modified": False,
            "kind": "event",
            # Present when the event came from a subscribed feed. The UI uses
            # it to mark those read-only: an edit would be silently undone by
            # the next sync, so offering one would be a lie.
            "feed_id": event["feed_id"] if "feed_id" in event.keys() else None,
        }

        if ov and ov["action"] == "move":
            if ov["new_start_at"]:
                new_start = parse_dt(ov["new_start_at"])
                occ["start_at"] = fmt_dt(new_start)
                occ["end_at"] = (
                    fmt_dt(parse_dt(ov["new_end_at"]))
                    if ov["new_end_at"]
                    else (fmt_dt(new_start + duration) if end else None)
                )
            if ov["new_title"]:
                occ["title"] = ov["new_title"]
            occ["modified"] = True

        out.append(occ)

    return out


def describe_rrule(rrule_str):
    """A short human label for the UI. Not exhaustive - covers common cases."""
    if not rrule_str:
        return "One-time"
    s = rrule_str.upper()
    freq = "Repeats"
    if "FREQ=DAILY" in s:
        freq = "Daily"
    elif "FREQ=WEEKLY" in s:
        freq = "Weekly"
    elif "FREQ=MONTHLY" in s:
        freq = "Monthly"
    elif "FREQ=YEARLY" in s:
        freq = "Yearly"

    interval = 1
    for part in s.split(";"):
        if part.startswith("INTERVAL="):
            try:
                interval = int(part.split("=")[1])
            except ValueError:
                pass
    if interval > 1:
        unit = {"Daily": "days", "Weekly": "weeks", "Monthly": "months", "Yearly": "years"}
        freq = f"Every {interval} {unit.get(freq, 'times')}"

    if "COUNT=" in s or "UNTIL=" in s:
        freq += " (ends)"
    return freq
