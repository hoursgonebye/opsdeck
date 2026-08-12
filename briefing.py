"""
The mentor's daily briefing: a deterministic end-of-day digest per profile,
written into Docs (folder "Briefings") so the chat mentor starts each day
already knowing what happened and what's coming - without spending a single
API token to find out. Everything here is queries and string formatting;
no model is ever called.

Written nightly by a daemon thread (started from app.py, same pattern as
the calendar-feed sweeper), and on demand via POST /api/mentor/briefing.
Whether today's briefing exists is answered from the docs table, not
memory - the same lesson the chat bridge learned about container restarts.
"""
import os
import threading
from datetime import datetime, timedelta

from db import connect
from recurrence import now_local, today_local, fmt_dt, expand_event

import finance as fin

BRIEFING_FOLDER = "Briefings"

# The cashflow guard: liquid balance minus recurring charges due in the
# next two weeks under this floor -> a pushed warning. 0 disables.
LOW_BALANCE_CENTS = int(os.environ.get("OPSDECK_LOW_BALANCE_CENTS", "2500"))

# The morning nudge: a short pushed summary of the day - shifts, payday
# countdown, unfiled transactions, budget state - at this local time.
# Deterministic, composed from the same data as the briefing. Empty disables.
MORNING_TIME = os.environ.get("OPSDECK_MORNING_TIME", "08:30")

# One rotating closer per weekday (Mon..Sun): the "don't forget to be a
# person" line. Deliberately small and dumb - charm, not AI.
CLOSERS = [
    "Eat something real today.",
    "Water before caffeine.",
    "Stretch — your back will thank you at 40.",
    "Tell someone you appreciate them.",
    "Ten minutes on the tree beats zero.",
    "Log purchases when they happen, not later.",
    "Sleep is a skill. Practice tonight.",
]

_TICK_SECONDS = 60
_stop = threading.Event()


def _money(cents):
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) // 100:,}.{abs(cents) % 100:02d}"


def _events_between(conn, pid, start_dt, end_dt):
    """Expanded occurrences for a profile in a window, sorted by start."""
    rows = conn.execute(
        "SELECT * FROM events WHERE profile_id=?", (pid,)).fetchall()
    out = []
    for ev in rows:
        by_occ = {ov["occurrence"]: ov for ov in conn.execute(
            "SELECT * FROM event_overrides WHERE event_id=?", (ev["id"],)).fetchall()}
        out.extend(expand_event(ev, start_dt, end_dt, by_occ))
    out.sort(key=lambda o: o["start_at"])
    return out


