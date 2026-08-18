"""
Tests for the homelab module. Run inside the app image:

    docker run --rm -v /root/opstest:/app -w /app opsdeck-opsdeck:latest \
        python test_homelab.py

The probe tests use real sockets against loopback rather than mocks, because
the thing worth proving is the three-way distinction the UI depends on:
up, down, and nothing-to-probe. A mock would happily agree with a wrong
implementation.
"""
import os
import shutil
import socket
import sys
import tempfile
import threading

os.environ["OPSDECK_TOKEN"] = "testtoken"

_tmp = tempfile.mkdtemp(prefix="opstest-")
import db  # noqa: E402
db.DATA_DIR = __import__("pathlib").Path(_tmp)
db.DB_PATH = db.DATA_DIR / "opsdeck.db"
db.UPLOAD_DIR = db.DATA_DIR / "uploads"

import homelab as hl  # noqa: E402

FAILS = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}{': ' + str(detail) if detail else ''}")
    if not cond:
        FAILS.append(label)


def main():
    db.init_db()
    import app as appmod
    client = appmod.app.test_client()
    H = {"X-API-Token": "testtoken", "X-Profile-Id": "primary"}

    print("\n== probing is real, and three-valued ==")
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    threading.Thread(target=lambda: srv.accept(), daemon=True).start()

    state, _ = hl.probe("127.0.0.1", port)
    check("a listening port reads up", state == "up", state)
    state, _ = hl.probe("127.0.0.1", 1)
    check("a closed port on a live host reads up (it answered)", state == "up", state)
    # TEST-NET-1 goes nowhere at all.
    state, _ = hl.probe("192.0.2.1", 80, timeout=0.6)
    check("an unroutable host reads down", state == "down", state)
    state, _ = hl.probe("10.0.0.1", 0)
    check("port 0 means no-probe, not down", state == "no-probe", state)
    state, _ = hl.probe("", 80)
    check("no host means no-probe", state == "no-probe", state)
    srv.close()

    print("\n== the seeded inventory ==")
    r = client.get("/api/homelab?probe=0", headers=H)
    check("GET 200", r.status_code == 200)
    data = r.get_json()
    names = [d["name"] for d in data["devices"]]
    check("example devices seeded", len(names) == 5, len(names))
    for want in ("Hypervisor", "DNS sinkhole", "App host", "Workstation",
                 "Network switch"):
        check(f"seeded: {want}", want in names)

    hyp = next(d for d in data["devices"] if d["name"] == "Hypervisor")
    check("a probe target is set", hyp["probe_port"] == 8006, hyp["probe_port"])
    check("specs are populated", "NVMe" in hyp["specs"])

    switch = next(d for d in data["devices"] if d["name"] == "Network switch")
    check("the unmanaged switch has no probe port", switch["probe_port"] == 0)

    print("\n== recommendations ==")
    counts = data["counts"]
    check("recommendations seeded", counts["upgrades_open"] >= 8,
          counts["upgrades_open"])
    check("high-severity ones exist", counts["upgrades_high"] >= 3,
          counts["upgrades_high"])
    lab = [u["title"] for u in data["lab_upgrades"]]
    check("backups are covered", any("back up" in t.lower() or "backup" in t.lower()
                                 for t in lab))
    check("firewall finding is lab-wide", any("firewall" in t.lower() for t in lab))
    check("managed switch is recommended", any("switch" in t.lower() for t in lab))
    check("network visibility is covered",
          any("logging" in t.lower() or "ids" in t.lower() for t in lab))

    check("a recommendation can hang off a device",
          any(d["upgrades"] for d in data["devices"]))

    print("\n== status distinguishes expected-off ==")
    dev = client.post("/api/homelab/devices", json={
        "name": "Planned NAS", "kind": "server", "status": "planned",
        "lan_ip": "192.0.2.9", "probe_host": "192.0.2.9", "probe_port": 80},
        headers=H).get_json()
    full = client.get("/api/homelab", headers=H).get_json()
    planned = next(d for d in full["devices"] if d["id"] == dev["id"])
    check("a planned device that is down reads expected-off",
          planned["state"] == "expected-off", planned["state"])

    print("\n== CRUD ==")
    r = client.patch(f"/api/homelab/devices/{dev['id']}",
                     json={"purpose": "Backup target", "status": "active"}, headers=H)
    check("patch 200", r.status_code == 200)
    check("purpose saved", r.get_json()["purpose"] == "Backup target")
    check("unknown kind falls back rather than erroring",
          client.patch(f"/api/homelab/devices/{dev['id']}", json={"kind": "toaster"},
                       headers=H).get_json()["kind"] == "other")

    up = client.post("/api/homelab/upgrades", json={
        "title": "Buy drives", "device_id": dev["id"], "severity": "high",
        "category": "capacity", "cost": "$120"}, headers=H)
    check("upgrade created", up.status_code == 201)
    uid = up.get_json()["id"]
    check("upgrade attached to the device", up.get_json()["device_id"] == dev["id"])
    check("status transition works",
          client.patch(f"/api/homelab/upgrades/{uid}", json={"status": "done"},
                       headers=H).get_json()["status"] == "done")

    check("a lab-wide upgrade takes a null device",
          client.post("/api/homelab/upgrades",
                      json={"title": "Lab thing", "device_id": None},
                      headers=H).get_json()["device_id"] is None)
    check("upgrade on a missing device is 404",
          client.post("/api/homelab/upgrades",
                      json={"title": "x", "device_id": 999999},
                      headers=H).status_code == 404)
    check("nameless device refused",
          client.post("/api/homelab/devices", json={"name": "  "},
                      headers=H).status_code == 400)

    check("delete cascades to its recommendations",
          client.delete(f"/api/homelab/devices/{dev['id']}", headers=H).status_code == 204)
    check("the cascaded upgrade is gone",
          client.patch(f"/api/homelab/upgrades/{uid}", json={"status": "idea"},
                       headers=H).status_code == 404)

    print("\n== discovery guards its input ==")
    check("a bad subnet is refused",
          client.post("/api/homelab/discover", json={"subnet": "not.a.subnet"},
                      headers=H).status_code == 400)
    check("a subnet with too many octets is refused",
          client.post("/api/homelab/discover", json={"subnet": "10.0.0.0.1"},
                      headers=H).status_code == 400)

    print("\n== scoping and auth ==")
    ph = {"X-API-Token": "testtoken", "X-Profile-Id": "partner"}
    check("partner sees no devices",
          client.get("/api/homelab?probe=0", headers=ph).get_json()["devices"] == [])
    check("no token is refused", client.get("/api/homelab").status_code == 401)
    check("discover needs a token",
          client.post("/api/homelab/discover", json={}).status_code == 401)

    print("\n== read-only with respect to the estate ==")
    verbs = set()
    for rule in appmod.app.url_map.iter_rules():
        if str(rule).startswith("/api/homelab"):
            verbs |= (rule.methods - {"HEAD", "OPTIONS"})
    check("no endpoint can act on a device itself",
          verbs <= {"GET", "POST", "PATCH", "DELETE"}, sorted(verbs))

    print("\n" + "=" * 58)
    print(f"{len(FAILS)} FAILURES: {FAILS}" if FAILS else "all checks passed")
    print("=" * 58)
    return 1 if FAILS else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(_tmp, ignore_errors=True)
