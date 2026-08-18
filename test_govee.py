"""
Tests for the Govee module. Run inside the app image:

    docker run --rm -v /root/opstest:/app -w /app opsdeck-opsdeck:latest \
        python test_govee.py

No API key is available here, so the emphasis is on the two things that
actually matter before one exists: that every endpoint degrades honestly
without a key, and that the request bodies sent to Govee are exactly right -
verified by intercepting them rather than by hitting the live service.
"""
import json
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

import govee as gv  # noqa: E402

# Govee's own id for this bulb is eight bytes: a two-byte prefix in front
# of the MAC printed on the device.
TARGET = "AB:CD:11:22:33:44:55:66"
MAC = "11:22:33:44:55:66"
FAILS = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}{': ' + str(detail) if detail else ''}")
    if not cond:
        FAILS.append(label)


def main():
    db.init_db()
    import app as appmod
    client = appmod.app.test_client()
    H = {"X-API-Token": "testtoken"}

    print("\n== with no key saved ==")
    r = client.get("/api/govee/config", headers=H)
    check("config 200 even unconfigured", r.status_code == 200)
    check("reports unconfigured", r.get_json()["configured"] is False)
    check("no key hint to leak", r.get_json()["key_hint"] == "")

    r = client.get("/api/govee/devices", headers=H)
    check("devices says 428 (needs a key), not 500", r.status_code == 428, r.status_code)
    check("with a readable reason", "key" in r.get_json()["error"].lower(),
          r.get_json()["error"])

    r = client.post("/api/govee/control",
                    json={"device": TARGET, "action": "power", "value": 1}, headers=H)
    check("control without a key is 428", r.status_code == 428, r.status_code)

    print("\n== key validation ==")
    check("empty key refused",
          client.put("/api/govee/key", json={"api_key": ""}, headers=H).status_code == 400)
    check("obviously-too-short key refused",
          client.put("/api/govee/key", json={"api_key": "abc"}, headers=H).status_code == 400)

    print("\n== the key never comes back to the browser ==")
    FAKE = "0123456789abcdef0123456789abcdef01234567"
    gv._set_setting(gv.SETTING_KEY, FAKE)
    cfg = client.get("/api/govee/config", headers=H).get_json()
    check("configured now reads true", cfg["configured"] is True)
    check("only the last 4 are exposed", cfg["key_hint"] == "…4567", cfg["key_hint"])
    blob = json.dumps(cfg)
    check("the key itself is absent from the payload", FAKE not in blob)

    print("\n== the exact requests sent to Govee ==")
    # Intercept at the transport instead of calling the real service: what is
    # worth asserting is the capability names and the RGB packing, and those
    # are wrong or right regardless of whether a key exists.
    sent = []

    def fake_call(method, path, payload=None):
        sent.append((method, path, payload))
        if path == "/user/devices":
            return {"code": 200, "data": [{
                "sku": "H6008", "device": TARGET, "deviceName": "Desk Lamp",
                "type": "devices.types.light",
                "capabilities": [
                    {"instance": "powerSwitch"}, {"instance": "brightness"},
                    {"instance": "colorRgb"}, {"instance": "colorTemperatureK"},
                ],
            }]}
        return {"code": 200, "payload": {"capabilities": [
            {"instance": "powerSwitch", "state": {"value": 1}},
            {"instance": "brightness", "state": {"value": 62}},
            {"instance": "colorRgb", "state": {"value": 16711935}},
        ]}}

    real_call = gv._call
    gv._call = fake_call
    gv._device_cache.update({"at": 0.0, "data": None})
    try:
        r = client.get("/api/govee/devices", headers=H)
        devs = r.get_json()["devices"]
        check("device list parsed", len(devs) == 1, devs)
        check("capabilities flattened", devs[0]["supports"] == {
            "power": True, "brightness": True, "color": True, "color_temp": True})

        sent.clear()
        client.post("/api/govee/control",
                    json={"device": TARGET, "action": "power", "value": 1}, headers=H)
        cap = sent[-1][2]["payload"]["capability"]
        check("power uses the on_off capability",
              cap["type"] == "devices.capabilities.on_off" and cap["instance"] == "powerSwitch",
              cap)
        check("power value is 1", cap["value"] == 1)

        sent.clear()
        client.post("/api/govee/control",
                    json={"device": TARGET, "action": "power", "value": "off"}, headers=H)
        check("'off' maps to 0", sent[-1][2]["payload"]["capability"]["value"] == 0)

        sent.clear()
        client.post("/api/govee/control",
                    json={"device": TARGET, "action": "brightness", "value": 55}, headers=H)
        cap = sent[-1][2]["payload"]["capability"]
        check("brightness capability", cap["instance"] == "brightness" and cap["value"] == 55, cap)

        sent.clear()
        client.post("/api/govee/control", json={
            "device": TARGET, "action": "color", "value": {"r": 255, "g": 0, "b": 255}},
            headers=H)
        cap = sent[-1][2]["payload"]["capability"]
        # Govee wants one packed int, not three channels: 0xFF00FF.
        check("rgb packed into a single int", cap["value"] == 16711935, cap["value"])
        check("colour uses colorRgb", cap["instance"] == "colorRgb")

        sent.clear()
        client.post("/api/govee/control",
                    json={"device": TARGET, "action": "color_temp", "value": 2700}, headers=H)
        check("colour temp capability",
              sent[-1][2]["payload"]["capability"]["instance"] == "colorTemperatureK")

        print("\n== clamping and rejection ==")
        sent.clear()
        client.post("/api/govee/control",
                    json={"device": TARGET, "action": "brightness", "value": 999}, headers=H)
        check("brightness clamped to 100",
              sent[-1][2]["payload"]["capability"]["value"] == 100)
        sent.clear()
        client.post("/api/govee/control",
                    json={"device": TARGET, "action": "color_temp", "value": 100}, headers=H)
        check("kelvin clamped to 2000",
              sent[-1][2]["payload"]["capability"]["value"] == 2000)
        check("non-numeric brightness is 400",
              client.post("/api/govee/control",
                          json={"device": TARGET, "action": "brightness", "value": "bright"},
                          headers=H).status_code == 400)
        check("unknown action is 400",
              client.post("/api/govee/control",
                          json={"device": TARGET, "action": "explode"},
                          headers=H).status_code == 400)
        check("a device not on the account is 404",
              client.post("/api/govee/control",
                          json={"device": "AA:BB:CC:DD:EE:FF", "action": "power", "value": 1},
                          headers=H).status_code == 404)

        print("\n== the MAC a human actually has resolves to the API's 8-byte id ==")
        # Govee identifies this bulb as E2:A0:<mac>; the label on the device and
        # the phone app both show only the MAC.
        sent.clear()
        r = client.post("/api/govee/control",
                        json={"device": MAC, "action": "power", "value": 1},
                        headers=H)
        check("bare MAC is accepted", r.status_code == 200, r.get_json())
        check("and resolves to the full id",
              sent[-1][2]["payload"]["device"] == TARGET, sent[-1][2]["payload"]["device"])
        check("case and separators do not matter",
              client.post("/api/govee/control",
                          json={"device": "60-74-f4-1c-0c-72", "action": "power", "value": 1},
                          headers=H).status_code == 200)
        check("a suffix matching nothing is still 404",
              client.post("/api/govee/control",
                          json={"device": "11:22:33:44:55:66", "action": "power", "value": 1},
                          headers=H).status_code == 404)

        print("\n== state flattening ==")
        st = client.get(f"/api/govee/state?device={TARGET}", headers=H).get_json()
        check("power surfaced", st["power"] == 1)
        check("brightness surfaced", st["brightness"] == 62)
        check("name surfaced", st["name"] == "Desk Lamp", st["name"])

        print("\n== primary device ==")
        client.put("/api/govee/primary", json={"device": TARGET}, headers=H)
        check("primary persisted",
              client.get("/api/govee/config", headers=H).get_json()["primary_device"] == TARGET)
        sent.clear()
        # No device in the body: it must fall back to the saved primary.
        client.post("/api/govee/control", json={"action": "power", "value": 1}, headers=H)
        check("control falls back to the primary device",
              sent[-1][2]["payload"]["device"] == TARGET)
    finally:
        gv._call = real_call

    print("\n== auth ==")
    check("config needs a token", client.get("/api/govee/config").status_code == 401)
    check("control needs a token",
          client.post("/api/govee/control", json={"action": "power"}).status_code == 401)

    print("\n== key removal ==")
    check("delete 204", client.delete("/api/govee/key", headers=H).status_code == 204)
    check("back to unconfigured",
          client.get("/api/govee/config", headers=H).get_json()["configured"] is False)

    print("\n" + "=" * 58)
    print(f"{len(FAILS)} FAILURES: {FAILS}" if FAILS else "all checks passed")
    print("=" * 58)
    return 1 if FAILS else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(_tmp, ignore_errors=True)
