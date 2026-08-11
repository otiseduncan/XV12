const CACHE = "xoduz-shell-v3";
const SHELL = [
  "/static/styles.css?v=4.1.1",
  "/static/app.js?v=4.1.1",
  "/static/manifest.webmanifest?v=xoduz-xv12",
  "/static/icons/xoduz-192.png",
  "/static/icons/xoduz-512.png",
  "/assets/avatar/xoduz-512.png",
  "/assets/avatar/xoduz-icon.svg"
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)))).then(() => self.clients.claim()));
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/") || url.pathname.startsWith("/onboard/")) return;
  if (url.pathname === "/") {
    event.respondWith(fetch(event.request, { cache: "no-store" }));
    return;
  }
  if (url.pathname.startsWith("/static/") || url.pathname.startsWith("/assets/")) {
    event.respondWith(caches.match(event.request).then((cached) => cached || fetch(event.request)));
  }
});
