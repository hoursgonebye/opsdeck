"""
Govee - lighting control, under /api/govee.

**Why the cloud API and not something local.** Three transports were tested
against this network before writing a line of this:

  - *Govee LAN API* (UDP 4001/4002/4003, no key, no cloud): the one Govee
    device currently online here - an H6008 at 10.0.0.192 - answers on none
    of those ports. Either the model has no LAN mode or it is switched off in
    the app. A full /24 ARP sweep found no other Govee-OUI host.
  - *BLE*: impossible from the app at all. The container is an unprivileged
    LXC where AF_BLUETOOTH is not even a supported address family, and the
    Proxmox host's adapter - the only real radio in the building - sees the
    bulb at about -98 dBm, which is the noise floor rather than a link.
  - *Cloud*: works regardless of where the light is, whether it is on the same
    subnet, or whether anything of ours can see it. It is also the only one
    that can reach a device that is currently powered off and later comes back.

So this speaks to openapi.api.govee.com. The cost is a dependency on Govee's
servers and an API key; the benefit is that it actually works.

**The key lives in the database, not the environment.** Every other secret in
this app is an env var set at deploy time, but this one has to be obtainable
by the user from a phone app and pasted in - making that a redeploy would be a
bad trade. It is stored in `settings`, never logged, and never sent back to
the browser: /config returns only the last four characters so the UI can show
that *a* key is present without shipping it around.

Not profile-scoped: household lights, like the printer and /api/joint.
"""
import json
import time
import urllib.error
import urllib.request
import uuid

from flask import Blueprint, jsonify, request

from api import require_token, body
from db import connect

govee = Blueprint("govee", __name__, url_prefix="/api/govee")

BASE = "https://openapi.api.govee.com/router/api/v1"
TIMEOUT = 12

SETTING_KEY = "govee_api_key"
SETTING_PRIMARY = "govee_primary_device"

# Govee rate-limits per account and per device. A dashboard that re-renders on
# every toggle would burn that budget on nothing, so the device list is cached
# and state is only fetched when actually asked for.
DEVICE_CACHE_TTL = 300
_device_cache = {"at": 0.0, "data": None}

# The capability names the API expects. Kept in one place because they are
# long, easy to typo, and a wrong one fails as a generic 400.
CAP_POWER = ("devices.capabilities.on_off", "powerSwitch")
CAP_BRIGHTNESS = ("devices.capabilities.range", "brightness")
CAP_COLOR = ("devices.capabilities.color_setting", "colorRgb")
CAP_TEMP = ("devices.capabilities.color_setting", "colorTemperatureK")


# ---------------------------------------------------------------- settings

