"""Exercise the real backup producer and offline encryption without live data."""
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from drivetheater.db import Database


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BackupPipelineTests(unittest.TestCase):
    def test_live_wal_snapshot_with_artwork_encrypts_and_verifies_offline(self):
        bootstrap, encrypted = load('bootstrap_config'), load('encrypted_backup')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); data = root / 'data'; env = root / 'install.env'
            with patch.object(bootstrap.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(10**12, 0, 10**12)):
                bootstrap.initialize(env, data, os.getuid() if hasattr(os, 'getuid') and os.getuid() else 10001, os.getgid() if hasattr(os, 'getgid') and os.getgid() else 10001, 8080)
            db = Database(data / 'db/drivetheater.sqlite3'); db.initialize()
            (data / 'artwork').mkdir()
            # Offline backup validation preserves bytes but never renders/restores artwork.
            poster = data / 'artwork' / ('a' * 64 + '.png'); poster.write_bytes(b'synthetic-private-artwork')
            with closing(sqlite3.connect(db.path)) as active:
                active.execute('PRAGMA journal_mode=WAL')
                active.execute("INSERT INTO settings(key,value) VALUES('pipeline_fixture','preserved')"); active.commit()
                backup = bootstrap.backup(env, 'synthetic-revision')
                self.assertFalse(list(backup.glob('drivetheater.sqlite3-*')))
                archive = root / 'offline.dtbackup'
                encrypted.encrypt_backup(backup, archive, 'synthetic offline independent passphrase')
                result = encrypted.verify_backup(archive, 'synthetic offline independent passphrase')
                self.assertEqual(result['database_integrity'], 'ok')
                self.assertEqual(result['config_members'], 1)
                self.assertEqual(active.execute("SELECT value FROM settings WHERE key='pipeline_fixture'").fetchone()[0], 'preserved')
                self.assertEqual(active.execute('SELECT COUNT(*) FROM backup_records').fetchone()[0], 1)
                self.assertTrue((backup / 'install.env').exists())
                self.assertEqual(poster.read_bytes(), b'synthetic-private-artwork')
                with self.assertRaises(ValueError):
                    encrypted.verify_backup(archive, 'wrong independent passphrase')


if __name__ == '__main__':
    unittest.main()
