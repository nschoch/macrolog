/* MacroLog service worker. API calls are never cached (always live). The app
   shell is served network-first so a deploy is picked up immediately; the cache
   is only an offline fallback. Asset URLs are build-versioned by the server, so
   stale code can't be reused even from this cache. */
const CACHE = 'macrolog-shell-v13';
const SHELL = ['/', '/manifest.webmanifest', '/icon.svg'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.pathname.startsWith('/api/')) return; // never cache data
  // Network-first for everything else; fall back to cache only when offline.
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        // Refresh the offline snapshot opportunistically.
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
        }
        return res;
      })
      .catch(() => caches.match(e.request).then((hit) => hit || caches.match('/')))
  );
});
