"""
Ops Deck - a self-hosted board / calendar / routines / docs dashboard.

The browser UI and any external script (Claude Code, cron, curl) both talk
to the same token-authenticated REST API in api.py. app.py only serves the
shell page and the service worker.
"""
import os
import secrets

from flask import Flask, render_template, send_from_directory

from db import init_db
from api import api, API_TOKEN
from social import social
from recurrence import TZ_NAME

app = Flask(__name__)
app.register_blueprint(api)
app.register_blueprint(social)

# Flask's own limit; api.py enforces the friendlier per-file check.
app.config["MAX_CONTENT_LENGTH"] = (
    int(os.environ.get("OPSDECK_MAX_UPLOAD_MB", "25")) + 1
) * 1024 * 1024


@app.route("/")
def index():
    # The token is embedded in the page so the UI can call its own API.
    # Anyone who can load this page could already read it from devtools -
    # the real boundary is Tailscale plus the token on the API itself.
    return render_template("index.html", api_token=API_TOKEN, tz=TZ_NAME)


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
    app.run(host="0.0.0.0", port=5000)
