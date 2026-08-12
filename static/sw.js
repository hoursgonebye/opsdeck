// Service worker: real Web Push (server sends via VAPID + pywebpush) plus
// notification click-through. Works on iOS 16.4+ when the app is installed
// to the Home Screen - Safari only grants push to installed web apps.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const link = event.notification.data && event.notification.data.link;
  const target = link && link.startsWith("#") ? "/" + link : "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      for (const client of list) {
        if ("focus" in client) {
          if (link && "navigate" in client) client.navigate(target);
          return client.focus();
        }
      }
      return self.clients.openWindow(target);
    })
  );
});

self.addEventListener("push", (event) => {
  if (!event.data) return;
  let payload = {};
  try { payload = event.data.json(); } catch (e) { payload = { title: event.data.text() }; }
  const opts = {
    body: payload.body || "",
    icon: "/static/icons/icon-192.png",
    badge: "/static/icons/icon-192.png",
    data: { link: payload.link || null },
  };
  // renotify is only legal alongside a tag, and it is what makes a
  // replacement actually alert instead of swapping in silently.
  if (payload.tag) { opts.tag = payload.tag; opts.renotify = true; }
  event.waitUntil(self.registration.showNotification(payload.title || "Ops Deck", opts));
});

// A push service can rotate a subscription underneath us; re-register the
// replacement so the device doesn't silently go deaf.
self.addEventListener("pushsubscriptionchange", (event) => {
  event.waitUntil((async () => {
    try {
      const sub = await self.registration.pushManager.subscribe(
        event.oldSubscription ? event.oldSubscription.options : { userVisibleOnly: true });
      await fetch("/api/push/subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ subscription: sub.toJSON() }),
      });
    } catch (e) { /* next page load re-subscribes */ }
  })());
});
