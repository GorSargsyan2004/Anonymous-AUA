/* AUA Hub Service Worker (PWA Support & Offline Shell Cache) */
const CACHE_NAME = "aua-hub-shell-v57";
const SHELL_ASSETS = [
  "/",
  "/static/styles.css?v=20261127_14",
  "/static/app.js?v=20261127_14",
  "/static/partners/findaua-logo.png",
  "/static/partners/jenzareviews-logo.png",
  "/static/partners/auautils-logo.png",
  "/static/favicon-32x32.png?v=20261113_1",
  "/static/favicon-192x192.png?v=20261113_1",
  "/static/apple-touch-icon.png?v=20261113_1",
  "/static/bot-logo.svg",
  "/favicon.ico",
  "/manifest.json",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(SHELL_ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key !== CACHE_NAME)
            .map((key) => caches.delete(key))
        )
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  // Never intercept cross-origin requests (e.g. Google Fonts, Cloudflare Turnstile/Insights)
  if (url.origin !== self.location.origin) {
    return;
  }

  // Never cache dynamic API or WebSocket endpoints
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws/")) {
    return;
  }

  // Network-first with cache fallback for static assets & HTML shell
  event.respondWith(
    fetch(req)
      .then((networkResp) => {
        if (networkResp && networkResp.status === 200) {
          const clone = networkResp.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(req, clone)).catch(() => { });
        }
        return networkResp;
      })
      .catch(() =>
        caches.match(req).then((cached) => {
          if (cached) return cached;
          if (req.mode === "navigate") {
            return caches.match("/");
          }
          return new Response("Offline", { status: 503, statusText: "Offline" });
        })
      )
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const payload = event.notification.data || {};
  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then((clientList) => {
        for (const client of clientList) {
          if (client.url && client.url.startsWith(self.location.origin) && "focus" in client) {
            client.postMessage({ type: "AUA_HUB_OPEN_NOTIFICATION", data: payload });
            return client.focus();
          }
        }
        if (self.clients.openWindow) {
          return self.clients.openWindow("/");
        }
        return undefined;
      })
      .catch(() => undefined)
  );
});

