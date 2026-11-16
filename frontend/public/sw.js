/*
 * Build template: replace the two placeholders with injectPwaPrecache() during
 * the production Vite build. Left untouched, this worker safely precaches
 * nothing rather than guessing at runtime URLs.
 */
const CACHE_PREFIX = 'drivetheater-shell-';
const CACHE_NAME = `${CACHE_PREFIX}__DRIVETHEATER_CACHE_VERSION__`;
const PRECACHE_URLS = Object.freeze(/* __DRIVETHEATER_PRECACHE__ */ [] /* __DRIVETHEATER_PRECACHE__ */);
const PRECACHE_SET = new Set(PRECACHE_URLS);

function isSafeResponse(response) {
  if (!response || !response.ok || response.type !== 'basic') return false;
  if (response.headers.has('Set-Cookie')) return false;
  const cacheControl = response.headers.get('Cache-Control') || '';
  return !/\b(?:private|no-store)\b/i.test(cacheControl);
}

function isExplicitShellRequest(request) {
  if (request.method !== 'GET' || request.headers.has('Range') || request.mode === 'navigate') return false;
  const url = new URL(request.url);
  return url.origin === self.location.origin && !url.search && PRECACHE_SET.has(url.pathname);
}

async function deleteShellCaches() {
  const cacheNames = await caches.keys();
  await Promise.all(cacheNames.filter((name) => name.startsWith(CACHE_PREFIX)).map((name) => caches.delete(name)));
}

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    for (const path of PRECACHE_URLS) {
      const response = await fetch(path, { cache: 'reload', credentials: 'omit' });
      if (!isSafeResponse(response)) throw new Error(`Refusing to cache non-public shell response: ${path}`);
      await cache.put(path, response);
    }
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const cacheNames = await caches.keys();
    await Promise.all(cacheNames
      .filter((name) => name.startsWith(CACHE_PREFIX) && name !== CACHE_NAME)
      .map((name) => caches.delete(name)));
  })());
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (!isExplicitShellRequest(request)) return;
  event.respondWith((async () => {
    const cache = await caches.open(CACHE_NAME);
    const cached = await cache.match(request, { ignoreSearch: false });
    if (cached) return cached;
    const response = await fetch(request);
    if (isSafeResponse(response)) {
      const cache = await caches.open(CACHE_NAME);
      await cache.put(request, response.clone());
    }
    return response;
  })());
});

self.addEventListener('message', (event) => {
  if (event.data?.type === 'SKIP_WAITING') self.skipWaiting();
  if (event.data?.type === 'CLEAR_SHELL_CACHE') event.waitUntil(deleteShellCaches());
});
