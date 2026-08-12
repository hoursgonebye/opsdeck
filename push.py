"""
Web Push delivery. This is what makes a notification reach a phone with the
app closed - including iPhones, where Safari has supported Web Push since
iOS 16.4 for sites installed to the Home Screen.

The moving parts:

- **VAPID keypair**, generated once and persisted at data/vapid_private.pem
  (the data dir is the bind-mounted volume, so rebuilds keep the identity -
  losing the key would orphan every subscription).
- **push_subscriptions** (schema v11): one row per browser/device per
  profile. Endpoints that return 404/410 are pruned on send - that's a
  device that unsubscribed or expired, not an error worth retrying.
- **send_to_profile()**, called from social.notify(), so every in-app
  notification the app already produces - pings, mailbox deliveries,
  milestones, reminders, the cashflow guard - reaches devices with no per-
  feature push code.

Sends run on a daemon thread: notify() is called mid-transaction inside
request handlers, and a slow push service must never hold up a request.
"""
import json
import os
import threading

from db import connect, DATA_DIR

VAPID_PEM = DATA_DIR / "vapid_private.pem"
VAPID_SUB = os.environ.get("OPSDECK_VAPID_SUB", "mailto:opsdeck@example.invalid")

_lock = threading.Lock()
_public_key_cache = None


def _vapid_private():
    """The PEM path, generating a keypair on first use (idempotent)."""
    if not VAPID_PEM.exists():
        with _lock:
            if not VAPID_PEM.exists():
                from py_vapid import Vapid
                v = Vapid()
                v.generate_keys()
                v.save_key(str(VAPID_PEM))
    return str(VAPID_PEM)


def public_key():
    """The applicationServerKey browsers need: base64url of the raw
    uncompressed P-256 point."""
    global _public_key_cache
    if _public_key_cache:
        return _public_key_cache
    import base64
    from py_vapid import Vapid
    from cryptography.hazmat.primitives import serialization
    v = Vapid.from_file(_vapid_private())
    raw = v.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    _public_key_cache = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return _public_key_cache


def save_subscription(conn, profile_id, sub, claim=False):
    """
    Store one browser's subscription. Upserts on endpoint - a device
    re-subscribing is the same device, not a second one.

    A device belongs to whoever *explicitly* claimed it (the notifications
    button, the device-binding chooser). Silent boot-time refreshes pass
    claim=False and keep the existing owner - otherwise merely browsing the
    partner's tab at page load would quietly steal the device into her
    notification pool.
    """
    endpoint = (sub.get("endpoint") or "").strip()
    keys = sub.get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        return False
    if claim:
        conn.execute(
            "INSERT INTO push_subscriptions (profile_id, endpoint, p256dh, auth) "
            "VALUES (?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET "
            "profile_id=excluded.profile_id, p256dh=excluded.p256dh, auth=excluded.auth",
            (profile_id, endpoint, keys["p256dh"], keys["auth"]))
    else:
        conn.execute(
            "INSERT INTO push_subscriptions (profile_id, endpoint, p256dh, auth) "
            "VALUES (?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET "
            "p256dh=excluded.p256dh, auth=excluded.auth",
            (profile_id, endpoint, keys["p256dh"], keys["auth"]))
    return True


def drop_subscription(conn, endpoint):
    return conn.execute("DELETE FROM push_subscriptions WHERE endpoint=?",
                        (endpoint,)).rowcount


def _send_one(row, payload):
    """One push. Returns False when the subscription is dead."""
    from pywebpush import webpush, WebPushException
    try:
        webpush(
            subscription_info={"endpoint": row["endpoint"],
                               "keys": {"p256dh": row["p256dh"], "auth": row["auth"]}},
            data=json.dumps(payload),
            vapid_private_key=_vapid_private(),
            vapid_claims={"sub": VAPID_SUB},
            ttl=3600,
        )
        return True
    except WebPushException as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code in (404, 410):
            return False          # gone: device unsubscribed or expired
        print(f"  [push] send failed ({code}): {e}", flush=True)
        return True               # transient: keep the subscription
    except Exception as e:
        print(f"  [push] send crashed: {e}", flush=True)
        return True


def send_to_profile(profile_id, title, body="", link=None, tag=None):
    """Deliver to every device subscribed for a profile. Blocking; most
    callers want send_async."""
    conn = connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM push_subscriptions WHERE profile_id=?", (profile_id,))]
    finally:
        conn.close()
    if not rows:
        return 0

    payload = {"title": title, "body": (body or "")[:180], "link": link, "tag": tag}
    dead, sent = [], 0
    for row in rows:
        if _send_one(row, payload):
            sent += 1
        else:
            dead.append(row["endpoint"])
    if dead:
        conn = connect()
        try:
            for ep in dead:
                drop_subscription(conn, ep)
            conn.commit()
        finally:
            conn.close()
    return sent


def send_async(profile_id, title, body="", link=None, tag=None):
    """Fire-and-forget: notify() runs inside request handlers and a slow
    push relay must never hold a request open."""
    threading.Thread(
        target=send_to_profile, args=(profile_id, title, body, link, tag),
        name="push-send", daemon=True,
    ).start()
