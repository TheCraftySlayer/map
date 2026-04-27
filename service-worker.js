// Service worker for the Bernalillo equity map.
//
// Goals:
//   1. Make the password-gated app usable offline once it's been loaded
//      at least once on a given device. The loader, manifest, and the
//      three .enc files are cached.
//   2. Network-first for the manifest (so a fresh deploy is picked up
//      quickly when the device IS online), cache-first for the loader
//      and ciphertext (which only change on full redeploys).
//   3. Survive a deploy: every change to this file's CACHE_VERSION
//      triggers a clean install + delete-old-caches dance.
//
// What this DOESN'T do:
//   - It never sees plaintext. The loader decrypts in-page after fetch.
//     If the device is offline AND the cache holds stale ciphertext,
//     the user still has to type their password.
//   - No background sync, no push. The field-tools "sync queue" lives
//     in the body's JS (see FIELD_V1) — it just queues to localStorage
//     and replays on the next online interaction.

const CACHE_VERSION = 'bernco-map-v1';
const PRECACHE_URLS = [
  './',
  './index.html',
  './manifest.webmanifest',
  './data/enc_manifest.json',
  './data/core.json.enc',
  './data/layers.json.enc',
  './index_body.html.enc',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) => {
      // Best-effort precache — a single 404 (e.g. a tier file the deploy
      // doesn't ship) shouldn't abort the install.
      return Promise.all(
        PRECACHE_URLS.map((u) =>
          cache.add(u).catch((err) => console.warn('precache miss:', u, err))
        )
      );
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET') return;
  if (url.origin !== self.location.origin) return;

  // Network-first for the small + frequently-deployed manifest.
  if (url.pathname.endsWith('/data/enc_manifest.json')) {
    event.respondWith(
      fetch(event.request).then((resp) => {
        const copy = resp.clone();
        caches.open(CACHE_VERSION).then((c) => c.put(event.request, copy));
        return resp;
      }).catch(() => caches.match(event.request))
    );
    return;
  }

  // Cache-first for everything else in the precache list.
  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;
      return fetch(event.request).then((resp) => {
        if (resp.ok && PRECACHE_URLS.some((p) => url.pathname.endsWith(p.replace(/^\.\//, '')))) {
          const copy = resp.clone();
          caches.open(CACHE_VERSION).then((c) => c.put(event.request, copy));
        }
        return resp;
      });
    })
  );
});
