import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / 'frontend' / 'public'


class PwaPolicySmokeTests(unittest.TestCase):
    def test_manifest_uses_local_icons_and_standalone_scope(self):
        manifest = json.loads((PUBLIC / 'manifest.webmanifest').read_text(encoding='utf-8'))
        self.assertEqual(manifest['start_url'], '/')
        self.assertEqual(manifest['scope'], '/')
        self.assertEqual(manifest['display'], 'standalone')
        self.assertTrue(manifest['icons'])
        for icon in manifest['icons']:
            self.assertTrue(icon['src'].startswith('/icons/'))
            self.assertNotIn('://', icon['src'])
            self.assertTrue((PUBLIC / icon['src'].lstrip('/')).is_file())

    def test_worker_defaults_to_an_empty_explicit_precache(self):
        worker = (PUBLIC / 'sw.js').read_text(encoding='utf-8')
        self.assertIn("const PRECACHE_URLS = Object.freeze(/* __DRIVETHEATER_PRECACHE__ */ []", worker)
        self.assertIn("request.mode === 'navigate'", worker)
        self.assertIn("request.headers.has('Range')", worker)
        self.assertIn("response.headers.has('Set-Cookie')", worker)
        self.assertIn("private|no-store", worker)
        self.assertNotIn("respondWith(caches.match('/'))", worker)

    def test_worker_never_uses_navigation_or_runtime_fallbacks(self):
        worker = (PUBLIC / 'sw.js').read_text(encoding='utf-8')
        self.assertIn('PRECACHE_SET.has(url.pathname)', worker)
        self.assertNotIn('navigateFallback', worker)
        self.assertNotIn('CacheFirst', worker)
        self.assertNotIn('NetworkFirst', worker)


if __name__ == '__main__':
    unittest.main()
