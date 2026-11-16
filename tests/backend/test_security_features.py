from __future__ import annotations

import atexit
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))
_import_runtime = tempfile.TemporaryDirectory(prefix='dt-security-import-')
atexit.register(_import_runtime.cleanup)
with patch.dict(os.environ, {'DT_DATA_DIR': _import_runtime.name, 'DT_ADMIN_PASSWORD': 'synthetic-import-password', 'DT_SECRET': 's' * 48}):
    from drivetheater.main import create_app
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request
from drivetheater.config import Config
from drivetheater.access import client_address, enforce_network, require_principal, validate_policy
from drivetheater.security import match_counter, totp


class SecurityFeatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dt-security-')
        self.addCleanup(self.tmp.cleanup)
        self.password = 'correct-synthetic-password'
        self.app = create_app(Config(Path(self.tmp.name), 'admin', self.password, 's' * 48, False, True, None))
        self.client = TestClient(self.app)
        self.assertEqual(self.client.post('/api/auth/login', json={'username': 'admin', 'password': self.password}).status_code, 200)

    def begin(self):
        result = self.client.post('/api/security/mfa/begin', json={'password': self.password})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()['secret']

    def enroll(self):
        secret = self.begin()
        counter = int(time.time() // 30)
        result = self.client.post('/api/security/mfa/confirm', json={'password': self.password, 'code': totp(secret, counter)})
        self.assertEqual(result.status_code, 200, result.text)
        return secret, counter, result.json()['recovery_codes']

    def request(self, peer, forwarded='', asserted=''):
        headers = [(b'host', b'testserver')]
        if forwarded: headers.append((b'x-forwarded-for', forwarded.encode()))
        if asserted: headers.append((b'x-drivetheater-user', asserted.encode()))
        return Request({'type': 'http', 'method': 'GET', 'scheme': 'http', 'path': '/api/videos', 'query_string': b'', 'headers': headers, 'client': (peer, 1234), 'server': ('testserver', 80), 'app': self.app})

    def set_policy(self, value):
        with self.app.state.db.transaction() as conn:
            conn.execute("INSERT INTO security_policies(key,value) VALUES('network',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(value),))

    def test_totp_rfc6238_sha1_vectors_and_replay(self):
        secret = base64.b32encode(b'12345678901234567890').decode().rstrip('=')
        vectors = [(59, '94287082'), (1111111109, '07081804'), (1111111111, '14050471'), (1234567890, '89005924'), (2000000000, '69279037'), (20000000000, '65353130')]
        for timestamp, expected in vectors:
            self.assertEqual(totp(secret, timestamp // 30, digits=8), expected)
        code = totp(secret, 10)
        self.assertEqual(match_counter(secret, code, now=300), 10)
        self.assertIsNone(match_counter(secret, code, now=300, last=10))
        self.assertIsNone(match_counter(secret, '１２３４５６', now=300))

    def test_mfa_secret_is_encrypted_and_confirmation_is_required(self):
        secret = self.begin()
        self.assertFalse(self.client.get('/api/security').json()['mfa_enabled'])
        with self.app.state.db.connect() as conn:
            row = conn.execute('SELECT * FROM mfa_credentials').fetchone()
            self.assertNotIn(secret, row['pending_cipher'])
            self.assertIsNone(row['secret_cipher'])
        bad = self.client.post('/api/security/mfa/confirm', json={'password': self.password, 'code': 'nototp'})
        self.assertEqual(bad.status_code, 400)
        self.assertFalse(self.client.get('/api/security').json()['mfa_enabled'])

    def test_expired_enrollment_does_not_enable_mfa(self):
        secret = self.begin()
        with self.app.state.db.transaction() as conn:
            conn.execute('UPDATE mfa_credentials SET pending_until=0')
        result = self.client.post('/api/security/mfa/confirm', json={'password': self.password, 'code': totp(secret, int(time.time() // 30))})
        self.assertEqual(result.status_code, 400)

    def test_mfa_password_alone_creates_no_authenticated_session(self):
        secret, counter, _ = self.enroll()
        self.client.post('/api/auth/logout')
        result = self.client.post('/api/auth/login', json={'username': 'admin', 'password': self.password})
        self.assertEqual(result.status_code, 202)
        self.assertNotIn('dt_session', result.cookies)
        self.assertEqual(self.client.get('/api/videos').status_code, 401)
        proof = self.client.post('/api/auth/mfa', json={'challenge': result.json()['challenge'], 'code': totp(secret, counter + 1)})
        self.assertEqual(proof.status_code, 200, proof.text)
        self.assertEqual(self.client.get('/api/videos').status_code, 200)
        self.assertEqual(self.client.post('/api/auth/mfa', json={'challenge': result.json()['challenge'], 'code': totp(secret, counter + 1)}).status_code, 401)

    def test_recovery_code_is_one_use_and_plaintext_is_not_stored(self):
        _, _, codes = self.enroll()
        with self.app.state.db.connect() as conn:
            stored = [row[0] for row in conn.execute('SELECT code_hash FROM recovery_codes')]
        self.assertTrue(all(codes[0] not in value for value in stored))
        self.client.post('/api/auth/logout')
        for expected in (200, 401):
            login = self.client.post('/api/auth/login', json={'username': 'admin', 'password': self.password}).json()
            result = self.client.post('/api/auth/mfa', json={'challenge': login['challenge'], 'recovery_code': codes[0]})
            self.assertEqual(result.status_code, expected, result.text)
            if expected == 200: self.client.post('/api/auth/logout')

    def test_factor_failures_are_persistently_rate_limited(self):
        self.enroll(); self.client.post('/api/auth/logout')
        challenge = self.client.post('/api/auth/login', json={'username': 'admin', 'password': self.password}).json()['challenge']
        for _ in range(5):
            self.assertEqual(self.client.post('/api/auth/mfa', json={'challenge': challenge, 'code': 'wrong'}).status_code, 401)
        self.assertEqual(self.client.post('/api/auth/mfa', json={'challenge': challenge, 'code': 'wrong'}).status_code, 429)

    def test_session_listing_hides_tokens_and_revocation_is_immediate(self):
        token = self.client.cookies.get('dt_session')
        data = self.client.get('/api/security/sessions').json()
        self.assertNotIn(token, json.dumps(data))
        self.assertNotIn('token_hash', json.dumps(data))
        self.assertNotIn('network_hash', json.dumps(data))
        current = next(item for item in data['items'] if item['current'])
        denied = self.client.post('/api/security/sessions/revoke', json={'password': 'wrong', 'session_id': current['id']})
        self.assertEqual(denied.status_code, 400)
        result = self.client.post('/api/security/sessions/revoke', json={'password': self.password, 'session_id': current['id']})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.client.get('/api/discover/config').status_code, 401)
        self.assertEqual(self.client.get('/api/notifications').status_code, 401)

    def test_password_change_invalidates_pending_mfa_challenges(self):
        _, _, codes = self.enroll()
        with TestClient(self.app) as pending:
            login = pending.post('/api/auth/login', json={'username': 'admin', 'password': self.password})
            self.assertEqual(login.status_code, 202)
            changed = self.client.post('/api/auth/password', json={'current_password': self.password, 'new_password': 'different-synthetic-password', 'recovery_code': codes[0]})
            self.assertEqual(changed.status_code, 200, changed.text)
            self.assertEqual(pending.post('/api/auth/mfa', json={'challenge': login.json()['challenge'], 'recovery_code': codes[1]}).status_code, 401)
            self.assertEqual(self.client.get('/api/security').status_code, 401)

    def test_history_clear_does_not_erase_security_events(self):
        before = self.client.get('/api/security/events').json()['items']
        self.assertTrue(before)
        self.client.post('/api/activity/clear')
        self.assertEqual(self.client.get('/api/security/events').json()['items'], before)

    def test_network_preview_refuses_lockout_and_is_one_use(self):
        blocked = self.client.post('/api/security/network/preview', json={'policy': {'allowlist_enabled': True, 'allowed_cidrs': ['203.0.113.0/24']}}).json()
        self.assertFalse(blocked['current_network_permitted'])
        self.assertEqual(self.client.post('/api/security/network/apply', json={'password': self.password, 'confirmation': blocked['confirmation']}).status_code, 409)
        safe = self.client.post('/api/security/network/preview', json={'policy': {'allowlist_enabled': False}}).json()
        body = {'password': self.password, 'confirmation': safe['confirmation']}
        self.assertEqual(self.client.post('/api/security/network/apply', json=body).status_code, 200)
        self.assertEqual(self.client.post('/api/security/network/apply', json=body).status_code, 409)

    def test_untrusted_forwarded_headers_never_grant_identity(self):
        self.set_policy(validate_policy({'allowlist_enabled': True, 'allowed_cidrs': ['203.0.113.0/24'], 'trusted_proxy_cidrs': ['127.0.0.1/32']}))
        forged = self.request('198.51.100.10', '203.0.113.5', 'admin')
        self.assertEqual(client_address(forged), '198.51.100.10')
        with self.assertRaises(HTTPException) as denied: enforce_network(forged)
        self.assertEqual(denied.exception.status_code, 403)
        trusted = self.request('127.0.0.1', '198.51.100.9, 203.0.113.5', 'admin')
        self.assertEqual(client_address(trusted), '203.0.113.5')
        enforce_network(trusted)
        with self.assertRaises(HTTPException) as no_session: require_principal(trusted)
        self.assertEqual(no_session.exception.status_code, 401)

    def test_ipv6_and_invalid_policy(self):
        value = validate_policy({'allowlist_enabled': True, 'allowed_cidrs': ['2001:db8::/32']})
        self.set_policy(value)
        enforce_network(self.request('2001:db8::123'))
        for invalid in ({'allowlist_enabled': True}, {'trusted_proxy_cidrs': ['not-a-cidr']}, {'proxy_gate_enabled': True}, {'allowlist_enabled': 'false'}):
            with self.assertRaises(ValueError): validate_policy(invalid)

    def test_unconfirmed_and_unprotected_requests_do_not_bypass_auth(self):
        with TestClient(self.app) as anonymous:
            for route in ('/api/security', '/api/security/sessions', '/api/security/events', '/api/discover/config'):
                self.assertEqual(anonymous.get(route).status_code, 401)
        self.assertEqual(self.client.post('/api/security/mfa/begin', json={'password': 'wrong'}).status_code, 400)
        self.assertEqual(self.client.get('/api/security').headers['cache-control'], 'private, no-store')


if __name__ == '__main__':
    unittest.main()
