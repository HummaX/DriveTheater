from __future__ import annotations

import json
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

import httpx
from fastapi import HTTPException
from drivetheater.db import Database
from drivetheater import source_health as health


class SourceHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / 'health.sqlite3')
        self.db.initialize()
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO users(username,password_hash,created_at) VALUES('one','x','now'),('two','x','now')")
            conn.execute("INSERT INTO sources(id,name,kind) VALUES('source','Private source title','drive'),('untested','Untested','drive')")

    def tearDown(self):
        self.tmp.cleanup()

    def error(self, code, headers=None):
        request = httpx.Request('GET', 'https://private.example.net/folder?key=PRIVATE_KEY')
        response = httpx.Response(code, request=request, headers=headers, text='private provider response token=SECRET')
        return httpx.HTTPStatusError('private error /run/secrets/service.json token=SECRET', request=request, response=response)

    def test_known_errors_are_safe_actionable_and_retry_advisory_only(self):
        cases = [(401, 'auth_required', False), (403, 'permission_denied', False), (404, 'not_found', False), (429, 'rate_limited', True), (503, 'provider_unavailable', True)]
        for status, code, retryable in cases:
            with self.subTest(status=status):
                result = health.classify_error(self.error(status))
                self.assertEqual(result['code'], code)
                self.assertEqual(result['retryable'], retryable)
                self.assertEqual(set(result), {'code', 'message', 'retry_at', 'retryable'})
                serialized = json.dumps(result)
                for secret in ('SECRET', 'PRIVATE_KEY', 'private.example', '/run/secrets', 'service.json'):
                    self.assertNotIn(secret, serialized)
        for error in (httpx.ConnectError('https://user:secret@private.example/'), TimeoutError('SECRET'), socket.gaierror('SECRET')):
            self.assertEqual(health.classify_error(error)['code'], 'network_error')
        self.assertEqual(health.classify_error(RuntimeError('Google credential needs OAuth authorization'))['code'], 'auth_required')
        self.assertEqual(health.classify_error(RuntimeError('unknown error token=SECRET https://127.0.0.1'))['code'], 'unknown_error')
        self.assertNotIn('SECRET', json.dumps(health.classify_error(RuntimeError('unknown error token=SECRET https://127.0.0.1'))))

    def test_rate_limit_retry_header_is_bounded(self):
        with patch('drivetheater.source_health.time.time', return_value=10000):
            self.assertEqual(health.classify_error(self.error(429, {'Retry-After': '900000'}))['retry_at'], 13600)
            self.assertEqual(health.classify_error(self.error(429, {'Retry-After': '12'}))['retry_at'], 10012)
            self.assertEqual(health.classify_error(self.error(429, {'Retry-After': 'private text secret'}))['retry_at'], 10060)

    def test_metadata_attachment_is_copy_and_has_no_jobs_or_provider_calls(self):
        source = {'id': 'source', 'name': 'Visible name', 'status': 'ready'}
        with patch.object(httpx, 'Client') as client, patch.object(socket, 'getaddrinfo') as dns:
            attached = health.attach_health(self.db, source)
            self.assertEqual(attached['health']['state'], 'unknown')
            self.assertEqual(health.get_health(self.db, 'untested')['state'], 'unknown')
            health.record_result(self.db, 'source', 'test', True)
            health.record_result(self.db, 'source', 'sync', False, self.error(403))
            client.assert_not_called(); dns.assert_not_called()
        self.assertNotIn('health', source)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM videos').fetchone()[0], 0)
            row = conn.execute("SELECT status,video_count,last_sync FROM sources WHERE id='source'").fetchone()
        self.assertEqual(dict(row), {'status': 'ready', 'video_count': 0, 'last_sync': None})

    def test_separate_test_and_sync_success_timestamps_and_oauth(self):
        with patch('drivetheater.source_health.time.time', return_value=10000):
            first = health.record_result(self.db, 'source', 'test', True)
        self.assertEqual(first['last_test_at'], 10000)
        self.assertEqual(first['last_success_at'], 10000)
        self.assertEqual(first['last_test_success_at'], 10000)
        with patch('drivetheater.source_health.time.time', return_value=10100):
            sync = health.record_result(self.db, 'source', 'sync', True)
        self.assertEqual(sync['last_sync_at'], 10100)
        self.assertEqual(sync['last_sync_success_at'], 10100)
        self.assertEqual(sync['last_test_success_at'], 10000)
        with patch('drivetheater.source_health.time.time', return_value=10200):
            failed = health.record_result(self.db, 'source', 'test', False, self.error(401))
        self.assertEqual(failed['last_test_at'], 10200)
        self.assertEqual(failed['last_test_success_at'], 10000)
        self.assertEqual(failed['last_sync_success_at'], 10100)
        with patch('drivetheater.source_health.time.time', return_value=10300):
            oauth = health.record_result(self.db, 'source', 'oauth', True)
        self.assertEqual(oauth['state'], 'healthy')
        self.assertEqual(oauth['last_test_at'], 10200)
        self.assertEqual(oauth['last_test_success_at'], 10000)
        self.assertEqual(oauth['last_sync_at'], 10100)
        self.assertEqual(oauth['last_sync_success_at'], 10100)

    def test_failure_recovery_transitions_are_deduplicated_per_user(self):
        health.record_result(self.db, 'source', 'test', True)
        failure = health.record_result(self.db, 'source', 'sync', False, self.error(401))
        self.assertEqual(failure['failures'], 1)
        repeated = health.record_result(self.db, 'source', 'sync', False, self.error(403))
        self.assertEqual(repeated['failures'], 2)
        self.assertEqual(repeated['error_code'], 'permission_denied')
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications WHERE kind='source_failure'").fetchone()[0], 2)
        recovered = health.record_result(self.db, 'source', 'sync', True)
        health.record_result(self.db, 'source', 'test', True)
        self.assertEqual(recovered['failures'], 0)
        self.assertIsNone(recovered['error_code']); self.assertIsNone(recovered['message']); self.assertIsNone(recovered['retry_at'])
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications WHERE kind='source_recovered'").fetchone()[0], 2)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM notification_outbox').fetchone()[0], 0)
        health.record_result(self.db, 'source', 'test', False, self.error(429))
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications WHERE kind='source_failure'").fetchone()[0], 4)

    def test_result_and_transition_notifications_rollback_together(self):
        with patch('drivetheater.source_health.emit_event', side_effect=RuntimeError('abort before commit')):
            with self.assertRaises(RuntimeError):
                health.record_result(self.db, 'source', 'test', False, self.error(401))
        self.assertEqual(health.get_health(self.db, 'source')['state'], 'unknown')
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM source_health_history').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0], 0)

    def test_history_is_bounded_and_fallback_never_leaks_private_source_details(self):
        for _ in range(health.HISTORY_LIMIT + 5):
            health.record_result(self.db, 'source', 'test', False, RuntimeError('token=SECRET https://10.0.0.1/private'))
        with self.db.connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM source_health_history WHERE source_id='source'").fetchone()[0]
            messages = json.dumps([dict(row) for row in conn.execute('SELECT * FROM notifications')])
        self.assertEqual(count, health.HISTORY_LIMIT)
        safe = json.dumps(health.get_health(self.db, 'source')) + messages
        self.assertNotIn('SECRET', safe); self.assertNotIn('10.0.0.1', safe); self.assertNotIn('Private source title', safe)

    def test_hard_delete_cleanup_is_transactional_and_preserves_historical_notices(self):
        health.record_result(self.db, 'source', 'test', False, self.error(401))
        with self.assertRaises(RuntimeError):
            with self.db.transaction() as conn:
                health.delete_health(conn, 'source')
                conn.execute("DELETE FROM sources WHERE id='source'")
                raise RuntimeError('rollback deletion')
        self.assertEqual(health.get_health(self.db, 'source')['state'], 'error')
        with self.db.transaction() as conn:
            health.delete_health(conn, 'source')
            conn.execute("DELETE FROM sources WHERE id='source'")
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM source_health').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM source_health_history').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0], 2)
            with self.assertRaises(ValueError): health.delete_health(conn, 'source')

    def test_input_validation_and_missing_source_do_not_create_state(self):
        for action, success in [('download', True), ('sync', 1)]:
            with self.assertRaises(ValueError): health.record_result(self.db, 'source', action, success)
        with self.assertRaises(ValueError): health.record_result(self.db, 'missing', 'test', True)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM source_health').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
