export const PWA_CACHE_PREFIX = 'drivetheater-shell-';
export const SERVICE_WORKER_PATH = '/sw.js';

const VERSIONED_APP_SHELL = /^\/assets\/[a-z0-9][a-z0-9._-]*[-.][a-z0-9_-]{8,}\.(?:js|css|woff2?|ttf|otf|svg|png|webp|avif|ico)$/i;
const PRECACHE_MARKER = '/* __DRIVETHEATER_PRECACHE__ */ [] /* __DRIVETHEATER_PRECACHE__ */';
const CACHE_VERSION_MARKER = '__DRIVETHEATER_CACHE_VERSION__';

type HeaderSource = Headers | Record<string, string | undefined> | undefined;

export type ShellRequest = {
  method?: string;
  url: string;
  headers?: HeaderSource;
};

export type ViteManifestEntry = {
  file?: string;
  css?: string[];
  assets?: string[];
};

export type ViteManifest = Record<string, ViteManifestEntry>;

function headerValue(headers: HeaderSource, name: string): string | undefined {
  if (!headers) return undefined;
  if (headers instanceof Headers) return headers.get(name) ?? undefined;
  const expected = name.toLowerCase();
  const key = Object.keys(headers).find((candidate) => candidate.toLowerCase() === expected);
  return key ? headers[key] : undefined;
}

function fullyDecodePath(pathname: string): string | null {
  let current = pathname;
  for (let attempt = 0; attempt < 4; attempt += 1) {
    try {
      const decoded = decodeURIComponent(current);
      if (decoded === current) return decoded;
      current = decoded;
    } catch {
      return null;
    }
  }
  return current.includes('%') ? null : current;
}

/** True only for hashed build output that is safe to list in a public shell cache. */
export function isVersionedAppShellPath(pathname: string): boolean {
  const decoded = fullyDecodePath(pathname);
  return decoded !== null && VERSIONED_APP_SHELL.test(decoded);
}

/**
 * A service worker may handle only these public, versioned build assets.
 * Queries and Range requests are deliberately excluded to avoid cache poisoning
 * and video/media responses. API, admin, and media routes never match this shape.
 */
export function isPwaShellRequest(request: ShellRequest, origin = 'https://drivetheater.invalid'): boolean {
  if ((request.method ?? 'GET').toUpperCase() !== 'GET') return false;
  if (headerValue(request.headers, 'range')) return false;

  let url: URL;
  try {
    url = new URL(request.url, origin);
  } catch {
    return false;
  }
  if (url.origin !== origin || url.search) return false;
  const decoded = fullyDecodePath(url.pathname);
  if (decoded === null || /^\/(?:api|admin)(?:\/|$)/i.test(decoded)) return false;
  return isVersionedAppShellPath(decoded);
}

function toPublicPath(value: string): string | null {
  if (!value || value.startsWith('/') || value.includes('?') || value.includes('#')) return null;
  const candidate = `/${value.replaceAll('\\', '/')}`;
  return isVersionedAppShellPath(candidate) ? candidate : null;
}

/**
 * Extracts a minimal public app-shell manifest from Vite's build manifest.
 * It intentionally ignores HTML, source maps, data files, public unversioned
 * files, API URLs, and every unknown extension.
 */
export function buildPwaPrecacheList(manifest: ViteManifest): string[] {
  const paths = new Set<string>();
  for (const entry of Object.values(manifest)) {
    for (const asset of [entry.file, ...(entry.css ?? []), ...(entry.assets ?? [])]) {
      if (typeof asset !== 'string') continue;
      const publicPath = toPublicPath(asset);
      if (publicPath) paths.add(publicPath);
    }
  }
  return [...paths].sort();
}

/** Replaces the inert placeholders in public/sw.js during Vite's production build. */
export function injectPwaPrecache(serviceWorkerTemplate: string, precachePaths: readonly string[], cacheVersion: string): string {
  if (!/^[a-z0-9][a-z0-9._-]{0,63}$/i.test(cacheVersion)) throw new Error('PWA cache version must be a short safe identifier.');
  const safePaths = [...new Set(precachePaths)].filter((path) => isPwaShellRequest({ url: path })).sort();
  if (!serviceWorkerTemplate.includes(PRECACHE_MARKER) || !serviceWorkerTemplate.includes(CACHE_VERSION_MARKER)) {
    throw new Error('Service worker template is missing its PWA build placeholders.');
  }
  return serviceWorkerTemplate
    .replace(PRECACHE_MARKER, `/* __DRIVETHEATER_PRECACHE__ */ ${JSON.stringify(safePaths)} /* __DRIVETHEATER_PRECACHE__ */`)
    .replaceAll(CACHE_VERSION_MARKER, cacheVersion);
}

export function pwaInstallContextAvailable(location: Pick<Location, 'protocol' | 'hostname'> = window.location): boolean {
  return location.protocol === 'https:' || location.hostname === 'localhost' || location.hostname === '127.0.0.1' || location.hostname === '[::1]';
}

export type PwaRegistrationHandlers = {
  onUpdateReady?: (registration: ServiceWorkerRegistration) => void;
  onOfflineReady?: (registration: ServiceWorkerRegistration) => void;
};

/** Registers the public-shell worker only in an HTTPS or localhost context. */
export async function registerDriveTheaterPwa(handlers: PwaRegistrationHandlers = {}): Promise<ServiceWorkerRegistration | null> {
  if (typeof window === 'undefined' || !('serviceWorker' in navigator) || !pwaInstallContextAvailable(window.location)) return null;
  const registration = await navigator.serviceWorker.register(SERVICE_WORKER_PATH, { scope: '/' });
  const watch = (worker: ServiceWorker | null) => {
    if (!worker) return;
    worker.addEventListener('statechange', () => {
      if (worker.state !== 'installed') return;
      if (navigator.serviceWorker.controller) handlers.onUpdateReady?.(registration);
      else handlers.onOfflineReady?.(registration);
    });
  };
  watch(registration.installing);
  registration.addEventListener('updatefound', () => watch(registration.installing));
  if (registration.waiting && navigator.serviceWorker.controller) handlers.onUpdateReady?.(registration);
  return registration;
}

/** Lets a visible update prompt activate a waiting worker; callers should then reload. */
export function activatePwaUpdate(registration: ServiceWorkerRegistration): void {
  registration.waiting?.postMessage({ type: 'SKIP_WAITING' });
}

/**
 * Removes only the public shell cache. Call from the application's logout path
 * if it needs a belt-and-suspenders cleanup; the worker never caches account data.
 */
export function clearPwaShellCache(): void {
  if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return;
  const clear = (worker: ServiceWorker | null | undefined) => worker?.postMessage({ type: 'CLEAR_SHELL_CACHE' });
  clear(navigator.serviceWorker.controller);
  void navigator.serviceWorker.ready.then((registration) => clear(registration.active));
}
