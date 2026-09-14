self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open('bongatrack-cache-v1').then((cache) => {
      return cache.addAll([
        '/',
        '/resident-portal',
        '/login'
      ]);
    })
  );
});

self.addEventListener('fetch', (e) => {
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});