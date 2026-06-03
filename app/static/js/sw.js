/* TMS Склад — Service Worker
   Стратегия:
   - навигация (HTML): network-first, при оффлайне — заглушка;
   - статика (/static/, шрифты, CDN): stale-while-revalidate;
   - POST и прочее: всегда сеть (не кэшируем мутации).
   Кэш намеренно лёгкий — это инструмент локальной сети, данные всегда свежие. */

const VERSION    = 'tms-wh-v1';
const STATIC_CACHE = `${VERSION}-static`;

// Базовая статика для мгновенной загрузки оболочки
const PRECACHE = [
  '/static/css/warehouse_mobile.css',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
];

const OFFLINE_HTML = `<!doctype html><html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Нет связи</title>
<style>body{font-family:system-ui,sans-serif;background:#0F1A2E;color:#fff;height:100vh;margin:0;
display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:24px}
.box{font-size:3rem;margin-bottom:12px}h1{font-size:1.2rem;margin:0 0 8px}p{color:#94a3b8;margin:0 0 20px}
button{background:#2563EB;color:#fff;border:none;border-radius:12px;padding:14px 28px;font-size:1rem;font-weight:700}</style>
</head><body>
<div class="box">📦</div>
<h1>Нет связи с сервером</h1>
<p>Проверьте Wi-Fi и подключение к серверу TMS</p>
<button onclick="location.reload()">Повторить</button>
</body></html>`;

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

  // Навигация по страницам — сеть в приоритете
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).catch(() =>
        new Response(OFFLINE_HTML, { headers: { 'Content-Type': 'text/html; charset=utf-8' } })
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
