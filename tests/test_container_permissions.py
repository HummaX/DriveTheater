"""Container permission regressions; never build an image or modify host storage.

Portable tests inspect the real Dockerfile and exercise its non-root build gate
with synthetic access checks. A separate POSIX test runs the real chmod commands
on temporary files and drops privileges in a child when run as root. Windows
cannot validate Linux ownership/permission enforcement and skips that test.
"""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / 'Dockerfile'
COMPOSE = ROOT / 'compose.yaml'
GATE = re.compile(r"^RUN python - <<'PY'\n(.*?)^PY$", re.MULTILINE | re.DOTALL)
DOCKER_COMMAND = re.compile(r'^CMD (\[.*\])$', re.MULTILINE)
COMPOSE_WEB_COMMAND = re.compile(r'^  web:\n(?:.*\n)*?^    command: (\[.*\])$', re.MULTILINE)


def permission_commands(source):
    commands = re.findall(r'^RUN (chmod [^\n]+)$', source, re.MULTILINE)
    if len(commands) != 1:
        raise AssertionError('Expected exactly one scoped application permission rule')
    parsed = [shlex.split(command.strip()) for command in commands[0].split('&&')]
    expected = [
        ['chmod', '0755', '/app'],
        ['chmod', '-R', 'a=rX', '/app/drivetheater', '/app/ui'],
    ]
    if parsed != expected:
        raise AssertionError('Only immutable application code/assets may be normalized')
    return parsed


def readability_gate(source):
    matches = list(GATE.finditer(source))
    if len(matches) != 1:
        raise AssertionError('Expected one non-root image readability gate')
    return matches[0]


def json_command(pattern, source, label):
    matches = list(pattern.finditer(source))
    if len(matches) != 1:
        raise AssertionError(f'Expected one {label}')
    return json.loads(matches[0].group(1))


