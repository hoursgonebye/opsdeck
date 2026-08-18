"""
Homelab - the device inventory and what to do about it, under /api/homelab.

Two ideas hold this together:

**Reachability is probed, never stored.** A cached "online" flag is wrong the
instant something is unplugged, so /status opens a TCP connection at read
time, in parallel, with a short timeout. It is deliberately TCP and not ICMP:
the app image has no ping binary, and raw sockets inside Docker inside an
unprivileged LXC is a fight with no payoff. A device with probe_port 0 simply
has nothing to knock on - an unmanaged switch is not down, it is silent - and
reports "no probe" rather than a red dot it does not deserve.

**Recommendations are first-class rows, not prose.** Every one carries a
category, a severity and a cost, and hangs off a device or off the lab as a
whole. That is what makes this an inventory you can act on instead of a wiki
page that rots: the open high-severity items sort to the top on their own.

Not profile-scoped in spirit - one household, one lab - but rows carry
profile_id so the partner profile does not inherit a rack she never asked for,
the same choice the skill tree makes.

Everything here is read-only with respect to the estate itself: nothing in
this module can restart a container, change a config, or touch a device. It
describes and it probes. Control lives in Proxmox, Fluidd, and the shell.
"""
import concurrent.futures
import socket

from flask import Blueprint, jsonify, request

from api import require_token, resolve_profile, active_profile, body, one, many
from db import connect

homelab = Blueprint("homelab", __name__, url_prefix="/api/homelab")
homelab.before_request(resolve_profile)

KINDS = ("server", "guest", "laptop", "sbc", "workstation", "printer",
         "network", "iot", "phone", "other")
STATUSES = ("active", "building", "planned", "retired")
CATEGORIES = ("security", "reliability", "performance", "capacity", "cost",
              "capability")
SEVERITIES = ("high", "medium", "low")
UPGRADE_STATUSES = ("idea", "planned", "doing", "done", "declined")

# Short enough that a whole page of dead hosts still renders promptly: eight
# devices at 1.2s, probed in parallel, is one slow second not eight.
PROBE_TIMEOUT = 1.2
PROBE_WORKERS = 12


def _clean(value, allowed, fallback):
    v = (value or "").strip().lower()
    return v if v in allowed else fallback


def probe(host, port, timeout=PROBE_TIMEOUT):
    """
    One TCP connect. Returns 'up', 'down', or 'no-probe'.

    A refused connection still proves something is answering at that address,
    so it counts as up: an unmanaged service on a live host is not the same
    thing as a dark host, and conflating them would show half the lab as down.
    """
    if not host or not port:
        return "no-probe", None
    s = socket.socket()
    s.settimeout(timeout)
    try:
        rc = s.connect_ex((host, int(port)))
    except (OSError, ValueError):
        return "down", None
    finally:
        s.close()
    if rc == 0:
        return "up", rc
    # ECONNREFUSED means a host answered and said no. Anything else (timeout,
    # no route) means nothing was there.
    return ("up" if rc in (111, 10061) else "down"), rc


def probe_all(devices):
    """Probe every device concurrently; returns {device_id: (state, rc)}."""
    out = {}
    targets = [(d["id"], d["probe_host"] or d["lan_ip"] or d["tailscale_ip"],
                d["probe_port"]) for d in devices]
    if not targets:
        return out
    with concurrent.futures.ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
        futures = {pool.submit(probe, host, port): did for did, host, port in targets}
        for fut in concurrent.futures.as_completed(futures):
            did = futures[fut]
            try:
                out[did] = fut.result()
            except Exception:
                out[did] = ("down", None)
    return out


def load(conn, profile_id, with_status=True):
    devices = many(conn, "SELECT * FROM lab_devices WHERE profile_id=? "
                         "ORDER BY position, id", (profile_id,))
    upgrades = many(conn, "SELECT * FROM lab_upgrades WHERE profile_id=? "
                          "ORDER BY position, id", (profile_id,))

    states = probe_all(devices) if with_status else {}
    by_device = {}
    for u in upgrades:
        by_device.setdefault(u["device_id"], []).append(u)

    for d in devices:
        state, rc = states.get(d["id"], ("unknown", None))
        # A device nobody expects to be on should not read as a failure.
        if d["status"] in ("planned", "retired") and state == "down":
            state = "expected-off"
        d["state"] = state
        d["upgrades"] = by_device.get(d["id"], [])

    open_high = [u for u in upgrades
                 if u["severity"] == "high" and u["status"] not in ("done", "declined")]
    return {
        "devices": devices,
        # device_id NULL: the recommendations that are about the lab, not a box.
        "lab_upgrades": by_device.get(None, []),
        "counts": {
            "devices": len(devices),
            "up": sum(1 for d in devices if d["state"] == "up"),
            "down": sum(1 for d in devices if d["state"] == "down"),
            "building": sum(1 for d in devices if d["status"] == "building"),
            "upgrades_open": sum(1 for u in upgrades
                                 if u["status"] not in ("done", "declined")),
            "upgrades_high": len(open_high),
            "upgrades_done": sum(1 for u in upgrades if u["status"] == "done"),
        },
        "kinds": list(KINDS),
        "categories": list(CATEGORIES),
    }