def _get_setting(key):
    conn = connect()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def _set_setting(key, value):
    conn = connect()
    try:
        if value is None:
            conn.execute("DELETE FROM settings WHERE key=?", (key,))
        else:
            conn.execute(
                "INSERT INTO settings (key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        conn.commit()
    finally:
        conn.close()


def api_key():
    return (_get_setting(SETTING_KEY) or "").strip()


# ------------------------------------------------------------- the client

class GoveeError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def _call(method, path, payload=None):
    """
    One Govee API call. Raises GoveeError with a message worth showing rather
    than letting a urllib exception reach the user.
    """
    key = api_key()
    if not key:
        raise GoveeError("no Govee API key saved", 428)

    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={
            "Govee-API-Key": key,
            "Content-Type": "application/json",
            "User-Agent": "OpsDeck/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            parsed = json.loads(res.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode() or "{}").get("message", "")
        except Exception:
            pass
        if e.code in (401, 403):
            raise GoveeError("Govee rejected the API key", 401)
        if e.code == 429:
            raise GoveeError("Govee rate limit hit - wait a minute", 429)
        raise GoveeError(f"Govee HTTP {e.code}{': ' + detail if detail else ''}", 502)
    except Exception as e:
        raise GoveeError(f"could not reach Govee: {type(e).__name__}", 502)

    # The API answers 200 with a non-zero code for application-level failures.
    code = parsed.get("code")
    if code not in (200, 0, None):
        raise GoveeError(f"Govee: {parsed.get('message') or 'error ' + str(code)}", 502)
    return parsed


def _control(sku, device, cap_type, instance, value):
    return _call("POST", "/device/control", {
        "requestId": str(uuid.uuid4()),
        "payload": {
            "sku": sku,
            "device": device,
            "capability": {"type": cap_type, "instance": instance, "value": value},
        },
    })


def list_devices(force=False):
    now = time.time()
    if not force and _device_cache["data"] is not None \
            and now - _device_cache["at"] < DEVICE_CACHE_TTL:
        return _device_cache["data"]

    parsed = _call("GET", "/user/devices")
    devices = []
    for d in parsed.get("data") or []:
        caps = {c.get("instance") for c in (d.get("capabilities") or [])}
        devices.append({
            "device": d.get("device"),
            "sku": d.get("sku"),
            "name": d.get("deviceName") or d.get("device"),
            "type": d.get("type"),
            "supports": {
                "power": "powerSwitch" in caps,
                "brightness": "brightness" in caps,
                "color": "colorRgb" in caps,
                "color_temp": "colorTemperatureK" in caps,
            },
        })
    _device_cache.update({"at": now, "data": devices})
    return devices


def _norm(value):
    return (value or "").strip().upper().replace(":", "").replace("-", "")


def _find(device_id):
    """
    Resolve a device id to its entry, so callers need not send the sku.

    Govee's API identifies a device by an *eight*-byte id - a two-byte prefix
    in front of the six-byte MAC, e.g. AB:CD:11:22:33:44:55:66 for the bulb
    whose MAC is 11:22:33:44:55:66. The MAC is what is printed on the device
    and shown in the phone app, so it is the thing a human actually has, and
    an exact-match lookup would reject it. Match the full id first, then fall
    back to a unique suffix - and refuse an ambiguous suffix rather than
    picking a light at random.
    """
    wanted = _norm(device_id)
    if not wanted:
        return None
    devices = list_devices()

    for d in devices:
        if _norm(d["device"]) == wanted:
            return d

    partial = [d for d in devices
               if _norm(d["device"]).endswith(wanted) or wanted.endswith(_norm(d["device"]))]
    return partial[0] if len(partial) == 1 else None


# ------------------------------------------------------------------ routes

@govee.route("/config", methods=["GET"])
@require_token
def get_config():
    key = api_key()
    return jsonify({
        "configured": bool(key),
        # Enough to recognise which key is saved, useless to anyone who sees it.
        "key_hint": ("…" + key[-4:]) if len(key) >= 4 else "",
        "primary_device": _get_setting(SETTING_PRIMARY) or "",
        "help_url": "https://developer.govee.com/reference/apply-you-govee-api-key",
    })


@govee.route("/key", methods=["PUT"])
@require_token
def put_key():
    value = (body().get("api_key") or "").strip()
    if not value:
        return jsonify({"error": "api_key required"}), 400
    if len(value) < 20:
        return jsonify({"error": "that does not look like a Govee API key"}), 400

    _set_setting(SETTING_KEY, value)
    _device_cache.update({"at": 0.0, "data": None})
    # Prove the key before reporting success - a saved-but-dead key is the
    # single most confusing state this section could be in.
    try:
        devices = list_devices(force=True)
    except GoveeError as e:
        return jsonify({"error": str(e), "saved": True, "verified": False}), e.status
    return jsonify({"saved": True, "verified": True, "device_count": len(devices)})


@govee.route("/key", methods=["DELETE"])
@require_token
def delete_key():
    _set_setting(SETTING_KEY, None)
    _device_cache.update({"at": 0.0, "data": None})
    return "", 204


@govee.route("/primary", methods=["PUT"])
@require_token
def put_primary():
    _set_setting(SETTING_PRIMARY, (body().get("device") or "").strip() or None)
    return jsonify({"primary_device": _get_setting(SETTING_PRIMARY) or ""})


@govee.route("/devices", methods=["GET"])
@require_token
def get_devices():
    try:
        devices = list_devices(force=request.args.get("refresh") == "1")
    except GoveeError as e:
        return jsonify({"error": str(e)}), e.status
    return jsonify({"devices": devices, "primary": _get_setting(SETTING_PRIMARY) or ""})


@govee.route("/state", methods=["GET"])
@require_token
def get_state():
    device_id = request.args.get("device") or _get_setting(SETTING_PRIMARY)
    if not device_id:
        return jsonify({"error": "device required"}), 400
    try:
        entry = _find(device_id)
        if not entry:
            return jsonify({"error": f"{device_id} is not in this Govee account"}), 404
        parsed = _call("POST", "/device/state", {
            "requestId": str(uuid.uuid4()),
            "payload": {"sku": entry["sku"], "device": entry["device"]},
        })
    except GoveeError as e:
        return jsonify({"error": str(e)}), e.status

    # Flatten the capability list into something a template can use directly.
    flat = {}
    for cap in ((parsed.get("payload") or {}).get("capabilities") or []):
        inst = cap.get("instance")
        val = (cap.get("state") or {}).get("value")
        if inst:
            flat[inst] = val
    return jsonify({
        "device": entry["device"],
        "sku": entry["sku"],
        "name": entry["name"],
        "online": flat.get("online", True),
        "power": flat.get("powerSwitch"),
        "brightness": flat.get("brightness"),
        "color": flat.get("colorRgb"),
        "color_temp": flat.get("colorTemperatureK"),
        "raw": flat,
    })


def _is_offline(entry):
    """
    Ask Govee whether a device is actually reachable.

    Only ever called after a control has already failed. Govee answers a
    command to an unplugged bulb with a bare "error 400", which tells the user
    nothing; one extra call on the failure path buys a real explanation
    without spending rate limit on every successful press.
    """
    try:
        parsed = _call("POST", "/device/state", {
            "requestId": str(uuid.uuid4()),
            "payload": {"sku": entry["sku"], "device": entry["device"]},
        })
    except GoveeError:
        return False
    for cap in ((parsed.get("payload") or {}).get("capabilities") or []):
        if cap.get("instance") == "online":
            return (cap.get("state") or {}).get("value") is False
    return False


@govee.route("/control", methods=["POST"])
@require_token
def post_control():
    d = body()
    device_id = (d.get("device") or _get_setting(SETTING_PRIMARY) or "").strip()
    action = (d.get("action") or "").strip()
    value = d.get("value")

    if not device_id:
        return jsonify({"error": "device required"}), 400

    try:
        entry = _find(device_id)
        if not entry:
            return jsonify({"error": f"{device_id} is not in this Govee account"}), 404
        sku, dev = entry["sku"], entry["device"]

        if action == "power":
            on = value in (1, "1", True, "on", "true")
            _control(sku, dev, *CAP_POWER, 1 if on else 0)
            result = {"power": 1 if on else 0}

        elif action == "brightness":
            try:
                pct = max(1, min(100, int(value)))
            except (TypeError, ValueError):
                return jsonify({"error": "brightness must be 1-100"}), 400
            _control(sku, dev, *CAP_BRIGHTNESS, pct)
            result = {"brightness": pct}

        elif action == "color":
            rgb = value or {}
            try:
                r = max(0, min(255, int(rgb.get("r"))))
                g = max(0, min(255, int(rgb.get("g"))))
                b = max(0, min(255, int(rgb.get("b"))))
            except (TypeError, ValueError, AttributeError):
                return jsonify({"error": "color needs r, g and b (0-255)"}), 400
            # Govee takes one packed integer, not three channels.
            _control(sku, dev, *CAP_COLOR, (r << 16) | (g << 8) | b)
            result = {"color": {"r": r, "g": g, "b": b}}

        elif action == "color_temp":
            try:
                kelvin = max(2000, min(9000, int(value)))
            except (TypeError, ValueError):
                return jsonify({"error": "color_temp must be 2000-9000"}), 400
            _control(sku, dev, *CAP_TEMP, kelvin)
            result = {"color_temp": kelvin}

        else:
            return jsonify({"error": f"unknown action {action!r}"}), 400

    except GoveeError as e:
        # Only worth asking "is it offline?" when the failure could plausibly
        # be the device. A missing or rejected key fails every call including
        # this one, and letting that second failure escape would turn an
        # honest 428 into a 500.
        if e.status not in (401, 428, 429):
            try:
                entry = _find(device_id)
                if entry and _is_offline(entry):
                    return jsonify({
                        "error": f"“{entry['name']}” is offline — Govee can't reach "
                                 f"it. It's either switched off at the wall or has "
                                 f"dropped off Wi-Fi.",
                        "offline": True,
                    }), 409
            except GoveeError:
                pass
        return jsonify({"error": str(e)}), e.status

    return jsonify({"ok": True, "device": dev, **result})
