/* Service worker Life OS v3.
   Правило простое: оболочка — всегда сначала из сети (иначе после деплоя
   браузер держит старый index.html и ссылается на удалённые файлы сборки),
   хешированные ассеты — из кеша, API — только из сети. */

const VERSION = 'lifeos-v3-3';
const SHELL = ['./offline.html', './manifest.webmanifest', './icon-192.png', './icon-512.png'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(VERSION)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
      .catch(() => undefined),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== VERSION).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

function isApi(url) {
  return url.pathname.startsWith('/api/') || url.pathname.startsWith('/media/') || url.pathname.startsWith('/ws');
}

/** Оболочка: сеть → кеш обновляем, при офлайне отдаём последний index.html. */
async function shellResponse(request) {
  try {
    const response = await fetch(request);
    if (response && response.ok) {
      const cache = await caches.open(VERSION);
      void cache.put('./index.html', response.clone());
    }
    return response;
  } catch (error) {
    const cached = (await caches.match('./index.html')) || (await caches.match('./offline.html'));
    return cached || new Response('Нет связи', { status: 503, headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
  }
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (isApi(url)) {
    event.respondWith(
      fetch(request).catch(() =>
        new Response(JSON.stringify({ error: 'offline' }), {
          status: 503,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    );
    return;
  }

  if (request.mode === 'navigate' || url.pathname === '/' || url.pathname.endsWith('/index.html')) {
    event.respondWith(shellResponse(request));
    return;
  }

  event.respondWith(
    caches.match(request).then((hit) => {
      if (hit) return hit;
      return fetch(request).then((response) => {
        if (response.ok && response.type === 'basic') {
          const copy = response.clone();
          void caches.open(VERSION).then((cache) => cache.put(request, copy));
        }
        return response;
      });
    }),
  );
});
