/* sw.js — FoodRescue service worker: offline app shell.
 *
 * Strategy: cache-first for the static shell (HTML/CSS/JS/icons on this
 * origin), network passthrough for everything else. The API lives on another
 * origin (localhost:5000), so data requests never touch this cache — the app
 * shell opens instantly and offline, data stays live.
 *
 * Bump CACHE_VERSION whenever shipped frontend files change.
 */
const CACHE_VERSION = "fr-shell-v1";

const SHELL = [
  "auth.html", "donor.html", "ngo.html", "volunteer.html", "admin.html",
  "theme.css", "auth.css", "donor.css", "ngo.css", "volunteer.css", "admin.css",
  "theme.js", "fr-ui.js", "auth.js", "donor.js", "ngo.js", "volunteer.js", "admin.js",
  "manifest.webmanifest", "icon.svg", "icon-maskable.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((key) => key !== CACHE_VERSION).map((key) => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  // Same-origin GETs only — API calls and third-party fonts pass straight through.
  if (event.request.method !== "GET" || url.origin !== self.location.origin) return;

  event.respondWith(
    caches.match(event.request, { ignoreSearch: true }).then(
      (cached) =>
        cached ||
        fetch(event.request).then((response) => {
          // Keep the shell cache warm with whatever static file just loaded.
          if (response.ok) {
            const copy = response.clone();
            caches.open(CACHE_VERSION).then((cache) => cache.put(event.request, copy));
          }
          return response;
        })
    )
  );
});