def _hours(occ):
    """An occurrence's duration in hours, or None (all-day / no end)."""
    if occ.get("all_day") or not occ.get("end_at"):
        return None
    try:
        s = datetime.strptime(occ["start_at"][:16], "%Y-%m-%dT%H:%M")
        e = datetime.strptime(occ["end_at"][:16], "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    h = (e - s).total_seconds() / 3600
    return round(h, 2) if h > 0 else None


def compose(conn, pid, date=None):
    """Build one profile's briefing markdown for a date (default today)."""
    date = str(date or today_local())
    day = datetime.strptime(date, "%Y-%m-%d")
    lines = [f"# Briefing — {date}", ""]

    # ---- today, in numbers ----
    done = conn.execute(
        "SELECT COUNT(*) FROM routine_completions rc JOIN routines r ON r.id=rc.routine_id "
        "WHERE r.profile_id=? AND rc.local_date=?", (pid, date)).fetchone()[0]
    total = conn.execute(
        "SELECT COUNT(*) FROM routines WHERE profile_id=? AND active=1", (pid,)).fetchone()[0]
    cards_done = conn.execute(
        "SELECT COUNT(*) FROM cards c JOIN lists l ON l.id=c.list_id "
        "JOIN boards b ON b.id=l.board_id "
        "WHERE b.profile_id=? AND date(c.completed_at)=?", (pid, date)).fetchone()[0]
    lines += [f"## The day itself",
              f"- Routines: {done}/{total} completed",
              f"- Cards completed: {cards_done}"]

    unfiled = conn.execute(
        "SELECT COUNT(*) FROM quick_notes WHERE profile_id=? AND status='pending'",
        (pid,)).fetchone()[0]
    if unfiled:
        lines.append(f"- Unfiled quick notes waiting: {unfiled}")

    # ---- money ----
    summary = fin.compute_summary(conn, pid, date[:7])
    if summary and summary["balances"]:
        tx_today = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(CASE WHEN t.direction='debit' "
            "THEN t.amount_cents ELSE -t.amount_cents END),0) "
            "FROM fin_transactions t JOIN fin_accounts a ON a.id=t.account_id "
            "WHERE a.profile_id=? AND t.posted_date=?", (pid, date)).fetchone()
        lines += ["", "## Money"]
        for b in summary["balances"]:
            owed = " owed" if b["type"] == "credit" else ""
            lines.append(f"- {b['name']}: {_money(b['balance_cents'])}{owed} ({b['basis']})")
        lines.append(f"- Net position: {_money(summary['net_cents'])}")
        lines.append(f"- This month: spent {_money(summary['spend_total_cents'])}, "
                     f"received {_money(summary['income_received_cents'])}")
        if tx_today[0]:
            lines.append(f"- Logged today: {tx_today[0]} transactions, "
                         f"net {_money(tx_today[1])} out")
        over = [c for c in summary["categories"]
                if c.get("limit_cents") and c.get("remaining_cents", 0) < 0]
        for c in over:
            lines.append(f"- OVER BUDGET: {c['name']} by {_money(-c['remaining_cents'])}")
        if summary["uncategorized"]["count"]:
            lines.append(f"- Uncategorized transactions: {summary['uncategorized']['count']}")

        soon = day + timedelta(days=8)
        due = [r for r in fin.compute_recurring(conn, pid)
               if date <= r["next_expected"] <= soon.strftime("%Y-%m-%d")]
        if due:
            lines.append("- Recurring charges expected in the next week: "
                         + ", ".join(f"{r['merchant']} ~{_money(r['amount_cents'])} "
                                     f"({r['next_expected']})" for r in due))

        squeeze = cashflow_check(conn, pid)
        if squeeze:
            lines.append(f"- ⚠ CASHFLOW: {squeeze['message']}")

    # ---- the next seven days ----
    week = _events_between(conn, pid, day, day + timedelta(days=8))
    if week:
        lines += ["", "## Coming up (7 days)"]
        shift_hours = 0.0
        for occ in week:
            d = occ["start_at"][:10]
            h = _hours(occ)
            when = "all day" if occ.get("all_day") else occ["start_at"][11:16]
            extra = f" ({h}h)" if h and occ.get("feed_id") else ""
            if h and occ.get("feed_id"):
                shift_hours += h
            lines.append(f"- {d} {when} — {occ['title']}{extra}")
        if shift_hours:
            lines.append(f"- Scheduled work (from the roster feed): "
                         f"{round(shift_hours, 2)} hours this week")

    # ---- skills ----
    pending = conn.execute(
        "SELECT COUNT(*) FROM levelup_attempts WHERE profile_id=? "
        "AND status IN ('awaiting_questions','awaiting_answer','grading')",
        (pid,)).fetchone()[0]
    last = conn.execute(
        "SELECT sl.local_date, sn.title, sl.level FROM skill_levels sl "
        "JOIN skill_nodes sn ON sn.id=sl.node_id "
        "WHERE sn.profile_id=? ORDER BY sl.id DESC LIMIT 1", (pid,)).fetchone()
    if pending or last:
        lines += ["", "## Skills"]
        if pending:
            lines.append(f"- Verification attempts in flight: {pending}")
        if last:
            lines.append(f"- Last level earned: {last['title']} L{last['level']} "
                         f"on {last['local_date']}")

    # ---- health (yesterday's numbers are the freshest complete ones) ----
    h = conn.execute(
        "SELECT metric, MAX(value) AS v FROM health_metrics "
        "WHERE profile_id=? AND local_date=? AND metric IN "
        "('sleep_minutes','steps','exercise_minutes') GROUP BY metric",
        (pid, date)).fetchall()
    hmap = {r["metric"]: r["v"] for r in h}
    if hmap:
        bits = []
        if hmap.get("sleep_minutes"):
            m = int(hmap["sleep_minutes"])
            bits.append(f"slept {m // 60}h{m % 60:02d}")
        if hmap.get("steps"):
            bits.append(f"{int(hmap['steps']):,} steps")
        if hmap.get("exercise_minutes"):
            bits.append(f"{int(hmap['exercise_minutes'])} exercise min")
        if bits:
            lines += ["", "## Health", "- " + ", ".join(bits)]

    lines += ["", f"*Generated {fmt_dt(now_local())} — deterministic digest, "
              f"no model involved.*"]
    return "\n".join(lines)


def cashflow_check(conn, pid):
    """
    The deterministic money guard: liquid balance (checking + cash) minus
    recurring charges due in the next 14 days. Below the floor, returns a
    dict with a plain-language message; otherwise None. Pure arithmetic
    over server data - no model anywhere near it.
    """
    if LOW_BALANCE_CENTS <= 0:
        return None
    accounts = [dict(r) for r in conn.execute(
        "SELECT * FROM fin_accounts WHERE profile_id=? AND is_active=1 "
        "AND type IN ('checking','cash')", (pid,))]
    if not accounts:
        return None
    liquid = sum(fin.derived_balance(conn, a)[0] for a in accounts)

    horizon = (datetime.strptime(str(today_local()), "%Y-%m-%d")
               + timedelta(days=14)).strftime("%Y-%m-%d")
    due = [r for r in fin.compute_recurring(conn, pid)
           if r["next_expected"] <= horizon]
    committed = sum(r["amount_cents"] for r in due)
    projected = liquid - committed
    if projected >= LOW_BALANCE_CENTS:
        return None

    charges = ", ".join(f"{r['merchant']} {_money(r['amount_cents'])} "
                        f"on {r['next_expected']}" for r in due) or "none detected"
    return {
        "liquid_cents": liquid,
        "committed_cents": committed,
        "projected_cents": projected,
        "message": (f"{_money(liquid)} liquid minus {_money(committed)} in "
                    f"recurring charges due within 14 days leaves "
                    f"{_money(projected)} ({charges})"),
    }


def _notify_cashflow(conn, pid):
    """Push the guard's warning, at most once per profile per day."""
    squeeze = cashflow_check(conn, pid)
    if not squeeze:
        return False
    already = conn.execute(
        "SELECT 1 FROM notifications WHERE profile_id=? AND source_type='cashflow' "
        "AND date(created_at)=date('now')", (pid,)).fetchone()
    if already:
        return False
    from social import notify
    notify(conn, pid, "cashflow", "Money is tight ahead",
           squeeze["message"], link="#finance")
    conn.commit()
    return True


def compose_morning(conn, pid):
    """The day in one push notification: schedule, payday, money, a nudge."""
    day = datetime.strptime(str(today_local()), "%Y-%m-%d")
    parts = []

    today_events = _events_between(conn, pid, day, day + timedelta(days=1))
    if today_events:
        bits = []
        for occ in today_events[:2]:
            h = _hours(occ)
            when = "all day" if occ.get("all_day") else occ["start_at"][11:16]
            bits.append(f"{when} {occ['title']}" + (f" ({h}h)" if h and occ.get("feed_id") else ""))
        if len(today_events) > 2:
            bits.append(f"+{len(today_events) - 2} more")
        parts.append("; ".join(bits))
    else:
        parts.append("Nothing scheduled")

    for occ in _events_between(conn, pid, day, day + timedelta(days=4)):
        if "payday" in (occ["title"] or "").lower():
            d = occ["start_at"][:10]
            delta = (datetime.strptime(d, "%Y-%m-%d") - day).days
            when = "today" if delta == 0 else "tomorrow" if delta == 1 else \
                datetime.strptime(d, "%Y-%m-%d").strftime("%a")
            parts.append(f"💰 Payday {when}")
            break

    summary = fin.compute_summary(conn, pid, str(today_local())[:7])
    if summary and summary["balances"]:
        if summary["uncategorized"]["count"]:
            parts.append(f"{summary['uncategorized']['count']} unfiled transactions")
        over = [c["name"] for c in summary["categories"]
                if c.get("limit_cents") and c.get("remaining_cents", 0) < 0]
        if over:
            parts.append("over budget: " + ", ".join(over[:2]))
        squeeze = cashflow_check(conn, pid)
        if squeeze:
            parts.append(f"money's tight ({_money(squeeze['projected_cents'])} "
                         f"after upcoming bills)")

    parts.append(CLOSERS[day.weekday()])
    return " · ".join(parts)


def _notify_morning(conn, pid):
    """Push the morning nudge, at most once per profile per day."""
    already = conn.execute(
        "SELECT 1 FROM notifications WHERE profile_id=? AND source_type='morning' "
        "AND date(created_at)=date('now')", (pid,)).fetchone()
    if already:
        return False
    body = compose_morning(conn, pid)
    day = datetime.strptime(str(today_local()), "%Y-%m-%d")
    from social import notify
    notify(conn, pid, "morning", f"☀ {day.strftime('%A')}", body, link="#today")
    conn.commit()
    return True


def generate(conn, pid, date=None):
    """Compose and upsert the briefing doc. Returns (title, body)."""
    date = str(date or today_local())
    body = compose(conn, pid, date)
    title = f"Briefing — {date}"
    row = conn.execute(
        "SELECT id FROM docs WHERE profile_id=? AND folder=? AND title=?",
        (pid, BRIEFING_FOLDER, title)).fetchone()
    if row:
        conn.execute("UPDATE docs SET body=?, updated_at=datetime('now') WHERE id=?",
                     (body, row["id"]))
    else:
        conn.execute(
            "INSERT INTO docs (title, kind, body, folder, profile_id) "
            "VALUES (?,?,?,?,?)", (title, "md", body, BRIEFING_FOLDER, pid))
    conn.commit()
    return title, body


def latest(conn, pid):
    """Most recent briefing doc, or None."""
    row = conn.execute(
        "SELECT title, body, updated_at FROM docs WHERE profile_id=? AND folder=? "
        "ORDER BY title DESC LIMIT 1", (pid, BRIEFING_FOLDER)).fetchone()
    return dict(row) if row else None


def _exists_today(conn, pid):
    return bool(conn.execute(
        "SELECT 1 FROM docs WHERE profile_id=? AND folder=? AND title=?",
        (pid, BRIEFING_FOLDER, f"Briefing — {today_local()}")).fetchone())


def _parse_hhmm(s):
    try:
        hh, mm = (s or "").split(":")
        return int(hh) * 60 + int(mm)
    except ValueError:
        return None


def start_scheduler(connect_fn, at="23:45"):
    """
    Two daily jobs on one daemon thread: the nightly briefing write (at the
    configured time) and the morning nudge (MORNING_TIME). Both answer "did
    today already run" from the database - the docs table for briefings, the
    notifications table for nudges - never from memory, so a restart
    neither skips nor duplicates a day. Returns the thread, or None when
    both are disabled.
    """
    night = _parse_hhmm(at)
    morning = _parse_hhmm(MORNING_TIME)
    if night is None and morning is None:
        return None

    def loop():
        while not _stop.wait(_TICK_SECONDS):
            now = now_local()
            minutes = now.hour * 60 + now.minute
            try:
                conn = connect_fn()
                try:
                    profiles = [r["id"] for r in conn.execute(
                        "SELECT id FROM profiles WHERE type!='joint'").fetchall()]
                    if night is not None and minutes >= night:
                        for pid in profiles:
                            if not _exists_today(conn, pid):
                                generate(conn, pid)
                                if _notify_cashflow(conn, pid):
                                    print(f"  [briefing] cashflow warning for {pid}",
                                          flush=True)
                                print(f"  [briefing] wrote {pid} {today_local()}",
                                      flush=True)
                    # A four-hour window: a container that was down all
                    # morning skips the day rather than nudging at 9pm.
                    if morning is not None and morning <= minutes < morning + 240:
                        for pid in profiles:
                            if _notify_morning(conn, pid):
                                print(f"  [briefing] morning nudge for {pid}",
                                      flush=True)
                finally:
                    conn.close()
            except Exception as e:
                print(f"  [briefing] failed: {e}", flush=True)

    thread = threading.Thread(target=loop, name="briefing", daemon=True)
    thread.start()
    return thread


def stop_scheduler():
    _stop.set()
