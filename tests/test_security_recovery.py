from contextlib import closing
import importlib.util
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from drivetheater.db import Database

spec = importlib.util.spec_from_file_location('security_recovery', ROOT / 'scripts/security_recovery.py')
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class SecurityRecoveryTests(unittest.TestCase):
    def test_requires_local_root_and_explicit_confirmation(self):
        with patch.object(recovery.os, 'geteuid', return_value=1000, create=True):
            with self.assertRaises(PermissionError):
                recovery.recover_network(Path('/missing'), confirm=True)
        with patch.object(recovery.os, 'geteuid', return_value=0, create=True):
            with self.assertRaises(ValueError):
                recovery.recover_network(Path('/missing'))
            with self.assertRaises(ValueError):
                recovery.recover_network(Path('relative.sqlite3'), confirm=True)

    def test_recovers_network_only_and_keeps_private_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'app.sqlite3'
            db = Database(database); db.initialize()
            with db.transaction() as conn:
                conn.execute("INSERT INTO users VALUES('admin','preserved-hash','old')")
                conn.execute("INSERT INTO mfa_credentials(username,secret_cipher,enabled) VALUES('admin','preserved-cipher',1)")
                conn.execute("INSERT INTO security_policies VALUES('network','broken-policy')")
                conn.execute("INSERT INTO security_policies VALUES('unrelated','preserved')")
                conn.execute("INSERT INTO security_confirmations VALUES('one','admin','network','{}',9999999999)")
            with patch.object(recovery.os, 'geteuid', return_value=0, create=True):
                snapshot = recovery.recover_network(database, confirm=True)
            with db.connect() as conn:
                self.assertIsNone(conn.execute("SELECT 1 FROM security_policies WHERE key='network'").fetchone())
                self.assertEqual(conn.execute('SELECT password_hash FROM users').fetchone()[0], 'preserved-hash')
                self.assertEqual(conn.execute('SELECT secret_cipher FROM mfa_credentials').fetchone()[0], 'preserved-cipher')
                self.assertEqual(conn.execute("SELECT value FROM security_policies WHERE key='unrelated'").fetchone()[0], 'preserved')
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT event_type FROM security_events").fetchone()[0], 'local_network_recovery')
            with closing(sqlite3.connect(snapshot)) as conn:
                self.assertEqual(conn.execute("SELECT value FROM security_policies WHERE key='network'").fetchone()[0], 'broken-policy')
                self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertFalse(list(Path(directory).glob(snapshot.name + '-*')))
            if os.name == 'posix':
                self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
