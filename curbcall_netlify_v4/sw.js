// CurbCall Pro service worker — caches just the app shell so "Add to Home
// Screen" behaves like a real installed app and a flaky connection doesn't
// leave you on a blank white screen. It deliberately does NOT cache API
// calls (Render backend, Supabase, map tiles) — those always go to the
// network so bid data, auth, and the map are never stale.
const CACHE_NAME = "curbcall-shell-v2";
const SHELL_FILES = [
  "app.html",
  "index.html",
  "admin.html",
  "terms.html",
  "privacy.html",
  "manifest.webmanifest",
  "icon-192.png",
  "icon-512.png"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_FILES)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Clicking a bid-alert notification should bring an already-open tab to the
// front, or open one if none exists — otherwise the notification is a dead
// end on some platforms.
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if (client.url.includes("app.html") && "focus" in client) return client.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow("app.html");
    })
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  const isShellFile = url.origin === self.location.origin &&
    SHELL_FILES.some((f) => url.pathname.endsWith("/" + f) || url.pathname === "/" + f);

  if (!isShellFile) return; // let everything else (API/CDN calls) hit the network normally

  event.respondWith(
    caches.match(event.request).then((cached) => {
      const network = fetch(event.request)
        .then((res) => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
          }
          return res;
        })
        .catch(() => cached);
      return cached || network;
    })
  );
});
