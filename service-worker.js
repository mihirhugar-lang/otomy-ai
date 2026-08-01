const CACHE = 'crusherops-v4';

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE)
      .then(c => c.addAll(['/static/manifest.webmanifest']))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // API calls, snapshot data, and HTML navigation stay network-only.
  if (url.pathname.startsWith('/api/')) return;
  if (url.pathname.startsWith('/data/')) return;
  if (event.request.mode === 'navigate') return;

  const canCache =
    url.pathname === '/static/manifest.webmanifest' ||
    url.pathname.startsWith('/static/icons/');
  if (!canCache) return;

  // Static assets only (icons, manifest, etc.) — cache first
  event.respondWith(
    caches.match(event.request).then(cached => {
      if (cached) return cached;
      return fetch(event.request).then(resp => {
        if (resp.ok) {
          const clone = resp.clone();
          caches.open(CACHE).then(c => c.put(event.request, clone));
        }
        return resp;
      });
    })
  );
});
