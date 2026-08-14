/* Кабинет клиента — Service Worker.

   Задача одна: чтобы страница открылась в подвале кофейни, где связи нет.
   Всё остальное (корзина, отложенная отправка заказа) живёт в самой странице —
   SW не пытается быть умнее и не трогает POST-запросы.

   Стратегия:
   - навигация по /shop/… : network-first, при офлайне — последняя удачная копия
     этой же страницы из кэша;
   - статика: stale-while-revalidate;
   - POST и всё остальное: только сеть. Заказ, «отправленный» из кэша, был бы
     хуже честной ошибки.
*/
const VERSION = 'nerpa-shop-v2';
const CACHE = `${VERSION}-pages`;

self.addEventListener('install', () => self.skipWaiting());

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(
        names.filter((n) => (n.startsWith('nerpa-shop-') || n.startsWith('tms-shop-')) && n !== CACHE)
             .map((n) => caches.delete(n))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Страница кабинета: сначала сеть, кэш — как подстраховка
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE).then((c) => c.put(request, copy)).catch(() => {});
          return response;
        })
        .catch(() => caches.match(request).then((cached) => cached || caches.match(url.pathname)))
    );
    return;
  }

  // Статика — отдаём из кэша сразу, в фоне обновляем
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(request).then((cached) => {
        const network = fetch(request)
          .then((response) => {
            const copy = response.clone();
            caches.open(CACHE).then((c) => c.put(request, copy)).catch(() => {});
            return response;
          })
          .catch(() => cached);
        return cached || network;
      })
    );
  }
});