class ContainerPermissionTests(unittest.TestCase):
    def setUp(self):
        self.source = DOCKERFILE.read_text(encoding='utf-8')
        self.gate = readability_gate(self.source).group(1)
        self.tmp = tempfile.TemporaryDirectory(prefix='drivetheater-image-permissions-')
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name)
        self.app = self.parent / 'app'
        for directory in ('drivetheater/nested', 'ui/assets'):
            (self.app / directory).mkdir(parents=True)
        self.files = {
            'drivetheater/__init__.py': '"""Synthetic package; no application startup."""\n',
            'drivetheater/nested/helper.py': 'VALUE = 1\n',
            'ui/index.html': '<!doctype html><title>Synthetic UI</title>',
            'ui/assets/app.js': 'void 0;',
        }
        for name, content in self.files.items():
            (self.app / name).write_text(content, encoding='utf-8')

    def run_portable_gate(self, scenario='readable'):
        # These access overrides test the real gate's logic, not Windows ACLs.
        program = '''
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, os.getcwd())
scenario, source = sys.argv[1:]
real_open = Path.open
real_walk = os.walk

def fixture_open(path, *args, **kwargs):
    if scenario == 'unreadable-code' and path.as_posix() == 'drivetheater/nested/helper.py':
        raise PermissionError('synthetic unreadable code')
    if scenario == 'unreadable-ui' and path.as_posix() == 'ui/assets/app.js':
        raise PermissionError('synthetic unreadable UI')
    return real_open(path, *args, **kwargs)

def fixture_access(path, mode):
    normalized = Path(path).as_posix()
    return (
        (scenario == 'writable-code' and normalized == 'drivetheater/nested/helper.py')
        or (scenario == 'writable-directory' and normalized == 'ui/assets')
    )

def fixture_walk(root, onerror=None):
    if scenario == 'unreadable-directory' and str(root) == 'ui':
        onerror(PermissionError('synthetic unreadable directory'))
    yield from real_walk(root, onerror=onerror)

with patch.object(os, 'geteuid', return_value=0 if scenario == 'root' else 10001, create=True), \\
     patch.object(os, 'access', side_effect=fixture_access), \\
     patch.object(os, 'walk', side_effect=fixture_walk), \\
     patch.object(Path, 'open', fixture_open):
    exec(compile(source, '<image-readability-check>', 'exec'), {})
'''
        return subprocess.run(
            [sys.executable, '-I', '-B', '-c', program, scenario, self.gate],
            cwd=self.app, capture_output=True, text=True, timeout=10,
        )

    def test_permissions_are_scoped_and_set_after_copy_before_non_root_gate(self):
        permission_commands(self.source)
        normalization = self.source.index('RUN chmod ')
        user = self.source.index('\nUSER 10001:10001\n')
        gate = readability_gate(self.source).start()
        self.assertLess(self.source.index('COPY backend/drivetheater ./drivetheater'), normalization)
        self.assertLess(self.source.index('COPY --from=ui /build/dist ./ui'), normalization)
        self.assertLess(normalization, user)
        self.assertLess(user, gate)
        self.assertNotRegex(self.source[user + 1:], r'(?m)^USER (?:root|0)(?::|\s|$)')
        self.assertNotRegex(self.source[normalization:user], r'(?:\b777\b|/data(?:/|\b)|/etc(?:/|\b)|\bchown\b)')
        self.assertIn('PYTHONDONTWRITEBYTECODE=1', self.source)
        compile(self.gate, '<image-readability-check>', 'exec')

    def test_gate_accepts_readable_read_only_synthetic_assets(self):
        result = self.run_portable_gate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('readable and read-only', result.stdout)

    def test_default_web_commands_disable_untrusted_proxy_headers(self):
        image = json_command(DOCKER_COMMAND, self.source, 'image CMD')
        compose = json_command(
            COMPOSE_WEB_COMMAND,
            COMPOSE.read_text(encoding='utf-8'),
            'Compose web command',
        )
        self.assertEqual(image, [
            'python', '-m', 'uvicorn', 'drivetheater.main:app',
            '--host', '0.0.0.0', '--port', '8000',
            '--no-access-log', '--no-proxy-headers',
        ])
        self.assertEqual(compose, [
            'uvicorn', 'drivetheater.main:app', '--host', '0.0.0.0',
            '--port', '8000', '--no-proxy-headers',
        ])
        self.assertNotIn('--proxy-headers', image)
        self.assertNotIn('--proxy-headers', compose)

    def test_gate_rejects_root_and_inaccessible_or_writable_assets(self):
        for scenario, expected in (
            ('root', 'must run as a non-root user'),
            ('unreadable-code', 'synthetic unreadable code'),
            ('unreadable-ui', 'synthetic unreadable UI'),
            ('unreadable-directory', 'synthetic unreadable directory'),
            ('writable-code', 'Application file is writable'),
            ('writable-directory', 'Application directory is writable'),
        ):
            with self.subTest(scenario=scenario):
                result = self.run_portable_gate(scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)

    def test_gate_rejects_missing_ui_index(self):
        (self.app / 'ui/index.html').unlink()
        result = self.run_portable_gate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('index.html', result.stderr)

    @unittest.skipUnless(os.name == 'posix' and shutil.which('chmod'), 'Real Linux/POSIX permission enforcement is required')
    def test_real_normalization_repairs_restrictive_context_without_changing_private_data(self):
        # This separate directory stands in for private storage; never use /data.
        private = self.parent / 'private-data'
        private.mkdir(mode=0o700)
        secret = private / 'sentinel'
        secret.write_bytes(b'private synthetic bytes')
        os.chmod(secret, 0o600)
        before = (private.stat().st_mode & 0o777, secret.stat().st_mode & 0o777, secret.read_bytes())

        self.parent.chmod(0o755)
        self.app.chmod(0o755)
        for path in self.app.rglob('*'):
            path.chmod(0o700 if path.is_dir() else 0o600)
        if os.geteuid() != 0:
            # The same owner can read 0600; 0000 models another UID's denial.
            (self.app / 'drivetheater/__init__.py').chmod(0o000)

        program = '''
import os
import sys
if os.geteuid() == 0:
    os.setgroups([])
    os.setgid(10001)
    os.setuid(10001)
sys.path.insert(0, os.getcwd())
exec(compile(sys.argv[1], '<image-readability-check>', 'exec'), {})
'''
        command = [sys.executable, '-I', '-B', '-c', program, self.gate]
        failed = subprocess.run(command, cwd=self.app, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('PermissionError', failed.stderr)

        targets = {
            '/app': str(self.app),
            '/app/drivetheater': str(self.app / 'drivetheater'),
            '/app/ui': str(self.app / 'ui'),
        }
        for chmod in permission_commands(self.source):
            subprocess.run([targets.get(arg, arg) for arg in chmod], check=True, capture_output=True, timeout=10)
        result = subprocess.run(command, cwd=self.app, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('readable and read-only', result.stdout)
        for path in self.app.rglob('*'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o555 if path.is_dir() else 0o444)
        self.assertEqual((private.stat().st_mode & 0o777, secret.stat().st_mode & 0o777, secret.read_bytes()), before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