# ------------------------------------------------------------------ reading

@homelab.route("", methods=["GET"])
@homelab.route("/", methods=["GET"])
@require_token
def get_all():
    conn = connect()
    try:
        # ?probe=0 for a fast render when only the text matters.
        return jsonify(load(conn, active_profile(),
                            with_status=request.args.get("probe") != "0"))
    finally:
        conn.close()


@homelab.route("/status", methods=["GET"])
@require_token
def get_status():
    """Just the reachability pass - cheap enough to poll on its own."""
    conn = connect()
    try:
        devices = many(conn, "SELECT * FROM lab_devices WHERE profile_id=? "
                             "ORDER BY position, id", (active_profile(),))
    finally:
        conn.close()
    states = probe_all(devices)
    return jsonify({str(d["id"]): {"name": d["name"], "state": states.get(d["id"], ("unknown",))[0]}
                    for d in devices})


# ------------------------------------------------------------------ devices

def _device_fields(d, existing=None):
    e = existing or {}
    out = {
        "name": (d.get("name") if "name" in d else e.get("name", "")) or "",
        "kind": _clean(d.get("kind", e.get("kind")), KINDS, "other"),
        "status": _clean(d.get("status", e.get("status")), STATUSES, "active"),
    }
    for f in ("purpose", "specs", "hostname", "lan_ip", "tailscale_ip", "mac",
              "probe_host", "notes"):
        out[f] = (d[f] if f in d else e.get(f, "")) or ""
    try:
        out["probe_port"] = int(d["probe_port"] if "probe_port" in d
                                else e.get("probe_port", 0) or 0)
    except (TypeError, ValueError):
        out["probe_port"] = 0
    return out


@homelab.route("/devices", methods=["POST"])
@require_token
def create_device():
    f = _device_fields(body())
    if not f["name"].strip():
        return jsonify({"error": "name required"}), 400
    conn = connect()
    try:
        pid = active_profile()
        pos = conn.execute("SELECT COALESCE(MAX(position),-1)+1 FROM lab_devices "
                           "WHERE profile_id=?", (pid,)).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO lab_devices (profile_id,name,kind,status,purpose,specs,"
            "hostname,lan_ip,tailscale_ip,mac,probe_host,probe_port,notes,position) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, f["name"], f["kind"], f["status"], f["purpose"], f["specs"],
             f["hostname"], f["lan_ip"], f["tailscale_ip"], f["mac"],
             f["probe_host"], f["probe_port"], f["notes"], pos))
        conn.commit()
        return jsonify(one(conn, "SELECT * FROM lab_devices WHERE id=?",
                           (cur.lastrowid,))), 201
    finally:
        conn.close()


@homelab.route("/devices/<int:did>", methods=["PATCH"])
@require_token
def update_device(did):
    conn = connect()
    try:
        row = one(conn, "SELECT * FROM lab_devices WHERE id=? AND profile_id=?",
                  (did, active_profile()))
        if not row:
            return jsonify({"error": "device not found"}), 404
        f = _device_fields(body(), row)
        if "position" in body():
            try:
                f["position"] = int(body()["position"])
            except (TypeError, ValueError):
                f["position"] = row["position"]
        else:
            f["position"] = row["position"]
        conn.execute(
            "UPDATE lab_devices SET name=?,kind=?,status=?,purpose=?,specs=?,"
            "hostname=?,lan_ip=?,tailscale_ip=?,mac=?,probe_host=?,probe_port=?,"
            "notes=?,position=? WHERE id=?",
            (f["name"], f["kind"], f["status"], f["purpose"], f["specs"],
             f["hostname"], f["lan_ip"], f["tailscale_ip"], f["mac"],
             f["probe_host"], f["probe_port"], f["notes"], f["position"], did))
        conn.commit()
        return jsonify(one(conn, "SELECT * FROM lab_devices WHERE id=?", (did,)))
    finally:
        conn.close()


@homelab.route("/devices/<int:did>", methods=["DELETE"])
@require_token
def delete_device(did):
    conn = connect()
    try:
        if not one(conn, "SELECT id FROM lab_devices WHERE id=? AND profile_id=?",
                   (did, active_profile())):
            return jsonify({"error": "device not found"}), 404
        conn.execute("DELETE FROM lab_devices WHERE id=?", (did,))
        conn.commit()
        return "", 204
    finally:
        conn.close()


@homelab.route("/devices/<int:did>/probe", methods=["GET"])
@require_token
def probe_one(did):
    conn = connect()
    try:
        row = one(conn, "SELECT * FROM lab_devices WHERE id=? AND profile_id=?",
                  (did, active_profile()))
    finally:
        conn.close()
    if not row:
        return jsonify({"error": "device not found"}), 404
    host = row["probe_host"] or row["lan_ip"] or row["tailscale_ip"]
    state, rc = probe(host, row["probe_port"])
    return jsonify({"device_id": did, "host": host, "port": row["probe_port"],
                    "state": state, "code": rc})


