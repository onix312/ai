/* PrintFlow — служебный воркер: панель открывается мгновенно и не пропадает
   при мигании сети.

   Правила намеренно простые и безопасные для системы учёта:
     • данные (/api/*) НИКОГДА не кэшируются — цифры всегда живые;
     • статика берётся из сети, а копия в кэше — только запасной аэродром,
       поэтому обновление системы не оставляет старый интерфейс;
     • при обрыве связи открывается последняя рабочая версия страницы,
       а интерфейс сам показывает «Коннектор недоступен».

   Воркер регистрируется только в защищённом контексте (localhost или HTTPS):
   по обычному http с телефона браузер его не разрешает — это ограничение
   браузеров, а не PrintFlow. Панель на телефоне работает и без него. */

const CACHE = 'printflow-shell-v24';
/* Оболочка панели: всё, без чего интерфейс не соберётся офлайн.
   Список сверяется с index.html тестом test_pwa_shell — если в разметку
   добавили скрипт или стиль, проверка упадёт и напомнит внести его сюда.
   Раньше список вёлся вручную и разошёлся: bridge.js, ops10.js,
   workshop.js и gcode-viewer.js не кэшировались, и офлайн-панель падала
   на первом же незагруженном модуле. */
const SHELL = [
  '/',
  '/index.html',
  '/m.html',
  '/labels.html',
  '/price-tags.html',
  '/spool.html',
  '/assets/tokens.css',
  '/assets/theme.css',
  '/assets/app.css',
  '/assets/more.css',
  '/assets/core.js',
  '/assets/icons.js',
  '/assets/app.js',
  '/assets/queue.js',
  '/assets/stl-viewer.js',
  '/assets/stl-worker.js',
  '/assets/gcode-viewer.js',
  '/assets/ops.js',
  '/assets/ops10.js',
  '/assets/money.js',
  '/assets/finance.js',
  '/assets/products.js',
  '/assets/printer.js',
  '/assets/shelf.js',
  '/assets/qr.js',
  '/assets/bridge.js',
  '/assets/workshop.js',
  '/assets/marketing.js',
  '/assets/clientbot.js',
  '/assets/brand/nozza-logo.svg',
  '/assets/brand/nozza-mark.svg',
  '/assets/brand/favicon.svg',
  '/manifest.webmanifest',
];

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    // Один недоступный файл не должен рушить установку целиком.
    await Promise.all(SHELL.map((url) => cache.add(url).catch(() => {})));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.filter((name) => name !== CACHE).map((name) => caches.delete(name)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Живые данные, потоки и камера мимо кэша — иначе получим вчерашние цифры.
  if (url.pathname.startsWith('/api/')) return;

  event.respondWith((async () => {
    try {
      const response = await fetch(request);
      if (response && response.ok && response.type === 'basic') {
        const cache = await caches.open(CACHE);
        cache.put(request, response.clone()).catch(() => {});
      }
      return response;
    } catch (error) {
      const cached = await caches.match(request);
      if (cached) return cached;
      if (request.mode === 'navigate') {
        const shell = await caches.match('/index.html');
        if (shell) return shell;
      }
      throw error;
    }
  })());
});
