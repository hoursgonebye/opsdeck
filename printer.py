"""
Printer - a live view of the Klipper/Fluidd machine, under /api/printer.

The whole design here follows from one constraint: **Ops Deck is served over
HTTPS and the printer speaks plain HTTP on the LAN.** Browsers refuse to load
http:// subresources into an https:// page, so nothing on 10.0.0.x may be
referenced by the browser directly - an <img> or <iframe> pointing at the
printer renders an empty box on every device, with no error the user can see.

So there are exactly two ways across that line, and this module is one of them:

  - **The camera is proxied here.** /snapshot and /stream fetch from
    mjpg-streamer server-side and re-serve the bytes same-origin, which makes
    them ordinary HTTPS resources the browser is happy with.
  - **The Fluidd UI is fronted by its own `tailscale serve` listener** on
    :8444, because a single-page app cannot be usefully reverse-proxied under
    a path prefix - Fluidd requests /js/app.js absolutely, and rewriting an
    SPA's asset paths and its Moonraker websocket on the fly is a losing game.
    That URL is only reachable inside the tailnet.

Deliberately **not profile-scoped**: there is one physical printer and one
household, so this ignores X-Profile-Id the way /api/joint does. Nothing here
writes - no endpoint can move an axis, heat a nozzle or start a job. Read-only
by construction, so the worst a bug can do is show a stale temperature.

A powered-off printer is the normal case, not an error case. Every endpoint
degrades to a clean "offline" rather than a stack trace or a hung request.
"""
import json
import os
import threading
import urllib.error
import urllib.request

from flask import Blueprint, Response, jsonify

from api import require_token
from db import connect  # noqa: F401  - kept for symmetry with the other blueprints

printer = Blueprint("printer", __name__, url_prefix="/api/printer")

HOST = os.environ.get("OPSDECK_PRINTER_HOST", "10.0.0.131")
CAMERA_PORT = os.environ.get("OPSDECK_PRINTER_CAMERA_PORT", "8080")
# The HTTPS front for the Fluidd SPA. Empty disables the embedded dashboard
# and the UI falls back to its own status panel plus a plain link.
UI_URL = os.environ.get("OPSDECK_PRINTER_UI_URL",
                        "https://opsdeck.example.ts.net:8444")

MOONRAKER = f"http://{HOST}"
CAMERA = f"http://{HOST}:{CAMERA_PORT}"

# Short, because the printer being off must feel like "off", not like the tab
# hanging. A dead host on the same subnet refuses fast; this is the ceiling.
TIMEOUT = 5
SNAPSHOT_TIMEOUT = 8

# Each live stream holds a thread and an upstream socket for as long as the
# tab is open. One viewer is the real case; the cap stops a stack of forgotten
# phone tabs from quietly eating the app's worker threads.
MAX_STREAMS = 3
_stream_slots = threading.BoundedSemaphore(MAX_STREAMS)

# The Moonraker objects worth one round trip.
QUERY = ("print_stats&display_status&heater_bed&extruder"
         "&toolhead&virtual_sdcard&idle_timeout")


