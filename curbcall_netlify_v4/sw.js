// CurbCall Pro service worker — keeps the app usable in a truck with no bars.
//
// Two caches, on purpose:
//   SHELL_CACHE  our own HTML/icons. Stale-while-revalidate: instant load,
//                refreshed in the background whenever there IS a connection.
//   ASSET_CACHE  the third-party libraries the app cannot boot without
//                (Leaflet, supabase-js) plus webfonts. Cache-first, because
//                these are version-pinned URLs whose contents never change.
//
// Why ASSET_CACHE matters: app.html loads supabase-js from a CDN and calls
// supabase.createClient() at the top level. With no network that script never
// arrives, the call throws, and the ENTIRE app script dies — a signed-in user
// gets a dead sign-in screen instead of the bids already on their phone.
// Caching the library is what makes offline mode reachable at all.
//
// API calls (Render backend, Supabase REST) and map tiles are still never
// cached — bid data, auth, and the map must never be served stale.
const SHELL_CACHE = "curbcall-shell-v3";
const ASSET_CACHE = "curbcall-assets-v3";
const KEEP = [SHELL_CACHE, ASSET_CACHE];

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

// Must stay in sync with the <script>/<link> tags in app.html. Version-pinned
// so a cached copy is always the right copy.
const VENDOR_FILES = [
  "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
  "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
  "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"
];

// Hosts whose responses are safe to keep forever once fetched. Fonts are
// included so the app doesn't reflow into fallback type when offline.
const ASSET_HOSTS = [
  "unpkg.com",
  "cdn.jsdelivr.net",
  "fonts.googleapis.com",
  "fonts.gstatic.com"
];

function isAssetRequest(url) {
  return ASSET_HOSTS.includes(url.hostname);
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    Promise.all([
      caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_FILES)).catch(() => {}),
      // Cached one at a time rather than via addAll(): addAll() is atomic, so a
      // single CDN hiccup would throw away every other vendor file too.
      caches.open(ASSET_CACHE).then((cache) =>
        Promise.allSettled(
          VENDOR_FILES.map((u) => cache.add(new Request(u, { mode: "cors" })))
        )
      ).catch(() => {})
    ])
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => !KEEP.includes(k)).map((k) => caches.delete(k)))
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
  if (event.request.method !== "GET") return;

  let url;
  try {
    url = new URL(event.request.url);
  } catch (e) {
    return;
  }

  // ── Third-party libraries + fonts: cache-first ──
  // These URLs are version-pinned, so a hit is always current and skipping the
  // network makes a cold start on a bad connection dramatically faster.
  if (isAssetRequest(url)) {
    event.respondWith(
      caches.match(event.request).then((cached) => {
        if (cached) return cached;
        return fetch(event.request)
          .then((res) => {
            // Opaque (no-cors) responses are cached too: they replay fine for
            // <script>/<link>/font loads, which is all these hosts serve here.
            if (res && (res.ok || res.type === "opaque")) {
              const copy = res.clone();
              caches.open(ASSET_CACHE).then((cache) => cache.put(event.request, copy));
            }
            return res;
          })
          .catch(() => cached);
      })
    );
    return;
  }

  // ── Our own shell: stale-while-revalidate ──
  const isShellFile = url.origin === self.location.origin &&
    SHELL_FILES.some((f) => url.pathname.endsWith("/" + f) || url.pathname === "/" + f);
  if (!isShellFile) return; // API calls and map tiles hit the network normally

  event.respondWith(
    caches.match(event.request).then((cached) => {
      const network = fetch(event.request)
        .then((res) => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(SHELL_CACHE).then((cache) => cache.put(event.request, copy));
          }
          return res;
        })
        .catch(() => cached);
      return cached || network;
    })
  );
});
