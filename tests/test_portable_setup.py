"""Portable private setup-bundle tests; no installer or provider execution."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('bootstrap_config', ROOT / 'scripts' / 'bootstrap_config.py')
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)


class PortableSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='drivetheater-portable-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.uid = os.getuid() if hasattr(os, 'getuid') and os.getuid() else 10001
        self.gid = os.getgid() if hasattr(os, 'getgid') and os.getgid() else 10001

    def initialize(self, name):
        env, data = self.root / f'{name}.env', self.root / name
        with patch.object(config.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(200 * 1024**3, 0, 100 * 1024**3)):
            config.initialize(env, data, self.uid, self.gid, 8080, public_url='https://new.example')
        return env, data

    def test_export_and_import_preserve_sources_only_and_rewrite_paths(self):
        old_env, old_data = self.initialize('old')
        source_id = 'a' * 32
        credential = old_data / 'config' / f'{source_id}.json'
        credential.write_text(json.dumps({'web': {'client_id': 'client', 'client_secret': 'secret'}}), encoding='utf-8')
        if os.name == 'posix':
            os.chmod(credential, 0o600)
        with sqlite3.connect(old_data / 'db' / 'drivetheater.sqlite3') as db:
            config._setup_db(old_data / 'db' / 'drivetheater.sqlite3')
            db.execute('INSERT INTO sources(id,name,kind,auth_mode,folder_id,credential_path,oauth_redirect_url) VALUES (?,?,?,?,?,?,?)',
                       (source_id, 'Private drive', 'google_drive', 'oauth', 'folder_1', str(credential), 'https://old.example/callback'))
            db.execute("INSERT INTO settings(key,value) VALUES ('cache_limit_gb','55')")
            db.execute("CREATE TABLE users (username TEXT)")
            db.execute("INSERT INTO users VALUES ('not-exported')")
        bundle = self.root / 'private-setup.json'
        config.export_setup_bundle(old_env, bundle)
        if os.name == 'posix':
            self.assertEqual(bundle.stat().st_mode & 0o777, 0o600)
        raw = json.loads(bundle.read_text(encoding='utf-8'))
        self.assertEqual(set(raw), {'schema', 'sources', 'credentials', 'settings'})
        self.assertNotIn('users', json.dumps(raw))

        new_env, new_data = self.initialize('new')
        imported = config.import_setup_bundle(bundle, new_data, 'https://new.example')
        self.assertEqual(imported, 1)
        new_credential = new_data / 'config' / f'{source_id}.json'
        self.assertEqual(json.loads(new_credential.read_text(encoding='utf-8')), raw['credentials'][source_id])
        with sqlite3.connect(new_data / 'db' / 'drivetheater.sqlite3') as db:
            row = db.execute('SELECT credential_path,oauth_redirect_url FROM sources WHERE id=?', (source_id,)).fetchone()
            self.assertEqual(row, (str(new_credential), 'https://new.example/api/sources/google/callback'))
            self.assertEqual(db.execute("SELECT value FROM settings WHERE key='cache_limit_gb'").fetchone()[0], '55')
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='users'").fetchone())

    def test_rejects_unexpected_fields_and_symlink_bundle(self):
        _, data = self.initialize('runtime')
        bundle = self.root / 'bundle.json'
        bundle.write_text(json.dumps({'schema': 1, 'sources': [], 'credentials': {}, 'settings': {}, 'history': []}), encoding='utf-8')
        if os.name == 'posix':
            os.chmod(bundle, 0o600)
        with self.assertRaises(ValueError):
            config.import_setup_bundle(bundle, data, '')
        target = self.root / 'target.json'
        target.write_text('{}', encoding='utf-8')
        link = self.root / 'link.json'
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest('Symlink creation requires unavailable Windows privileges')
        with self.assertRaises(ValueError):
            config.read_setup_bundle(link)

    def test_container_paths_round_trip_without_exposing_host_paths(self):
        old_env, old_data = self.initialize('container-old')
        source_id = 'b' * 32
        credential = old_data / 'config' / f'{source_id}.json'
        credential.write_text(json.dumps({'api_key': 'fixture-not-a-real-key'}), encoding='utf-8')
        os.chmod(credential, 0o600)
        config._setup_db(old_data / 'db' / 'drivetheater.sqlite3')
        with sqlite3.connect(old_data / 'db' / 'drivetheater.sqlite3') as db:
            db.execute('INSERT INTO sources(id,name,kind,auth_mode,folder_id,credential_path) VALUES (?,?,?,?,?,?)',
                       (source_id, 'Synthetic', 'google_drive', 'api_key', 'fixture_folder', f'/data/config/{source_id}.json'))
        bundle = self.root / 'container-private.json'
        config.export_setup_bundle(old_env, bundle)
        _, new_data = self.initialize('container-new')
        config.import_setup_bundle(bundle, new_data, '', runtime_data_dir='/data')
        with sqlite3.connect(new_data / 'db' / 'drivetheater.sqlite3') as db:
            stored = db.execute('SELECT credential_path FROM sources').fetchone()[0]
        self.assertEqual(stored, f'/data/config/{source_id}.json')
        self.assertEqual(json.loads((new_data / 'config' / f'{source_id}.json').read_text()), {'api_key': 'fixture-not-a-real-key'})
        if os.name == 'posix' and os.geteuid() == 0:
            expected = (new_data / 'config').stat()
            for path in (new_data / 'config' / f'{source_id}.json', new_data / 'db' / 'drivetheater.sqlite3'):
                self.assertEqual((path.stat().st_uid, path.stat().st_gid), (expected.st_uid, expected.st_gid))

    def test_import_rejects_invalid_runtime_settings_before_writing(self):
        _, data = self.initialize('invalid-setting')
        for key, value in [('cache_limit_gb', 'NaN'), ('download_schedule_enabled', 'yes'),
                           ('download_start_hour', '24'), ('download_concurrency', '0')]:
            with self.subTest(key=key):
                bundle = self.root / (key + '.json')
                bundle.write_text(json.dumps({'schema': 1, 'sources': [], 'credentials': {}, 'settings': {key: value}}), encoding='utf-8')
                os.chmod(bundle, 0o600)
                with self.assertRaises(ValueError):
                    config.import_setup_bundle(bundle, data, '')
        self.assertFalse((data / 'db' / 'drivetheater.sqlite3').exists())

    def test_private_addon_connections_survive_monthly_setup_bundle(self):
        old_env, old_data = self.initialize('addons-old')
        config._setup_db(old_data / 'db' / 'drivetheater.sqlite3')
        manifest = 'https://provider.example/private-fixture-config/manifest.json'
        with sqlite3.connect(old_data / 'db' / 'drivetheater.sqlite3') as db:
            db.execute("INSERT INTO settings(key,value) VALUES ('torrentio_manifest_url',?)", (manifest,))
        bundle = self.root / 'addons-private.json'
        config.export_setup_bundle(old_env, bundle)
        new_env, new_data = self.initialize('addons-new')
        config.import_setup_bundle(bundle, new_data, '')
        with sqlite3.connect(new_data / 'db' / 'drivetheater.sqlite3') as db:
            self.assertEqual(db.execute("SELECT value FROM settings WHERE key='torrentio_manifest_url'").fetchone()[0], manifest)
        self.assertNotEqual(config.read_env(old_env)['DT_ADMIN_PASSWORD'], config.read_env(new_env)['DT_ADMIN_PASSWORD'])
        self.assertNotIn(config.read_env(old_env)['DT_ADMIN_PASSWORD'], bundle.read_text())

    def test_new_cache_limit_is_three_quarters_of_free_disk(self):
        env, _ = self.initialize('limit')
        self.assertEqual(config.read_env(env)['DT_CACHE_LIMIT_GB'], '75')


if __name__ == '__main__':
    unittest.main(verbosity=2)