def _get_json(url, timeout=TIMEOUT):
    """GET and parse, or raise. Callers turn failure into 'offline'."""
    req = urllib.request.Request(url, headers={"User-Agent": "OpsDeck/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode())


def _num(value, fallback=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _heater(obj):
    return {
        "temp": round(_num(obj.get("temperature")), 1),
        "target": round(_num(obj.get("target")), 1),
        "power": round(_num(obj.get("power")), 2),
    }


def read_status():
    """
    One normalised snapshot of the machine, or an offline marker.

    Never raises: a printer that is unplugged, asleep, or mid-firmware-restart
    is an ordinary state for this tab to render.
    """
    try:
        info = _get_json(f"{MOONRAKER}/server/info")
    except Exception as e:
        return {"online": False,
                "error": f"{type(e).__name__}: {e}",
                "host": HOST}

    result = (info or {}).get("result") or {}
    klippy = result.get("klippy_state") or "unknown"
    out = {
        "online": True,
        "host": HOST,
        "klippy_state": klippy,
        "klippy_connected": bool(result.get("klippy_connected")),
        "error": None,
    }

    # Klipper can be in error/shutdown while Moonraker itself answers fine -
    # that distinction is the whole point of showing klippy_state separately.
    if klippy != "ready":
        out["state"] = klippy
        return out

    try:
        data = _get_json(f"{MOONRAKER}/printer/objects/query?{QUERY}")
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        out["state"] = "unknown"
        return out

    status = ((data or {}).get("result") or {}).get("status") or {}
    stats = status.get("print_stats") or {}
    display = status.get("display_status") or {}

    progress = _num(display.get("progress"))
    elapsed = _num(stats.get("print_duration"))

    # Progress-rate ETA rather than the slicer's estimate: it needs no file
    # metadata call and self-corrects as the print goes. Meaningless in the
    # first moments, so it stays None until there is something to extrapolate.
    eta = None
    if progress > 0.01 and elapsed > 30:
        eta = int(elapsed * (1 - progress) / progress)

    out.update({
        "state": stats.get("state") or "standby",
        "filename": stats.get("filename") or "",
        # print_stats.message only - it is the real state/error text. The one
        # on display_status is the LCD's M117 line, which keeps whatever the
        # last job set until something overwrites it: this machine sits idle
        # reading "Printing", and surfacing that would be a banner that lies.
        "message": stats.get("message") or "",
        "lcd_message": display.get("message") or "",
        "progress": round(progress, 4),
        "print_duration": int(elapsed),
        "total_duration": int(_num(stats.get("total_duration"))),
        "filament_used_mm": round(_num(stats.get("filament_used")), 1),
        "eta_seconds": eta,
        "extruder": _heater(status.get("extruder") or {}),
        "bed": _heater(status.get("heater_bed") or {}),
        "position": (status.get("toolhead") or {}).get("position") or [],
    })
    return out


@printer.route("/config", methods=["GET"])
@require_token
def get_config():
    """What the front end needs to decide what it can render."""
    return jsonify({
        "host": HOST,
        "camera_url_available": True,
        "ui_url": UI_URL,
        "lan_url": MOONRAKER,
        "max_streams": MAX_STREAMS,
    })


@printer.route("/status", methods=["GET"])
@require_token
def get_status():
    return jsonify(read_status())


@printer.route("/snapshot", methods=["GET"])
@require_token
def snapshot():
    """
    One JPEG, proxied. The front end polls this rather than holding a stream
    open, because a still every second or two is what a print actually needs
    and it survives a phone locking, backgrounding and waking without leaving
    a socket behind.
    """
    try:
        req = urllib.request.Request(f"{CAMERA}/?action=snapshot",
                                     headers={"User-Agent": "OpsDeck/1.0"})
        with urllib.request.urlopen(req, timeout=SNAPSHOT_TIMEOUT) as res:
            body = res.read()
            ctype = res.headers.get("Content-Type", "image/jpeg")
    except Exception as e:
        # 503 rather than 500: the app is fine, the camera is not.
        return jsonify({"error": f"camera unreachable: {type(e).__name__}"}), 503

    return Response(body, mimetype=ctype, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
    })


@printer.route("/stream", methods=["GET"])
@require_token
def stream():
    """
    The live MJPEG feed, proxied.

    Opt-in from the UI, because unlike /snapshot this holds a connection open
    for as long as the tab lives. The semaphore is released in the generator's
    finally, which Flask runs when the client disconnects.
    """
    if not _stream_slots.acquire(blocking=False):
        return jsonify({"error": f"too many live streams open (max {MAX_STREAMS}); "
                                 f"close one or use the snapshot view"}), 429

    try:
        req = urllib.request.Request(f"{CAMERA}/?action=stream",
                                     headers={"User-Agent": "OpsDeck/1.0"})
        upstream = urllib.request.urlopen(req, timeout=SNAPSHOT_TIMEOUT)
    except Exception as e:
        _stream_slots.release()
        return jsonify({"error": f"camera unreachable: {type(e).__name__}"}), 503

    ctype = upstream.headers.get("Content-Type",
                                 "multipart/x-mixed-replace; boundary=boundarydonotcross")

    def pump():
        try:
            while True:
                chunk = upstream.read(8192)
                if not chunk:
                    break
                yield chunk
        except (GeneratorExit, OSError):
            pass
        finally:
            try:
                upstream.close()
            finally:
                _stream_slots.release()

    return Response(pump(), mimetype=ctype, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        # Belt and braces: an intermediary buffering an MJPEG stream turns a
        # live view into a stall.
        "X-Accel-Buffering": "no",
    })
