// Minimal service worker. Right now it only handles notification clicks
// (focus an existing tab instead of opening a duplicate).
//
// The 'push' listener below is the hook for real Web Push later: generate
// VAPID keys, store subscriptions server-side, and send with pywebpush.
// Until then notifications are raised by the page while a tab is open.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      for (const client of list) {
        if ("focus" in client) return client.focus();
      }
      return self.clients.openWindow("/");
    })
  );
});

self.addEventListener("push", (event) => {
  if (!event.data) return;
  let payload = {};
  try { payload = event.data.json(); } catch (e) { payload = { title: event.data.text() }; }
  event.waitUntil(
    self.registration.showNotification(payload.title || "Ops Deck", {
      body: payload.body || "",
      tag: payload.tag,
    })
  );
});
