import { defineConfig } from 'vitest/config';
import type { Plugin, ResolvedConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { createHash } from 'node:crypto';
import { buildPwaPrecacheList, injectPwaPrecache, type ViteManifest } from './src/pwa';

function publicShell(): Plugin {
  let config: ResolvedConfig;
  return {
    name: 'drivetheater-public-shell',
    apply: 'build',
    configResolved(value) { config = value; },
    async writeBundle(_options, bundle) {
      const manifest: ViteManifest = {};
      for (const file of Object.keys(bundle)) manifest[file] = { file };
      const paths = buildPwaPrecacheList(manifest);
      if (!paths.length) throw new Error('No hashed public assets were produced for the app shell.');
      const version = createHash('sha256').update(paths.join('\n')).digest('hex').slice(0, 16);
      const template = await readFile(resolve(config.root, 'public/sw.js'), 'utf8');
      await writeFile(resolve(config.root, config.build.outDir, 'sw.js'), injectPwaPrecache(template, paths, version));
    }
  };
}

export default defineConfig({
  plugins: [react(), publicShell()],
  build: { manifest: true },
  server: { port: 5173 },
  test: { environment: 'node', include: ['src/**/*.test.ts'] }
});
