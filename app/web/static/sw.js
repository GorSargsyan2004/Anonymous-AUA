/* AUA Hub Service Worker (PWA Support & Offline Shell Cache) */
const CACHE_NAME = "aua-hub-shell-v30";
const SHELL_ASSETS = [
  "/",
  "/static/styles.css?v=20261108_2",
  "/static/app.js?v=20261111_1",
  "/static/bot-logo.svg",
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
  // Never cache dynamic API or WebSocket endpoints
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws/")) {
    return;
  }

  // Network-first with cache fallback for shell & static assets
  event.respondWith(
    fetch(req)
      .then((networkResp) => {
        if (networkResp && networkResp.status === 200 && url.origin === self.location.origin) {
          const clone = networkResp.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(req, clone)).catch(() => { });
        }
        return networkResp;
      })
      .catch(() => caches.match(req).then((cached) => cached || caches.match("/")))
  );
});

