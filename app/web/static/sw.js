/* AUA Hub Service Worker (PWA Support & Offline Shell Cache) */
const CACHE_NAME = "aua-hub-shell-v41";
const SHELL_ASSETS = [
  "/",
  "/static/styles.css?v=20261115_1",
  "/static/app.js?v=20261114_18",
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
  // Never cache dynamic API or WebSocket endpoints
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws/")) {
    return;
  }

  // Cache-first for immutable ?v= versioned static bundles on mobile/repeat loads
  if (
    url.origin === self.location.origin &&
    url.pathname.startsWith("/static/") &&
    url.searchParams.has("v")
  ) {
    event.respondWith(
      caches.match(req).then((cached) => {
        if (cached) return cached;
        return fetch(req).then((networkResp) => {
          if (networkResp && networkResp.status === 200) {
            const clone = networkResp.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(req, clone)).catch(() => { });
          }
          return networkResp;
        });
      })
    );
    return;
  }

  // Network-first with cache fallback for HTML shell & unversioned static assets
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

