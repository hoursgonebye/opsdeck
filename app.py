"""
Ops Deck - a self-hosted board / calendar / routines / docs dashboard.

The browser UI and any external script (Claude Code, cron, curl) both talk
to the same token-authenticated REST API in api.py. app.py only serves the
shell page and the service worker.
"""
import os
import secrets
from pathlib import Path

from flask import Flask, render_template, send_from_directory

from db import init_db, connect
from api import api, API_TOKEN
from social import social
from finance import finance
import finance_ai  # noqa: F401  - registers /api/finance/ai/* on the blueprint
from recurrence import TZ_NAME
from calendars import start_auto_sync, AUTO_SYNC_MINUTES
from briefing import start_scheduler as start_briefings

BRIEFING_TIME = os.environ.get("OPSDECK_BRIEFING_TIME", "23:45")

app = Flask(__name__)
app.register_blueprint(api)
app.register_blueprint(social)
app.register_blueprint(finance)

# Flask's own limit; api.py enforces the friendlier per-file check.
app.config["MAX_CONTENT_LENGTH"] = (
    int(os.environ.get("OPSDECK_MAX_UPLOAD_MB", "25")) + 1
) * 1024 * 1024


def asset_version():
    """
    A fingerprint of the current static assets, appended to every script and
    stylesheet URL as ?v=...

    Without it a browser can keep serving JS from a previous deploy - the
    server sends no-cache and the right ETag, but a copy cached earlier under
    different headers still wins, and the symptom is a UI that silently
    lacks whatever was just shipped. Changing the URL sidesteps the question
    entirely. Computed per request because the dev server reloads in place;
    it is a handful of stat() calls on ~15 files.
    """
    static_dir = Path(app.static_folder)
    latest = 0.0
    for path in static_dir.rglob("*"):
        if path.is_file():
            latest = max(latest, path.stat().st_mtime)
    return str(int(latest))


@app.route("/")
def index():
    # The token is embedded in the page so the UI can call its own API.
    # Anyone who can load this page could already read it from devtools -
    # the real boundary is Tailscale plus the token on the API itself.
    return render_template("index.html", api_token=API_TOKEN, tz=TZ_NAME,
                           v=asset_version())


@app.route("/sw.js")
def service_worker():
    # Must be served from the root so it can control the whole origin.
    return send_from_directory("static", "sw.js", mimetype="application/javascript")


if __name__ == "__main__":
    init_db()

    if not API_TOKEN:
        print("\n" + "=" * 62)
        print("  OPSDECK_TOKEN is not set - the API will refuse requests.")
        print("  Generate one and put it in your .env / compose file:")
        print(f"    OPSDECK_TOKEN={secrets.token_urlsafe(32)}")
        print("=" * 62 + "\n")

    print(f"  Timezone: {TZ_NAME}")

    # Started here rather than at import so that importing app.py for a test
    # never starts fetching calendars. app.run() has no reloader in this
    # configuration, so there is no risk of two sweepers.
    if AUTO_SYNC_MINUTES > 0:
        print(f"  Calendar feeds: auto-sync every {AUTO_SYNC_MINUTES} min")
    else:
        print("  Calendar feeds: auto-sync off, manual only")
    start_auto_sync(connect)

    # Nightly mentor briefing (deterministic digest, no model calls).
    if start_briefings(connect, BRIEFING_TIME):
        print(f"  Mentor briefing: nightly at {BRIEFING_TIME}")
    else:
        print("  Mentor briefing: disabled")

    app.run(host="0.0.0.0", port=5000)
