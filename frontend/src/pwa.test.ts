import { describe, expect, it } from 'vitest';
import { buildPwaPrecacheList, injectPwaPrecache, isPwaShellRequest, isVersionedAppShellPath, pwaInstallContextAvailable } from './pwa';

describe('PWA public shell policy', () => {
  it('allows only query-free versioned static assets', () => {
    expect(isVersionedAppShellPath('/assets/index-8Fq7eV2a.js')).toBe(true);
    expect(isPwaShellRequest({ url: '/assets/index-8Fq7eV2a.js' })).toBe(true);
    expect(isPwaShellRequest({ url: '/assets/index-8Fq7eV2a.js?v=1' })).toBe(false);
    expect(isPwaShellRequest({ url: '/assets/index-8Fq7eV2a.js', headers: { Range: 'bytes=0-100' } })).toBe(false);
    expect(isPwaShellRequest({ url: '/assets/index-8Fq7eV2a.js', method: 'POST' })).toBe(false);
  });

  it('rejects API, admin, media-shaped, and encoded API paths', () => {
    expect(isPwaShellRequest({ url: '/api/media/movie/download' })).toBe(false);
    expect(isPwaShellRequest({ url: '/%61pi/media/movie/download' })).toBe(false);
    expect(isPwaShellRequest({ url: '/%2561pi/media/movie/download' })).toBe(false);
    expect(isPwaShellRequest({ url: '/admin/session' })).toBe(false);
    expect(isPwaShellRequest({ url: '/video/movie-8Fq7eV2a.mp4' })).toBe(false);
    expect(isPwaShellRequest({ url: 'https://cdn.example/assets/index-8Fq7eV2a.js' })).toBe(false);
  });

  it('builds an explicit allowlist from only Vite versioned assets', () => {
    const list = buildPwaPrecacheList({
      entry: { file: 'assets/index-8Fq7eV2a.js', css: ['assets/index-AbCdEf12.css'], assets: ['assets/logo-F3dE2cB1.svg', 'assets/source-map-12345678.map'] },
      data: { file: 'api/session-8Fq7eV2a.json' },
    });
    expect(list).toEqual(['/assets/index-8Fq7eV2a.js', '/assets/index-AbCdEf12.css', '/assets/logo-F3dE2cB1.svg']);
  });

  it('injects only approved paths into the worker template', () => {
    const template = 'const c = "__DRIVETHEATER_CACHE_VERSION__"; const p = /* __DRIVETHEATER_PRECACHE__ */ [] /* __DRIVETHEATER_PRECACHE__ */;';
    const worker = injectPwaPrecache(template, ['/api/nope-8Fq7eV2a.js', '/assets/app-8Fq7eV2a.js'], '2026.11.16');
    expect(worker).toContain('"/assets/app-8Fq7eV2a.js"');
    expect(worker).not.toContain('/api/nope');
    expect(worker).toContain('2026.11.16');
  });

  it('limits installation registration to HTTPS or local development', () => {
    expect(pwaInstallContextAvailable({ protocol: 'https:', hostname: 'theater.example' } as Location)).toBe(true);
    expect(pwaInstallContextAvailable({ protocol: 'http:', hostname: 'localhost' } as Location)).toBe(true);
    expect(pwaInstallContextAvailable({ protocol: 'http:', hostname: 'theater.example' } as Location)).toBe(false);
  });
});
