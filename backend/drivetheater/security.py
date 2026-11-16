"""Password + TOTP/recovery primitives. No authenticated session before MFA proof."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

from fastapi import HTTPException

from .access import device_label, network_marker, security_event
from .auth import make_session, verify_password


def totp(secret: str, counter: int, digits: int = 6) -> str:
    key = base64.b32decode(secret.upper() + '=' * ((-len(secret)) % 8))
    digest = hmac.new(key, struct.pack('>Q', counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    number = (struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7fffffff) % (10 ** digits)
    return str(number).zfill(digits)


def match_counter(secret: str, code: str, *, now: float | None = None, last: int = -1) -> int | None:
    if not isinstance(code, str) or len(code) != 6 or not code.isascii() or not code.isdigit():
        return None
    current = int((time.time() if now is None else now) // 30)
    for counter in (current, current - 1, current + 1):
        if counter >= 0 and counter > last and hmac.compare_digest(totp(secret, counter), code):
            return counter
    return None


def mfa_enabled(db, username: str) -> bool:
    with db.connect() as conn:
        row = conn.execute('SELECT enabled FROM mfa_credentials WHERE username=?', (username,)).fetchone()
    return bool(row and row['enabled'])


def _recovery_hash(master: str, username: str, value: str) -> str:
    normalized = value.replace('-', '').replace(' ', '').upper()
    return hmac.new(master.encode(), ('recovery:' + username + ':' + normalized).encode(), hashlib.sha256).hexdigest()


def _new_recovery(conn, username: str, master: str) -> list[str]:
    codes = [secrets.token_hex(5).upper() for _ in range(10)]
    conn.execute('DELETE FROM recovery_codes WHERE username=?', (username,))
    conn.executemany('INSERT INTO recovery_codes(username,code_hash) VALUES(?,?)',
                     [(username, _recovery_hash(master, username, code)) for code in codes])
    return [code[:5] + '-' + code[5:] for code in codes]


def verify_factor(conn, username: str, master: str, code: str | None = None, recovery: str | None = None) -> bool:
    from .crypto_utils import open_secret
    row = conn.execute('SELECT * FROM mfa_credentials WHERE username=?', (username,)).fetchone()
    if not row or not row['enabled']:
        return True
    if code:
        secret = open_secret(row['secret_cipher'], master, 'mfa:' + username)
        matched = match_counter(secret, code, last=row['last_counter'])
        if matched is not None:
            conn.execute('UPDATE mfa_credentials SET last_counter=? WHERE username=?', (matched, username))
            return True
    if recovery and len(recovery) <= 64:
        digest = _recovery_hash(master, username, recovery)
        changed = conn.execute('UPDATE recovery_codes SET consumed_at=? WHERE username=? AND code_hash=? AND consumed_at IS NULL', (int(time.time()), username, digest)).rowcount
        return bool(changed)
    return False


def factor_limit(db, username: str) -> None:
    with db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM security_events WHERE username=? AND event_type IN ('factor_failure','sensitive_failure') AND occurred_at>?", (username, int(time.time()) - 60)).fetchone()[0]
    if count >= 5:
        raise HTTPException(429, 'Too many verification attempts; try again later', headers={'Retry-After': '60'})


def verify_sensitive(request, username: str, password: str, code: str | None = None, recovery: str | None = None) -> None:
    db, cfg = request.app.state.db, request.app.state.config
    factor_limit(db, username)
    with db.connect() as conn:
        row = conn.execute('SELECT password_hash FROM users WHERE username=?', (username,)).fetchone()
    valid = bool(row and verify_password(password, row['password_hash']))
    if valid:
        with db.transaction() as conn:
            valid = verify_factor(conn, username, cfg.secret, code, recovery)
    if not valid:
        security_event(db, username, 'sensitive_failure')
        raise HTTPException(400, 'Current password and an unused MFA/recovery code are required')


def begin_enrollment(request, username: str) -> dict:
    from .crypto_utils import seal_secret
    db, cfg = request.app.state.db, request.app.state.config
    if mfa_enabled(db, username):
        raise HTTPException(409, 'MFA is already enabled')
    secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')
    encrypted = seal_secret(secret, cfg.secret, 'mfa:' + username)
    with db.transaction() as conn:
        conn.execute('INSERT INTO mfa_credentials(username,pending_cipher,pending_until) VALUES(?,?,?) ON CONFLICT(username) DO UPDATE SET pending_cipher=excluded.pending_cipher,pending_until=excluded.pending_until', (username, encrypted, int(time.time()) + 600))
    security_event(db, username, 'mfa_enrollment_started')
    uri = 'otpauth://totp/' + quote('DriveTheater:' + username, safe='') + '?' + urlencode({'secret': secret, 'issuer': 'DriveTheater', 'algorithm': 'SHA1', 'digits': 6, 'period': 30})
    return {'secret': secret, 'provisioning_uri': uri, 'expires_in': 600}


def confirm_enrollment(request, principal, code: str) -> list[str]:
    from .crypto_utils import open_secret
    db, cfg = request.app.state.db, request.app.state.config
    factor_limit(db, principal.username)
    result = None
    with db.transaction() as conn:
        row = conn.execute('SELECT * FROM mfa_credentials WHERE username=?', (principal.username,)).fetchone()
        if row and not row['enabled'] and row['pending_cipher'] and row['pending_until'] > time.time():
            secret = open_secret(row['pending_cipher'], cfg.secret, 'mfa:' + principal.username)
            counter = match_counter(secret, code)
            if counter is not None:
                result = _new_recovery(conn, principal.username, cfg.secret)
                conn.execute('UPDATE mfa_credentials SET secret_cipher=pending_cipher,pending_cipher=NULL,pending_until=0,enabled=1,last_counter=? WHERE username=?', (counter, principal.username))
                conn.execute('DELETE FROM sessions WHERE username=? AND token_hash!=?', (principal.username, principal.token_hash))
                conn.execute('UPDATE sessions SET mfa_verified=1 WHERE token_hash=?', (principal.token_hash,))
    if result is None:
        security_event(db, principal.username, 'factor_failure')
        raise HTTPException(400, 'Invalid or expired enrollment code')
    security_event(db, principal.username, 'mfa_enabled', count=len(result))
    return result


def begin_challenge(request, username: str) -> str:
    db = request.app.state.db
    factor_limit(db, username)
    challenge, now = secrets.token_urlsafe(32), int(time.time())
    with db.transaction() as conn:
        conn.execute('DELETE FROM auth_challenges WHERE expires_at<?', (now,))
        conn.execute('DELETE FROM auth_challenges WHERE username=?', (username,))
        conn.execute('INSERT INTO auth_challenges(id,username,peer_hash,expires_at) VALUES(?,?,?,?)', (hashlib.sha256(challenge.encode()).hexdigest(), username, network_marker(request), now + 300))
    return challenge


def complete_challenge(request, challenge: str, code: str | None, recovery: str | None) -> str:
    db, cfg = request.app.state.db, request.app.state.config
    digest = hashlib.sha256(challenge.encode()).hexdigest()
    with db.connect() as conn:
        initial = conn.execute('SELECT username FROM auth_challenges WHERE id=?', (digest,)).fetchone()
    if not initial:
        raise HTTPException(401, 'Invalid or expired authentication challenge')
    username = initial['username']
    factor_limit(db, username)
    accepted = False
    with db.transaction() as conn:
        row = conn.execute('SELECT * FROM auth_challenges WHERE id=?', (digest,)).fetchone()
        if row and row['expires_at'] > time.time() and row['attempts'] < 5 and hmac.compare_digest(row['peer_hash'], network_marker(request)):
            accepted = verify_factor(conn, username, cfg.secret, code, recovery)
            if accepted:
                conn.execute('DELETE FROM auth_challenges WHERE id=?', (digest,))
            else:
                conn.execute('UPDATE auth_challenges SET attempts=attempts+1 WHERE id=?', (digest,))
    if not accepted:
        security_event(db, username, 'factor_failure')
        raise HTTPException(401, 'Invalid or already used authentication code')
    security_event(db, username, 'mfa_login', method='recovery' if recovery else 'totp')
    return username


def issue_session(request, username: str, *, verified: bool = False) -> str:
    marker, db = network_marker(request), request.app.state.db
    with db.connect() as conn:
        known = bool(conn.execute('SELECT 1 FROM sessions WHERE username=? AND network_hash=?', (username, marker)).fetchone())
    token = make_session(db, username, device=device_label(request), network_hash=marker, mfa_verified=verified)
    security_event(db, username, 'session_created', method='mfa' if verified else 'password')
    if not known:
        security_event(db, username, 'new_network_login')
        # Optional notification integration never sends directly in this request.
        callback = getattr(request.app.state, 'notify', None)
        if callback:
            callback(username, 'new_login', 'New network sign-in', 'A sign-in from a new network was recorded.', 'login:' + hashlib.sha256(token.encode()).hexdigest())
    return token


def rotate_recovery(request, username: str) -> list[str]:
    with request.app.state.db.transaction() as conn:
        codes = _new_recovery(conn, username, request.app.state.config.secret)
    security_event(request.app.state.db, username, 'recovery_codes_rotated', count=len(codes))
    return codes