# ----------------------------------------------------------------- upgrades

def _upgrade_fields(d, existing=None):
    e = existing or {}
    out = {
        "title": (d.get("title") if "title" in d else e.get("title", "")) or "",
        "category": _clean(d.get("category", e.get("category")), CATEGORIES, "performance"),
        "severity": _clean(d.get("severity", e.get("severity")), SEVERITIES, "medium"),
        "status": _clean(d.get("status", e.get("status")), UPGRADE_STATUSES, "idea"),
    }
    for f in ("detail", "cost"):
        out[f] = (d[f] if f in d else e.get(f, "")) or ""
    if "device_id" in d:
        out["device_id"] = int(d["device_id"]) if d["device_id"] else None
    else:
        out["device_id"] = e.get("device_id")
    return out


@homelab.route("/upgrades", methods=["POST"])
@require_token
def create_upgrade():
    f = _upgrade_fields(body())
    if not f["title"].strip():
        return jsonify({"error": "title required"}), 400
    conn = connect()
    try:
        pid = active_profile()
        if f["device_id"] and not one(
                conn, "SELECT id FROM lab_devices WHERE id=? AND profile_id=?",
                (f["device_id"], pid)):
            return jsonify({"error": "device not found"}), 404
        pos = conn.execute("SELECT COALESCE(MAX(position),-1)+1 FROM lab_upgrades "
                           "WHERE profile_id=?", (pid,)).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO lab_upgrades (profile_id,device_id,title,detail,category,"
            "severity,cost,status,position) VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, f["device_id"], f["title"], f["detail"], f["category"],
             f["severity"], f["cost"], f["status"], pos))
        conn.commit()
        return jsonify(one(conn, "SELECT * FROM lab_upgrades WHERE id=?",
                           (cur.lastrowid,))), 201
    finally:
        conn.close()


@homelab.route("/upgrades/<int:uid>", methods=["PATCH"])
@require_token
def update_upgrade(uid):
    conn = connect()
    try:
        row = one(conn, "SELECT * FROM lab_upgrades WHERE id=? AND profile_id=?",
                  (uid, active_profile()))
        if not row:
            return jsonify({"error": "upgrade not found"}), 404
        f = _upgrade_fields(body(), row)
        conn.execute(
            "UPDATE lab_upgrades SET device_id=?,title=?,detail=?,category=?,"
            "severity=?,cost=?,status=? WHERE id=?",
            (f["device_id"], f["title"], f["detail"], f["category"],
             f["severity"], f["cost"], f["status"], uid))
        conn.commit()
        return jsonify(one(conn, "SELECT * FROM lab_upgrades WHERE id=?", (uid,)))
    finally:
        conn.close()


@homelab.route("/upgrades/<int:uid>", methods=["DELETE"])
@require_token
def delete_upgrade(uid):
    conn = connect()
    try:
        if not one(conn, "SELECT id FROM lab_upgrades WHERE id=? AND profile_id=?",
                   (uid, active_profile())):
            return jsonify({"error": "upgrade not found"}), 404
        conn.execute("DELETE FROM lab_upgrades WHERE id=?", (uid,))
        conn.commit()
        return "", 204
    finally:
        conn.close()


# ------------------------------------------------------------------- recon
#
# A read-only sweep the user triggers. It exists so the inventory can be
# checked against reality rather than believed - the gap between what a wiki
# says is on the network and what is actually on it is the whole reason
# documentation like this rots.

@homelab.route("/discover", methods=["POST"])
@require_token
def discover():
    """
    Knock on a /24 and report which addresses answer, flagging any that are
    not already in the inventory. Writes nothing.
    """
    d = body()
    subnet = (d.get("subnet") or "192.168.1").strip().rstrip(".")
    if not all(p.isdigit() and 0 <= int(p) <= 255 for p in subnet.split(".")[:3]) \
            or subnet.count(".") != 2:
        return jsonify({"error": "subnet must look like 192.168.1"}), 400
    try:
        ports = [int(p) for p in (d.get("ports") or [22, 80, 443, 8006])][:6]
    except (TypeError, ValueError):
        return jsonify({"error": "ports must be numbers"}), 400

    conn = connect()
    try:
        known = {r["lan_ip"]: r["name"] for r in many(
            conn, "SELECT name, lan_ip FROM lab_devices WHERE profile_id=? AND lan_ip<>''",
            (active_profile(),))}
    finally:
        conn.close()

    def sweep(host):
        for port in ports:
            state, _ = probe(host, port, timeout=0.4)
            if state == "up":
                return {"ip": host, "port": port,
                        "known_as": known.get(host), "new": host not in known}
        return None

    hosts = [f"{subnet}.{i}" for i in range(1, 255)]
    found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        for r in pool.map(sweep, hosts):
            if r:
                found.append(r)
    found.sort(key=lambda r: int(r["ip"].rsplit(".", 1)[1]))
    return jsonify({"subnet": subnet, "ports_tried": ports,
                    "found": found,
                    "new_count": sum(1 for r in found if r["new"])})
