/* TMS Склад — Service Worker
   Стратегия:
   - навигация (HTML): network-first, при оффлайне — /static/offline.html из кеша;
   - статика (/static/, шрифты, CDN): stale-while-revalidate;
   - POST и прочее: всегда сеть (не кэшируем мутации).
   Кэш намеренно лёгкий — это инструмент локальной сети, данные всегда свежие. */

const VERSION      = 'tms-wh-v6';
const STATIC_CACHE = `${VERSION}-static`;
const OFFLINE_URL  = '/static/offline.html';

// Базовая статика + offline-страница — кешируем при установке
const PRECACHE = [
  OFFLINE_URL,
  '/static/css/warehouse_mobile.css',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE)
      .then((cache) => cache.addAll(PRECACHE).catch(() => {}))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => !k.startsWith(VERSION)).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

function isStaticAsset(url) {
  return (
    url.pathname.startsWith('/static/') ||
    url.hostname.includes('cdn.jsdelivr.net') ||
    url.hostname.includes('fonts.googleapis.com') ||
    url.hostname.includes('fonts.gstatic.com')
  );
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return; // мутации — мимо кэша

  const url = new URL(req.url);

  // Навигация по страницам — сеть в приоритете, при сбое — offline.html из кеша
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).catch(() =>
        caches.match(OFFLINE_URL).then((cached) =>
          cached || new Response(
            '<h1>Нет связи</h1><button onclick="location.reload()">Повторить</button>',
            { headers: { 'Content-Type': 'text/html; charset=utf-8' } }
          )
        )
      )
    );
    return;
  }

  // Статика — stale-while-revalidate
  if (isStaticAsset(url)) {
    event.respondWith(
      caches.open(STATIC_CACHE).then((cache) =>
        cache.match(req).then((cached) => {
          const network = fetch(req)
            .then((res) => {
              if (res && res.status === 200) cache.put(req, res.clone());
              return res;
            })
            .catch(() => cached);
          return cached || network;
        })
      )
    );
  }
});
