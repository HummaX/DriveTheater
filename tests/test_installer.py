"""Installer checks run only in disposable directories; no apt/Docker/host edits."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('bootstrap_config', ROOT / 'scripts' / 'bootstrap_config.py')
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='drivetheater-installer-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.data = self.root / 'runtime'
        self.env = self.root / 'install.env'
        self.uid = os.getuid() if hasattr(os, 'getuid') and os.getuid() > 0 else 10001
        self.gid = os.getgid() if hasattr(os, 'getgid') and os.getgid() > 0 else 10001

    def initialize(self):
        with patch.object(config.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(10**12, 0, 10**12)):
            return config.initialize(self.env, self.data, self.uid, self.gid, 8080)

    def test_fresh_root_disk_storage_needs_no_mount(self):
        values, created = self.initialize()
        self.assertTrue(created)
        self.assertEqual(values['DT_HOST_DATA_DIR'], self.data.as_posix())
        self.assertGreaterEqual(len(values['DT_SECRET']), 48)
        self.assertNotEqual(values['DT_SECRET'], values['DT_ADMIN_PASSWORD'])
        for name in ('db', 'config', 'cache', 'backups'):
            self.assertTrue((self.data / name).is_dir())
        self.assertEqual(config.verify_installation(self.env)['DT_PORT'], '8080')
        if os.name == 'posix':
            self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)

    def test_repeated_install_preserves_all_secret_bytes(self):
        values, _ = self.initialize()
        before = self.env.read_bytes()
        database = self.data / 'db' / 'drivetheater.sqlite3'
        database.write_bytes(b'preserve synthetic database bytes')
        second, created = self.initialize()
        self.assertFalse(created)
        self.assertEqual(second, values)
        self.assertEqual(self.env.read_bytes(), before)
        self.assertEqual(database.read_bytes(), b'preserve synthetic database bytes')

    def test_nonempty_data_is_never_adopted(self):
        self.data.mkdir()
        (self.data / 'existing').write_bytes(b'keep')
        with self.assertRaises(ValueError):
            self.initialize()
        self.assertFalse(self.env.exists())
        self.assertEqual((self.data / 'existing').read_bytes(), b'keep')

    def test_invalid_existing_config_is_not_replaced(self):
        self.env.write_text('DT_ADMIN_USER=admin\n')
        before = self.env.read_bytes()
        with self.assertRaises(ValueError):
            self.initialize()
        self.assertEqual(self.env.read_bytes(), before)

    def test_changed_identity_does_not_regenerate_credentials(self):
        self.initialize()
        before = self.env.read_bytes()
        with self.assertRaises(ValueError):
            config.initialize(self.env, self.data, self.uid, self.gid, 9999)
        self.assertEqual(self.env.read_bytes(), before)

    def test_restart_between_config_and_marker_recovers_empty_install_only(self):
        self.initialize()
        marker = self.data / '.drivetheater-install.json'
        marker.unlink()
        before = self.env.read_bytes()
        self.initialize()
        self.assertEqual(self.env.read_bytes(), before)
        self.assertTrue(json.loads(marker.read_text())['recovered'])
        marker.unlink()
        (self.data / 'cache' / 'existing.mp4').write_bytes(b'keep')
        with self.assertRaises(ValueError):
            self.initialize()
        self.assertEqual((self.data / 'cache' / 'existing.mp4').read_bytes(), b'keep')

    def test_missing_runtime_tree_is_not_silently_recreated(self):
        self.initialize()
        shutil.rmtree(self.data / 'db')
        with self.assertRaises(ValueError):
            config.verify_installation(self.env)

    def test_backup_contains_consistent_database_and_configuration_not_cache(self):
        self.initialize()
        database = self.data / 'db' / 'drivetheater.sqlite3'
        with sqlite3.connect(database) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE example(value TEXT)')
            db.execute('INSERT INTO example VALUES (?)', ('preserved',))
        (self.data / 'config' / 'private.json').write_text('{"fixture":"not-real"}')
        (self.data / 'cache' / 'movie.mp4').write_bytes(b'not part of backup')
        destination = config.backup(self.env, 'test-revision')
        with sqlite3.connect(destination / 'drivetheater.sqlite3') as db:
            self.assertEqual(db.execute('SELECT value FROM example').fetchone()[0], 'preserved')
        manifest = json.loads((destination / 'manifest.json').read_text())
        self.assertEqual(manifest['revision'], 'test-revision')
        self.assertFalse(manifest['media_included'])
        self.assertEqual((destination / 'install.env').read_bytes(), self.env.read_bytes())
        self.assertFalse((destination / 'movie.mp4').exists())
        self.assertTrue((destination / 'config.tar.gz').exists())

    def test_configuration_is_data_not_shell(self):
        self.initialize()
        self.env.write_bytes(self.env.read_bytes() + b'X=$(touch HARM)\n')
        with self.assertRaises(ValueError):
            config.read_env(self.env)
        self.assertFalse((self.root / 'HARM').exists())

    def test_symlink_storage_is_rejected(self):
        target = self.root / 'other'
        target.mkdir()
        try:
            self.data.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest('Symlink creation requires unavailable Windows privileges')
        with self.assertRaises(ValueError):
            self.initialize()

    def test_low_space_fails_before_credentials_or_data_are_created(self):
        with patch.object(config.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(100, 99, 1)):
            with self.assertRaises(ValueError):
                config.initialize(self.env, self.data, self.uid, self.gid, 80)
        self.assertFalse(self.env.exists())
        self.assertFalse(self.data.exists())

    def test_non_root_container_identity_is_required(self):
        with self.assertRaises(ValueError):
            config.initialize(self.env, self.data, 0, 0, 80)


@unittest.skipUnless(shutil.which('bash'), 'Bash is required')
class ShellInterfaceTests(unittest.TestCase):
    def run_script(self, *args):
        return subprocess.run(['bash', str(ROOT / 'scripts' / 'install.sh'), *args], capture_output=True, text=True, timeout=10)

    def test_help_requires_no_root_docker_or_ubuntu(self):
        result = self.run_script('--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--data-dir', result.stdout)

    def test_bad_arguments_stop_before_host_changes(self):
        for args in (('--wat',), ('--port',), ('--port', '65536'), ('--version', 'bad;command')):
            with self.subTest(args=args):
                result = self.run_script(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('Installing missing', result.stdout)

    def test_shell_syntax(self):
        for name in ('install.sh', 'manage.sh', 'update.sh'):
            result = subprocess.run(['bash', '-n', str(ROOT / 'scripts' / name)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
