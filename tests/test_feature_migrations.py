"""Feature upgrades must never replace existing login or job data."""
import concurrent.futures
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from drivetheater.db import Database
from drivetheater.auth import ensure_admin, make_session, session_user


class FeatureMigrationTests(unittest.TestCase):
    def test_legacy_session_and_password_are_preserved_on_repeat_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db.sqlite3'
            with sqlite3.connect(path) as conn:
                conn.executescript('CREATE TABLE users(username TEXT PRIMARY KEY,password_hash TEXT NOT NULL,created_at TEXT NOT NULL); CREATE TABLE sessions(token_hash TEXT PRIMARY KEY,username TEXT NOT NULL REFERENCES users(username),expires_at INTEGER NOT NULL);')
                conn.execute('INSERT INTO users VALUES(?,?,?)', ('admin', 'preserve-this-synthetic-hash', '2026-01-01'))
                conn.execute('INSERT INTO sessions VALUES(?,?,?)', ('legacy-hash', 'admin', int(time.time()) + 3600))
            database = Database(path)
            database.initialize(); database.initialize()
            with database.connect() as conn:
                self.assertEqual(conn.execute('SELECT password_hash FROM users').fetchone()[0], 'preserve-this-synthetic-hash')
                row = conn.execute('SELECT * FROM sessions').fetchone()
                self.assertEqual(row['token_hash'], 'legacy-hash')
                self.assertTrue(row['id'])
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0], 1)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)

    def test_web_and_worker_can_initialize_features_concurrently(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db.sqlite3'
            Database(path).initialize()
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: Database(path).initialize(), range(8)))
            with Database(path).connect() as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0], 1)
                self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_named_session_insert_after_schema_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / 'db.sqlite3'); db.initialize()
            ensure_admin(db, 'admin', 'synthetic-test-password')
            token = make_session(db, 'admin', device='Fixture', network_hash='marker')
            self.assertEqual(session_user(db, token), 'admin')
            with db.connect() as conn:
                row = conn.execute('SELECT * FROM sessions').fetchone()
                self.assertEqual(row['device_label'], 'Fixture')
                self.assertNotEqual(row['id'], row['token_hash'])
                self.assertNotEqual(row['token_hash'], token)


if __name__ == '__main__':
    unittest.main()
