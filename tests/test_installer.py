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

    def installer_definitions(self):
        source = (ROOT / 'scripts' / 'install.sh').read_text(encoding='utf-8')
        definitions, marker, trailing = source.rpartition('\nmain "$@"')
        self.assertTrue(marker, 'Installer entry point must be identified before fixture execution')
        self.assertFalse(trailing.strip(), 'Unexpected commands after installer entry point')
        self.assertNotRegex(definitions, r'(?m)^[ \t]*main(?:[ \t;]|$)', 'Unexpected earlier installer invocation')
        return source, definitions

    def run_shell_fixture(self, program, *args):
        # Load definitions only, never main(): no root, apt, Docker, Git or host edits.
        _, definitions = self.installer_definitions()
        return subprocess.run(
            ['bash', '-s', '--', *args], input=definitions + '\n' + program,
            capture_output=True, text=True, timeout=10,
        )

    def test_ubuntu_point_release_preserves_requested_git_revision(self):
        source, _ = self.installer_definitions()
        start = source.index('if (( FRESH )); then\n    git -C')
        checkout = source[start:source.index('\nfi', start) + len('\nfi')]
        parse_start = source.index('while (( $# )); do')
        arguments = source[parse_start:source.index('# A piped bootstrap', parse_start)]
        with tempfile.TemporaryDirectory(prefix='drivetheater-os-release-') as directory:
            release = Path(directory) / 'os-release'
            release.write_text(
                'PRETTY_NAME="Ubuntu 24.04.4 LTS"\n'
                'NAME="Ubuntu"\n'
                'VERSION_ID="24.04"\n'
                'VERSION="24.04.4 LTS (Noble Numbat)"\n'
                'ID=ubuntu\nID_LIKE=debian\n', encoding='utf-8',
            )
            for revision in (None, '42d2f9f9e1fcc7397957ce7d9482f0ce3e191742', 'main', 'claude-code', 'v1.2.3'):
                with self.subTest(revision=revision):
                    program = '''
release_file=$1
shift
''' + arguments + '''
VERSION=caller-version
ID=caller-id
VERSION_ID=caller-release
check_ubuntu_release "$release_file"
printf 'state:%s:%s:%s:%s:%s\\n' "$APP_VERSION" "$VERSION" "$ID" "$VERSION_ID" "$PORT"
git() {
    case " $* " in *' status --porcelain '*) return 0;; esac
    printf 'git-arg:%s\\n' "$@"
}
FRESH=1
INSTALL_DIR=/synthetic/checkout
''' + checkout
                    expected = 'main' if revision is None else revision
                    args = ['--port', '8080']
                    if revision is not None:
                        args += ['--version', revision]
                    result = self.run_shell_fixture(program, release.as_posix(), *args)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(f'state:{expected}:caller-version:caller-id:caller-release:8080', result.stdout)
                    self.assertIn(f'git-arg:{expected}\n', result.stdout)
                    self.assertNotIn('Noble Numbat', result.stdout)
                    self.assertIn('git-arg:FETCH_HEAD', result.stdout)

    def test_os_check_rejects_other_distributions_and_missing_release(self):
        with tempfile.TemporaryDirectory(prefix='drivetheater-os-check-') as directory:
            release = Path(directory) / 'os-release'
            for contents in ('ID=ubuntu\nVERSION_ID=22.04\n', 'ID=debian\nVERSION_ID=24.04\n', ''):
                with self.subTest(contents=contents):
                    release.write_text(contents, encoding='utf-8')
                    result = self.run_shell_fixture('check_ubuntu_release "$1"', release.as_posix())
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('This installer targets Ubuntu 24.04', result.stderr)
            missing = self.run_shell_fixture('check_ubuntu_release "$1"', (Path(directory) / 'missing').as_posix())
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn('Cannot identify the operating system', missing.stderr)

    def test_readiness_timeout_reports_recovery_instead_of_unbound_variable(self):
        source, _ = self.installer_definitions()
        start = source.index("echo '4. Waiting for DriveTheater readiness...'")
        readiness = source[start:source.index('\nADDRESS=', start)]
        program = '''
ENV_FILE=/synthetic/private/install.env
DATA_DIR=/synthetic/data
INSTALL_DIR=/synthetic/checkout
PORT=8080
curl() { return 1; }
python3() { return 1; }
sleep() { :; }
''' + readiness
        result = self.run_shell_fixture(program)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Readiness failed. Preserve /synthetic/private/install.env and /synthetic/data', result.stderr)
        self.assertIn('manage.sh logs', result.stderr)
        self.assertNotIn('unbound variable', result.stderr)

    def test_shell_syntax(self):
        for name in ('install.sh', 'manage.sh', 'update.sh'):
            result = subprocess.run(['bash', '-n', str(ROOT / 'scripts' / name)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
