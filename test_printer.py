"""
Tests for the printer module. Run inside the app image:

    docker run --rm --network host -v /root/opstest:/app -w /app \
        opsdeck-opsdeck:latest python test_printer.py

Hits the real machine, because the things worth checking here are exactly the
ones a mock would paper over: that the LAN host is reachable from inside the
container, that the camera bytes really are a JPEG, and that a powered-off
printer degrades to a clean "offline" instead of an exception.
"""
import os
import shutil
import sys
import tempfile

os.environ["OPSDECK_TOKEN"] = "testtoken"

_tmp = tempfile.mkdtemp(prefix="opstest-")
import db  # noqa: E402
db.DATA_DIR = __import__("pathlib").Path(_tmp)
db.DB_PATH = db.DATA_DIR / "opsdeck.db"
db.UPLOAD_DIR = db.DATA_DIR / "uploads"

import printer as pr  # noqa: E402

FAILS = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}{': ' + str(detail) if detail else ''}")
    if not cond:
        FAILS.append(label)


def main():
    db.init_db()

    print("\n== reachability from inside the container ==")
    s = pr.read_status()
    check("read_status returns a dict", isinstance(s, dict))
    if not s.get("online"):
        print(f"  info  printer OFFLINE ({s.get('error')}) - "
              f"checking graceful degradation only")
        check("offline is reported, not raised", s["online"] is False)
        check("offline carries the host", bool(s.get("host")))
        check("offline carries a reason", bool(s.get("error")))
    else:
        check("moonraker answered", s["online"] is True)
        check("klippy state present", bool(s.get("klippy_state")), s.get("klippy_state"))
        if s.get("klippy_state") == "ready":
            check("has a print state", bool(s.get("state")), s.get("state"))
            check("nozzle temp is a number", isinstance(s["extruder"]["temp"], float),
                  s["extruder"])
            check("bed temp is a number", isinstance(s["bed"]["temp"], float), s["bed"])
            check("progress in range", 0.0 <= s["progress"] <= 1.0, s["progress"])
            check("no ETA while idle" if s["state"] != "printing" else "eta shape",
                  s["eta_seconds"] is None or isinstance(s["eta_seconds"], int),
                  s["eta_seconds"])

    print("\n== the HTTP surface ==")
    import app as appmod
    client = appmod.app.test_client()
    H = {"X-API-Token": "testtoken"}

    r = client.get("/api/printer/config", headers=H)
    check("config 200", r.status_code == 200)
    cfg = r.get_json()
    check("config names the host", cfg["host"] == pr.HOST, cfg["host"])
    check("config exposes a ui_url", bool(cfg["ui_url"]), cfg["ui_url"])
    check("ui_url is https - an http iframe would be blocked outright",
          cfg["ui_url"].startswith("https://"), cfg["ui_url"])

    r = client.get("/api/printer/status", headers=H)
    check("status 200", r.status_code == 200)
    check("status is json", r.get_json() is not None)

    r = client.get("/api/printer/snapshot", headers=H)
    if r.status_code == 200:
        body = r.get_data()
        check("snapshot is served", True, f"{len(body)} bytes")
        # A real JPEG starts FFD8FF and ends FFD9 - proof we proxied an image
        # and not an error page with a 200 on it.
        check("snapshot really is a JPEG", body[:3] == b"\xff\xd8\xff", body[:8])
        check("snapshot is not cached",
              "no-store" in (r.headers.get("Cache-Control") or ""))
        check("content-type is an image",
              (r.headers.get("Content-Type") or "").startswith("image/"),
              r.headers.get("Content-Type"))
    else:
        check("camera down reports 503, not 500", r.status_code == 503, r.status_code)

    print("\n== auth ==")
    check("status needs a token", client.get("/api/printer/status").status_code == 401)
    check("snapshot needs a token", client.get("/api/printer/snapshot").status_code == 401)
    check("stream needs a token", client.get("/api/printer/stream").status_code == 401)
    # The <img> path: a browser cannot set a header on a stream, so ?token=
    # has to work or the live view is impossible.
    check("query-string token is accepted (the <img> path)",
          client.get("/api/printer/status?token=testtoken").status_code == 200)

    print("\n== read-only by construction ==")
    rules = [str(r) for r in appmod.app.url_map.iter_rules()
             if str(r).startswith("/api/printer")]
    methods = set()
    for r in appmod.app.url_map.iter_rules():
        if str(r).startswith("/api/printer"):
            methods |= (r.methods - {"HEAD", "OPTIONS"})
    print(f"  info  routes: {sorted(rules)}")
    check("no write verbs are exposed at all", methods == {"GET"}, sorted(methods))

    print("\n== the stream cap releases its slot ==")
    before = pr._stream_slots._value
    r = client.get("/api/printer/stream", headers=H)
    if r.status_code == 200:
        r.close()
        check("stream opened", True)
    else:
        check("stream unavailable is 503/429, not a crash",
              r.status_code in (503, 429), r.status_code)
    # Whether it opened or failed, the semaphore must be back where it started
    # or a few reloads permanently wedge the live view.
    check("semaphore restored", pr._stream_slots._value == before,
          f"{pr._stream_slots._value} vs {before}")

    print("\n== offline degradation (unroutable host) ==")
    real = pr.MOONRAKER
    pr.MOONRAKER = "http://192.0.2.1"      # TEST-NET-1, guaranteed to go nowhere
    pr.TIMEOUT = 2
    off = pr.read_status()
    check("unreachable host is offline, not an exception", off["online"] is False)
    check("and says why", bool(off.get("error")), off.get("error"))
    pr.MOONRAKER = real

    print("\n" + "=" * 58)
    print(f"{len(FAILS)} FAILURES: {FAILS}" if FAILS else "all checks passed")
    print("=" * 58)
    return 1 if FAILS else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(_tmp, ignore_errors=True)
