/* MacroLog service worker. API calls are never cached (always live). The app
   shell is served network-first so a deploy is picked up immediately; the cache
   is only an offline fallback. Asset URLs are build-versioned by the server, so
   stale code can't be reused even from this cache. */
// Bump this whenever the shell/API contract changes. A stale worker with an
// older cache is the most common reason a hard refresh still shows old UI.
const CACHE = 'macrolog-shell-v30';
const SHELL = ['/', '/manifest.webmanifest', '/icon.svg'];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) =>
      // Cache each entry on its own. `addAll` rejects the whole install if any
      // single request fails, and `/` legitimately redirects to the login page
      // whenever the app is signed out — which is exactly the state a
      // home-screen web app starts in (iOS gives it its own cookie jar). One
      // failed entry used to abort the install, so the worker never activated
      // and every launch retried the same broken install.
      Promise.all(SHELL.map((url) => c.add(url).catch(() => {})))
    ).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// Only the static shell is ever cached. Auth pages (e.g. the "Signed out"
// page, whose Set-Cookie a cached copy would never replay), Health endpoints,
// API data, and arbitrary SPA-fallback paths are always network-only.
const CACHEABLE = /^\/(|index\.html|app\.js|app\.css|sw\.js|icon\.svg|manifest\.webmanifest)$/;

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (!CACHEABLE.test(url.pathname)) return;            // network only
  // Network-first; fall back to cache only when offline.
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        if (res.ok && res.type === 'basic') {
          const copy = res.clone();
          // Replace older ?v= build copies of the same file instead of piling up.
          caches.open(CACHE)
            .then((c) => c.delete(e.request, { ignoreSearch: true }).then(() => c.put(e.request, copy)))
            .catch(() => {});
        }
        return res;
      })
      .catch(() => caches.match(e.request).then((hit) =>
        // The '/' shell is a valid fallback for a page load, never for a
        // script or stylesheet request (that would hand HTML to <script>).
        hit || (e.request.mode === 'navigate' ? caches.match('/') : Response.error())))
  );
});
