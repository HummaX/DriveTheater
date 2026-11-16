"""Offline checks for observed health transitions and bounded maintenance."""
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from drivetheater.db import Database
from drivetheater.feature_maintenance import observe_conditions


class FeatureMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dt-health-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.db = Database(self.root / 'db.sqlite3')
        self.db.initialize()
        self.now = int(time.time())
        with self.db.transaction() as conn:
            conn.execute('INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)', ('admin', 'not-a-real-password', 'synthetic'))
            conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('min_free_gb','2')")
            conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('worker_heartbeat',?)", (str(self.now),))

    def observe(self, free=4 * 1024 ** 3):
        with patch('drivetheater.feature_maintenance.shutil.disk_usage', return_value=SimpleNamespace(free=free)), patch('drivetheater.notifications.time.time', return_value=self.now):
            observe_conditions(self.db, self.root, now=self.now)

    def notices(self):
        with self.db.connect() as conn:
            return list(conn.execute('SELECT kind,title,message FROM notifications ORDER BY rowid'))

    def test_only_enter_and_recovery_transitions_notify(self):
        self.observe()
        self.assertEqual(self.notices(), [])
        with self.db.transaction() as conn:
            conn.execute("UPDATE settings SET value=? WHERE key='worker_heartbeat'", (str(self.now - 60),))
        self.observe(free=100)
        self.observe(free=100)
        self.assertEqual([row['kind'] for row in self.notices()], ['low_disk', 'worker_offline'])
        with self.db.transaction() as conn:
            conn.execute("UPDATE settings SET value=? WHERE key='worker_heartbeat'", (str(self.now),))
        self.observe()
        self.observe()
        self.assertEqual([row['kind'] for row in self.notices()], ['low_disk', 'worker_offline', 'low_disk_recovered', 'worker_offline_recovered'])
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM notification_outbox').fetchone()[0], 0)
            sample = json.loads(conn.execute("SELECT body FROM operation_samples WHERE kind='health' ORDER BY id DESC LIMIT 1").fetchone()[0])
        self.assertEqual(sample['queued_jobs'], 0)
        self.assertEqual(sample['worker_heartbeat_age_seconds'], 0)
        self.assertEqual(set(sample), {'disk_free_bytes', 'queued_jobs', 'worker_heartbeat_age_seconds'})

    def test_backup_notice_is_deduplicated_and_does_not_claim_encryption(self):
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backup_records(id,created_at,status,revision) VALUES('private-backup',?,'created','private-revision')", (self.now,))
        self.observe()
        self.observe()
        rows = self.notices()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['kind'], 'backup_created')
        self.assertIn('Encryption and disaster restore are not implied', rows[0]['message'])
        self.assertNotIn('private-backup', json.dumps([dict(row) for row in rows]))
        self.assertNotIn('private-revision', json.dumps([dict(row) for row in rows]))

    def test_expiry_prunes_only_expired_tokens_and_old_security_events(self):
        with self.db.transaction() as conn:
            for suffix, expiry in [('expired', self.now - 1), ('valid', self.now + 60)]:
                conn.execute('INSERT INTO auth_challenges(id,username,peer_hash,expires_at) VALUES(?,?,?,?)', (suffix, 'admin', 'private-network-hash', expiry))
                conn.execute('INSERT INTO security_confirmations(id,username,kind,body,expires_at) VALUES(?,?,?,?,?)', (suffix, 'admin', 'network', '{}', expiry))
                conn.execute('INSERT INTO download_plans(id,username,body,expires_at) VALUES(?,?,?,?)', (suffix, 'admin', '{}', expiry))
                conn.execute('INSERT INTO cleanup_plans(id,username,body,created_at,expires_at) VALUES(?,?,?,?,?)', (suffix, 'admin', '{}', self.now, expiry))
            conn.execute("INSERT INTO security_events(occurred_at,username,event_type) VALUES(?,'admin','old')", (self.now - 91 * 86400,))
            conn.execute("INSERT INTO security_events(occurred_at,username,event_type) VALUES(?,'admin','recent')", (self.now,))
            conn.execute("INSERT INTO mfa_credentials(username,enabled,secret_cipher) VALUES('admin',1,'opaque-synthetic-ciphertext')")
        self.observe()
        with self.db.connect() as conn:
            for table in ('auth_challenges', 'security_confirmations', 'download_plans', 'cleanup_plans'):
                self.assertEqual([row[0] for row in conn.execute(f'SELECT id FROM {table}')], ['valid'])
            self.assertEqual([row[0] for row in conn.execute('SELECT event_type FROM security_events')], ['recent'])
            self.assertEqual(conn.execute("SELECT enabled FROM mfa_credentials WHERE username='admin'").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT password_hash FROM users WHERE username='admin'").fetchone()[0], 'not-a-real-password')


if __name__ == '__main__':
    unittest.main()
