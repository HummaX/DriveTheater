from __future__ import annotations

import io
import json
import socket
import ssl
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from drivetheater import notification_routes, notifications as n
from drivetheater.auth import hash_password, make_session
from drivetheater.crypto_utils import seal_secret
from drivetheater.db import Database
from drivetheater.security import _recovery_hash, totp

TOKEN = '12345678:' + 'a' * 35
WEBHOOK = 'https://discord.com/api/webhooks/123456789012345678/' + 'b' * 40
SMTP = {'host': 'smtp.example.com', 'port': 587, 'tls_mode': 'starttls', 'username': 'mailer', 'password': 'private-password', 'from_address': 'from@example.com', 'to_address': 'to@example.com'}


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / 'test.sqlite3')
        self.db.initialize()
        self.cfg = SimpleNamespace(secret='test-notification-master-secret-' + 'z' * 32)
        self.now = int(time.time()) + 10
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO users(username,password_hash,created_at) VALUES('one',?,'now'),('two',?,'now')", (hash_password('correct-password'), hash_password('other-password')))

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, kind='telegram', username='one', enabled=True):
        config = {'telegram': {'bot_token': TOKEN, 'chat_id': '123456'}, 'discord': {'webhook_url': WEBHOOK}, 'smtp': SMTP}[kind]
        return n.save_channel(self.db, self.cfg, username, kind, config, enabled=enabled)

    def event(self, key='example', username='one'):
        return n.notify(self.db, username, 'source_failure', 'A safe title', 'A safe message', key)

    def row(self):
        with self.db.connect() as conn:
            return dict(conn.execute('SELECT * FROM notification_outbox ORDER BY rowid DESC LIMIT 1').fetchone())

    def test_atomic_event_rollback_global_snapshot_and_dedupe(self):
        self.save(); self.save(username='two')
        with self.assertRaises(RuntimeError):
            with self.db.transaction() as conn:
                n.emit_event(conn, 'rollback', 'source_failure', 'Title', 'Message')
                raise RuntimeError('abort domain transaction')
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM notification_outbox').fetchone()[0], 0)
        self.assertEqual(len(self.event('global', None)), 2)
        self.assertEqual(self.event('global', None), [])
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO users(username,password_hash,created_at) VALUES('new','x','now')")
        self.assertEqual(self.event('global', None), [])
        self.assertEqual(n.list_inbox(self.db, 'new')['items'], [])
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM notification_outbox').fetchone()[0], 2)
            with self.assertRaises(ValueError):
                n.emit_event(conn, 'not-atomic', 'source_failure', 'Title', 'Message')

    def test_username_dedupe_scope_and_inbox_isolation(self):
        one = self.event()[0]
        two = self.event(username='two')[0]
        self.assertNotEqual(one, two)
        self.assertEqual(self.event(), [])
        self.assertEqual(n.list_inbox(self.db, 'one')['unread_count'], 1)
        with self.assertRaises(HTTPException) as err:
            n.update_inbox(self.db, 'two', one)
        self.assertEqual(err.exception.status_code, 404)
        n.update_inbox(self.db, 'one', one)
        self.assertEqual(n.list_inbox(self.db, 'one')['unread_count'], 0)
        self.assertTrue(n.list_inbox(self.db, 'one')['items'][0]['read'])
        n.update_inbox(self.db, 'one', one, dismiss=True)
        self.assertEqual(n.list_inbox(self.db, 'one')['items'], [])
        self.assertEqual(n.list_inbox(self.db, 'two')['unread_count'], 1)

    def test_disabled_save_and_test_never_send(self):
        with patch.object(n, '_deliver') as send, patch.object(n.httpx, 'Client') as client, patch.object(n.socket, 'getaddrinfo') as dns:
            self.save(enabled=False)
            self.event()
            self.assertEqual(n.process_outbox(self.db, self.cfg)['claimed'], 0)
            with self.assertRaises(HTTPException):
                n.queue_test(self.db, 'one', 'telegram')
            send.assert_not_called(); client.assert_not_called(); dns.assert_not_called()
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM notification_outbox').fetchone()[0], 0)

    def test_status_and_ciphertext_hide_secrets_and_updates_preserve_masked_fields(self):
        self.save()
        result = n.channel_status(self.db, 'one')
        self.assertEqual(set(result['items'][0]), {'kind', 'configured', 'enabled'})
        self.assertNotIn(TOKEN, json.dumps(result))
        with self.db.connect() as conn:
            cipher = conn.execute('SELECT config_cipher FROM notification_channels').fetchone()[0]
        self.assertNotIn(TOKEN, cipher)
        n.save_channel(self.db, self.cfg, 'one', 'telegram', {'chat_id': '654321'}, enabled=False)
        with self.db.connect() as conn:
            row = conn.execute('SELECT * FROM notification_channels').fetchone()
            opened = n._configuration(row, self.cfg)
        self.assertEqual(opened['bot_token'], TOKEN)
        self.assertEqual(opened['chat_id'], '654321')
        self.assertNotIn(TOKEN, json.dumps(n.list_inbox(self.db, 'one')))

    def test_corrupt_and_cross_user_config_fail_closed(self):
        self.save(); self.save(username='two')
        with self.db.transaction() as conn:
            cipher = conn.execute("SELECT config_cipher FROM notification_channels WHERE username='two'").fetchone()[0]
            conn.execute("UPDATE notification_channels SET config_cipher=? WHERE username='one'", (cipher,))
        self.event()
        with patch.object(n, '_send_http') as send:
            result = n.process_outbox(self.db, self.cfg)
            self.assertEqual(result['dead'], 1)
            send.assert_not_called()
        self.assertEqual(self.row()['last_error'], 'configuration_invalid')
        n.save_channel(self.db, self.cfg, 'one', 'telegram', {'bot_token': TOKEN, 'chat_id': '1'}, enabled=True)
        with self.db.transaction() as conn:
            conn.execute("UPDATE notification_channels SET config_cipher='dtsec1.tampered' WHERE username='one'")
        with self.assertRaises(HTTPException):
            n.set_channel_enabled(self.db, self.cfg, 'one', 'telegram', True)
        self.assertFalse(n.set_channel_enabled(self.db, self.cfg, 'one', 'telegram', False)['enabled'])

    def test_pending_cancelled_on_disable_config_change_and_delete(self):
        self.save(); self.event('first')
        n.set_channel_enabled(self.db, self.cfg, 'one', 'telegram', False)
        self.assertEqual(self.row()['state'], 'cancelled')
        n.set_channel_enabled(self.db, self.cfg, 'one', 'telegram', True)
        self.event('second')
        self.save()
        self.assertEqual(self.row()['state'], 'cancelled')
        self.event('third')
        n.delete_channel(self.db, 'one', 'telegram')
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM notification_outbox').fetchone()[0], 0)
        with patch.object(n, '_deliver') as send:
            n.process_outbox(self.db, self.cfg); send.assert_not_called()

    def test_manual_test_only_enqueues_selected_channel_and_is_bounded(self):
        self.save(); self.save('discord')
        with patch.object(n, '_deliver') as send:
            result = n.queue_test(self.db, 'one', 'telegram')
            send.assert_not_called()
        self.assertTrue(result['queued'])
        with self.db.connect() as conn:
            kinds = [row[0] for row in conn.execute('SELECT c.kind FROM notification_outbox o JOIN notification_channels c ON c.id=o.channel_id')]
        self.assertEqual(kinds, ['telegram'])
        with self.assertRaises(HTTPException) as err:
            n.queue_test(self.db, 'one', 'telegram')
        self.assertEqual(err.exception.status_code, 429)

    def test_worker_sends_once_after_acknowledgment_and_reclaims_expired_lease(self):
        self.save(); self.event()
        with self.db.transaction() as conn:
            conn.execute("UPDATE notification_outbox SET state='sending:old',attempts=1,claim_until=?,available_at=?", (self.now - 1, self.now - 60))
        with patch.object(n, '_deliver') as send:
            self.assertEqual(n.process_outbox(self.db, self.cfg, now=self.now)['sent'], 1)
            self.assertEqual(n.process_outbox(self.db, self.cfg, now=self.now + 1)['sent'], 0)
            self.assertEqual(send.call_count, 1)
        self.assertEqual(self.row()['attempts'], 2)
        self.assertEqual(self.row()['state'], 'sent')

    def test_each_delivery_lease_uses_current_tick_time(self):
        self.save(); self.event('slow-first'); self.event('slow-second')
        elapsed, leases = [0], []
        # Read the currently claimed row rather than the last queued row.
        def current_delivery(*_):
            with self.db.connect() as conn:
                leases.append(conn.execute("SELECT claim_until FROM notification_outbox WHERE state LIKE 'sending:%'").fetchone()[0])
            elapsed[0] += 130
        with patch.object(n.time, 'monotonic', side_effect=lambda: elapsed[0]), patch.object(n, '_deliver', side_effect=current_delivery):
            self.assertEqual(n.process_outbox(self.db, self.cfg, now=self.now)['sent'], 2)
        self.assertEqual(leases, [self.now + n.LEASE_SECONDS, self.now + 130 + n.LEASE_SECONDS])

    def test_nonexpired_lease_not_claimed_and_stale_owner_is_fenced(self):
        self.save(); self.event()
        with self.db.transaction() as conn:
            conn.execute("UPDATE notification_outbox SET state='sending:other',attempts=1,claim_until=?", (self.now + 10,))
        with patch.object(n, '_deliver') as send:
            self.assertEqual(n.process_outbox(self.db, self.cfg, now=self.now)['claimed'], 0)
            send.assert_not_called()
        def replace_claim(*_):
            with self.db.transaction() as conn:
                conn.execute("UPDATE notification_outbox SET state='sending:new-owner',claim_until=?", (self.now + 200,))
        with patch.object(n, '_deliver', side_effect=replace_claim):
            self.assertEqual(n.process_outbox(self.db, self.cfg, now=self.now + 11)['sent'], 0)
        self.assertEqual(self.row()['state'], 'sending:new-owner')

    def test_retry_backoff_five_attempt_limit_and_expired_exhaustion(self):
        self.save(); self.event()
        with patch.object(n, '_deliver', side_effect=n._DeliveryFailure('network_error', retryable=True)) as send:
            for attempt in range(1, 6):
                tick = self.now if attempt == 1 else self.row()['available_at']
                result = n.process_outbox(self.db, self.cfg, now=tick)
                self.assertEqual(self.row()['attempts'], attempt)
                self.assertEqual(self.row()['state'], 'retry' if attempt < 5 else 'dead')
                if attempt < 5:
                    self.assertEqual(self.row()['available_at'] - tick, 30 * 2 ** (attempt - 1))
                    self.assertEqual(n.process_outbox(self.db, self.cfg, now=tick + 1)['claimed'], 0)
            self.assertEqual(send.call_count, 5)
        self.event('exhausted')
        with self.db.transaction() as conn:
            conn.execute("UPDATE notification_outbox SET state='sending:crashed',attempts=5,claim_until=? WHERE state='queued'", (self.now - 1,))
        with patch.object(n, '_deliver') as send:
            n.process_outbox(self.db, self.cfg, now=self.now); send.assert_not_called()
        self.assertEqual(self.row()['state'], 'dead')

    def test_durable_channel_rate_limit_and_tick_bound(self):
        self.save()
        for index in range(10): self.event(str(index))
        with patch.object(n, '_deliver') as send:
            self.assertEqual(n.process_outbox(self.db, self.cfg, now=self.now)['sent'], n.CHANNEL_RATE_PER_MINUTE)
            self.assertEqual(n.process_outbox(self.db, self.cfg, now=self.now)['claimed'], 0)
            self.assertEqual(n.process_outbox(self.db, self.cfg, now=self.now + 61)['sent'], n.CHANNEL_RATE_PER_MINUTE)
            self.assertEqual(send.call_count, 6)

    def test_global_rate_limit_and_worker_tick_bound(self):
        self.save(); self.save('discord')
        for index in range(6): self.event('bounded-' + str(index))
        with self.db.transaction() as conn:
            conn.executemany('INSERT INTO domain_events(id,kind,dedupe_key,occurred_at) VALUES(?,?,?,?)', [('rate-' + str(index), 'notification_attempt', 'global-rate-' + str(index), self.now) for index in range(n.RATE_PER_MINUTE)])
        with patch.object(n, '_deliver') as send:
            self.assertEqual(n.process_outbox(self.db, self.cfg, now=self.now)['claimed'], 0)
            send.assert_not_called()
            self.assertEqual(n.process_outbox(self.db, self.cfg, now=self.now + 61)['claimed'], n.MAX_TICK)
            self.assertEqual(send.call_count, n.MAX_TICK)

    def test_credential_urls_are_redacted_from_httpx_info_logs(self):
        self.save(); self.event()
        with self.assertLogs('httpx', level='INFO') as logs:
            self.assertEqual(self.http_tick(lambda _: httpx.Response(200, stream=httpx.ByteStream(b'{"ok":true}')))['sent'], 1)
        self.assertNotIn(TOKEN, '\n'.join(logs.output))
        self.assertIn('[redacted]', '\n'.join(logs.output))
        self.save('discord'); self.event('discord-log')
        with self.assertLogs('httpx', level='INFO') as logs:
            self.http_tick(lambda _: httpx.Response(204, stream=httpx.ByteStream(b'')))
        self.assertNotIn(WEBHOOK, '\n'.join(logs.output))

    def http_tick(self, handler):
        client = httpx.Client
        transport = httpx.MockTransport(handler)
        with patch.object(n, '_public_addresses', return_value=['93.184.216.34']), patch.object(n.httpx, 'Client', side_effect=lambda **kwargs: client(transport=transport, **kwargs)) as factory:
            result = n.process_outbox(self.db, self.cfg, now=self.now)
            self.assertFalse(factory.call_args.kwargs['trust_env'])
            self.assertFalse(factory.call_args.kwargs['follow_redirects'])
            return result

    def test_http_delivery_fixed_endpoint_and_no_mentions(self):
        self.save('discord'); self.event()
        seen = []
        def handler(request):
            seen.append(request)
            self.assertEqual(str(request.url), WEBHOOK)
            self.assertEqual(json.loads(request.content)['allowed_mentions'], {'parse': []})
            return httpx.Response(204, stream=httpx.ByteStream(b''))
        self.assertEqual(self.http_tick(handler)['sent'], 1)
        self.assertEqual(len(seen), 1)

    def test_telegram_success_and_bounded_response(self):
        self.save(); self.event()
        def handler(request):
            self.assertEqual(request.url.host, 'api.telegram.org')
            self.assertNotIn('parse_mode', json.loads(request.content))
            return httpx.Response(200, stream=httpx.ByteStream(b'{"ok":true}'))
        self.assertEqual(self.http_tick(handler)['sent'], 1)
        self.event('too-large')
        self.assertEqual(self.http_tick(lambda _: httpx.Response(200, stream=httpx.ByteStream(b'x' * (n.MAX_RESPONSE + 1))))['dead'], 1)
        self.assertEqual(self.row()['last_error'], 'response_invalid')

    def test_provider_errors_are_constant_retryable_and_redirects_not_followed(self):
        self.save(); self.event()
        result = self.http_tick(lambda _: httpx.Response(429, headers={'Retry-After': '999999'}, stream=httpx.ByteStream(TOKEN.encode())))
        self.assertEqual(result['retried'], 1)
        self.assertEqual(self.row()['available_at'], self.now + 3600)
        self.assertEqual(self.row()['last_error'], 'rate_limited')
        self.event('redirect')
        self.assertEqual(self.http_tick(lambda _: httpx.Response(302, headers={'Location': 'http://127.0.0.1/private'}, stream=httpx.ByteStream(b'')))['dead'], 1)
        self.assertNotIn(TOKEN, json.dumps(self.row()))

    def test_smtp_syntax_and_dns_reject_private_targets(self):
        for change in ({'host': '127.0.0.1'}, {'host': 'localhost'}, {'host': 'smtp.local'}, {'port': 25}, {'tls_mode': 'plaintext'}, {'from_address': 'a@example.com\r\nBcc: b@example.com'}):
            with self.assertRaises(ValueError):
                n.validate_channel('smtp', {**SMTP, **change})
        with patch.object(socket, 'getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 587))]), patch.object(socket, 'create_connection') as connect:
            with self.assertRaises(n._DeliveryFailure) as error:
                n._send_smtp(SMTP, 'Title', 'Message')
            self.assertEqual(error.exception.code, 'target_blocked'); connect.assert_not_called()
        with self.assertRaises(n._DeliveryFailure):
            n._peer(SimpleNamespace(getpeername=lambda: ('10.0.0.1', 587)), ['93.184.216.34'])

    def test_smtp_peer_and_tls_checked_before_auth(self):
        smtp = MagicMock(); smtp.__enter__.return_value = smtp
        smtp.sock = MagicMock(spec=ssl.SSLSocket)
        smtp.sock.getpeername.return_value = ('10.0.0.2', 587)
        with patch.object(n, '_public_addresses', return_value=['93.184.216.34']), patch.object(n, '_PinnedSMTP', return_value=smtp):
            with self.assertRaises(n._DeliveryFailure):
                n._send_smtp(SMTP, 'Title', 'Message')
        smtp.login.assert_not_called(); smtp.send_message.assert_not_called()
        smtp.sock.getpeername.return_value = ('93.184.216.34', 587)
        smtp.has_extn.return_value = False
        with patch.object(n, '_public_addresses', return_value=['93.184.216.34']), patch.object(n, '_PinnedSMTP', return_value=smtp):
            with self.assertRaises(n._DeliveryFailure):
                n._send_smtp(SMTP, 'Title', 'Message')
        smtp.login.assert_not_called()

    def test_smtp_success_requires_verified_tls_and_public_pinned_connection(self):
        smtp = MagicMock(); smtp.__enter__.return_value = smtp
        smtp.sock = MagicMock(spec=ssl.SSLSocket); smtp.sock.getpeername.return_value = ('93.184.216.34', 587)
        smtp.send_message.return_value = {}
        with patch.object(n, '_public_addresses', return_value=['93.184.216.34']), patch.object(n, '_PinnedSMTP', return_value=smtp):
            n._send_smtp(SMTP, 'Title', 'Message')
        context = smtp.starttls.call_args.kwargs['context']
        self.assertTrue(context.check_hostname); self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        smtp.login.assert_called_once_with('mailer', 'private-password')
        self.assertLess([call[0] for call in smtp.method_calls].index('starttls'), [call[0] for call in smtp.method_calls].index('login'))
        mailer = object.__new__(n._PinnedSMTP)
        mailer._public_ips = ['93.184.216.34']
        sock = MagicMock(); sock.getpeername.return_value = ('93.184.216.34', 587)
        with patch.object(socket, 'create_connection', return_value=sock) as connect:
            mailer._get_socket('smtp.example.com', 587, 5)
        self.assertEqual(connect.call_args.args[0], ('93.184.216.34', 587))

    def test_smtp_reply_body_and_multiline_are_bounded(self):
        mailer = object.__new__(n._PinnedSMTP)
        mailer.sock = MagicMock()
        mailer._deadline = time.monotonic() + 10
        mailer.file = io.BytesIO(b'250-' + b'x' * 4096 + b'\r\n')
        with self.assertRaises(n._DeliveryFailure): mailer.getreply()
        mailer.file = io.BytesIO(b'250-hello\r\n' * 21)
        with self.assertRaises(n._DeliveryFailure): mailer.getreply()

    def test_retention_bound_and_safe_text(self):
        with self.db.transaction() as conn:
            conn.executemany('INSERT INTO notifications(id,username,event_key,kind,title,message,created_at) VALUES(?,?,?,?,?,?,?)', [(str(i), 'one', str(i), 'example', 'Old', 'Message', self.now) for i in range(510)])
            conn.execute("INSERT INTO notifications(id,username,event_key,kind,title,message,created_at) VALUES('expired','two','expired','example','Expired','Message',?)", (self.now - 91 * 86400,))
        n.notify(self.db, 'one', 'example', 'Safe', 'Failure at http://127.0.0.1/private?token=secret password=hidden', 'retention')
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications WHERE username='one'").fetchone()[0], n.MAX_INBOX)
            self.assertIsNone(conn.execute("SELECT id FROM notifications WHERE id='expired'").fetchone())
        text = json.dumps(n.list_inbox(self.db, 'one'))
        self.assertNotIn('127.0.0.1', text); self.assertNotIn('hidden', text)

    def app(self):
        app = FastAPI(); app.state.db = self.db; app.state.config = self.cfg
        app.include_router(notification_routes.router)
        return app

    def test_routes_require_auth_and_proof_and_do_not_echo_secrets(self):
        app = self.app()
        client = TestClient(app)
        self.assertEqual(client.get('/api/notifications').status_code, 401)
        self.assertEqual(client.post('/api/notifications/channels/telegram', json={'password': 'correct-password', 'config': {'bot_token': TOKEN, 'chat_id': '1'}}).status_code, 401)
        token = make_session(self.db, 'one')
        client.cookies.set('dt_session', token)
        self.assertEqual(client.get('/api/notifications').status_code, 200)
        denied = client.post('/api/notifications/channels/telegram', json={'password': 'bad', 'config': {'bot_token': TOKEN, 'chat_id': '1'}, 'enabled': True})
        self.assertEqual(denied.status_code, 400); self.assertNotIn(TOKEN, denied.text)
        saved = client.post('/api/notifications/channels/telegram', json={'password': 'correct-password', 'config': {'bot_token': TOKEN, 'chat_id': '1'}})
        self.assertEqual(saved.status_code, 200); self.assertFalse(saved.json()['enabled'])
        bad = client.post('/api/notifications/channels/telegram', json={'password': 'correct-password', 'config': {'bot_token': TOKEN, 'chat_id': 'bad-private-token'}})
        self.assertEqual(bad.status_code, 400); self.assertNotIn(TOKEN, bad.text); self.assertNotIn('bad-private-token', bad.text)
        oversized = client.post('/api/notifications/channels/telegram', content=json.dumps({'password': TOKEN * 1000}), headers={'Content-Type': 'application/json'})
        self.assertEqual(oversized.status_code, 413); self.assertNotIn(TOKEN, oversized.text)
        with self.db.connect() as conn:
            audits = conn.execute("SELECT details FROM security_events WHERE event_type='notification_channel_saved'").fetchall()
        self.assertEqual(len(audits), 1); self.assertNotIn(TOKEN, audits[0]['details'])

    def test_one_time_recovery_proof_can_enable_but_cannot_be_replayed(self):
        self.save(enabled=False)
        with self.db.transaction() as conn:
            conn.execute('INSERT INTO mfa_credentials(username,secret_cipher,enabled,last_counter) VALUES(?,?,1,-1)', ('one', seal_secret('JBSWY3DPEHPK3PXP', self.cfg.secret, 'mfa:one')))
            conn.execute('INSERT INTO recovery_codes(username,code_hash) VALUES(?,?)', ('one', _recovery_hash(self.cfg.secret, 'one', 'ABCDE-F0123')))
        client = TestClient(self.app())
        client.cookies.set('dt_session', make_session(self.db, 'one', mfa_verified=True))
        payload = {'password': 'correct-password', 'recovery_code': 'ABCDE-F0123', 'enabled': True}
        self.assertEqual(client.post('/api/notifications/channels/telegram/enabled', json=payload).status_code, 200)
        payload['enabled'] = False
        self.assertEqual(client.post('/api/notifications/channels/telegram/enabled', json=payload).status_code, 400)
        self.assertTrue(n.channel_status(self.db, 'one')['items'][0]['enabled'])

    def test_routes_require_fresh_mfa_proof_for_channel_change(self):
        app = self.app()
        secret = 'JBSWY3DPEHPK3PXP'
        with self.db.transaction() as conn:
            conn.execute('INSERT INTO mfa_credentials(username,secret_cipher,enabled,last_counter) VALUES(?,?,1,-1)', ('one', seal_secret(secret, self.cfg.secret, 'mfa:one')))
        client = TestClient(app)
        client.cookies.set('dt_session', make_session(self.db, 'one', mfa_verified=True))
        base = {'password': 'correct-password', 'config': {'bot_token': TOKEN, 'chat_id': '1'}, 'enabled': True}
        self.assertEqual(client.post('/api/notifications/channels/telegram', json=base).status_code, 400)
        body = {**base, 'code': totp(secret, int(time.time()) // 30)}
        self.assertEqual(client.post('/api/notifications/channels/telegram', json=body).status_code, 200)
        self.assertEqual(client.post('/api/notifications/channels/telegram/test', json={'password': 'correct-password', 'code': body['code']}).status_code, 400)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM notification_outbox').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
