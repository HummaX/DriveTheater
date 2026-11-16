"""One request identity/authentication policy for every authenticated API."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import ipaddress
import json
import re
import time

from fastapi import HTTPException, Request

from .auth import session_hash


@dataclass(frozen=True)
class Principal:
    username: str
    session_id: str
    token_hash: str
    mfa_verified: bool


DEFAULT_POLICY = {'allowlist_enabled': False, 'allowed_cidrs': [],
                  'trusted_proxy_cidrs': [], 'proxy_gate_enabled': False}


def policy(db) -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT value FROM security_policies WHERE key='network'").fetchone()
    if not row:
        return dict(DEFAULT_POLICY)
    try:
        return validate_policy(json.loads(row['value']))
    except (ValueError, TypeError):
        raise HTTPException(503, 'Network security policy is invalid; use local administrator recovery.') from None


def validate_policy(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) - set(DEFAULT_POLICY):
        raise ValueError('Unsupported network policy field')
    result = {**DEFAULT_POLICY, **value}
    for key in ('allowlist_enabled', 'proxy_gate_enabled'):
        if type(result[key]) is not bool:
            raise ValueError('Policy switches must be booleans')
    for key in ('allowed_cidrs', 'trusted_proxy_cidrs'):
        values = result[key]
        if not isinstance(values, list) or len(values) > 32:
            raise ValueError('Use at most 32 CIDRs')
        try:
            result[key] = [str(ipaddress.ip_network(item, strict=False)) for item in values if isinstance(item, str)]
        except ValueError:
            raise ValueError('Use valid IPv4 or IPv6 CIDRs') from None
        if len(result[key]) != len(values):
            raise ValueError('CIDRs must be strings')
    if result['allowlist_enabled'] and not result['allowed_cidrs']:
        raise ValueError('An enabled allowlist needs at least one permitted CIDR')
    if result['proxy_gate_enabled'] and not result['trusted_proxy_cidrs']:
        raise ValueError('Proxy authentication requires explicitly trusted proxy CIDRs')
    return result


def _within(address: str, cidrs: list[str]) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
        return any(parsed in ipaddress.ip_network(cidr) for cidr in cidrs)
    except ValueError:
        return False


def client_address(request: Request, settings: dict | None = None) -> str:
    settings = settings if settings is not None else policy(request.app.state.db)
    peer = request.client.host if request.client else 'unknown'
    trusted = settings['trusted_proxy_cidrs']
    if not _within(peer, trusted):
        return peer
    forwarded = request.headers.get('x-forwarded-for', '')
    if len(forwarded) > 2048:
        raise HTTPException(400, 'Invalid forwarded address chain')
    chain = [part.strip() for part in forwarded.split(',') if part.strip()]
    if len(chain) > 16:
        raise HTTPException(400, 'Invalid forwarded address chain')
    current = peer
    while chain and _within(current, trusted):
        current = chain.pop()
        try:
            ipaddress.ip_address(current)
        except ValueError:
            raise HTTPException(400, 'Invalid forwarded address chain') from None
    return current


def enforce_network(request: Request) -> None:
    settings = policy(request.app.state.db)
    peer = request.client.host if request.client else 'unknown'
    # Only local liveness/readiness probes remain reachable outside operator restrictions.
    if request.url.path in ('/api/health', '/api/ready') and _within(peer, ['127.0.0.0/8', '::1/128']):
        return
    address = client_address(request, settings)
    if settings['allowlist_enabled'] and not _within(address, settings['allowed_cidrs']):
        raise HTTPException(403, 'This network is not permitted')
    if settings['proxy_gate_enabled']:
        asserted = request.headers.get('x-drivetheater-user', '')
        if not _within(peer, settings['trusted_proxy_cidrs']) or not hmac.compare_digest(asserted.encode('utf-8'), request.app.state.config.admin_user.encode('utf-8')):
            raise HTTPException(403, 'Trusted proxy authentication is required')
    # A proxy assertion NEVER creates a session or satisfies an application MFA factor.


def network_marker(request: Request) -> str:
    address = client_address(request)
    try:
        parsed = ipaddress.ip_address(address)
        address = str(ipaddress.ip_network(f'{parsed}/{24 if parsed.version == 4 else 64}', strict=False))
    except ValueError:
        address = 'unknown-peer'
    return hmac.new(request.app.state.config.secret.encode(), ('login-network:' + address).encode(), hashlib.sha256).hexdigest()


def device_label(request: Request) -> str:
    agent = request.headers.get('user-agent', '')[:512]
    browser = next((label for needle, label in [('Edg/', 'Edge'), ('Firefox/', 'Firefox'), ('Chrome/', 'Chrome'), ('Safari/', 'Safari')] if needle in agent), 'Browser')
    system = next((label for needle, label in [('Android', 'Android'), ('iPhone', 'iOS'), ('iPad', 'iPadOS'), ('Windows', 'Windows'), ('Macintosh', 'macOS'), ('Linux', 'Linux')] if needle in agent), 'device')
    return f'{browser} on {system}'


def require_principal(request: Request) -> Principal:
    enforce_network(request)
    token = request.cookies.get('dt_session')
    if not token or len(token) > 256:
        raise HTTPException(401, 'authentication required')
    digest, now = session_hash(token), int(time.time())
    with request.app.state.db.connect() as conn:
        row = conn.execute('SELECT s.*,COALESCE(m.enabled,0) AS mfa_enabled FROM sessions s LEFT JOIN mfa_credentials m ON m.username=s.username WHERE token_hash=? AND expires_at>?', (digest, now)).fetchone()
        if not row or (row['mfa_enabled'] and not row['mfa_verified']):
            raise HTTPException(401, 'authentication required')
        if row['last_seen_at'] < now - 60:
            conn.execute('UPDATE sessions SET last_seen_at=? WHERE token_hash=?', (now, digest))
    return Principal(row['username'], row['id'], digest, bool(row['mfa_verified']))


def security_event(db, username: str | None, kind: str, **details) -> None:
    if not re.fullmatch(r'[a-z][a-z0-9_]{1,63}', kind):
        raise ValueError('Invalid security event')
    allowed = {'session_id', 'method', 'action', 'result', 'code', 'count', 'channel', 'enabled'}
    safe = {key: value for key, value in details.items() if key in allowed and
            (isinstance(value, (bool, int)) or isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_ -]{0,96}', value))}
    now = int(time.time())
    with db.transaction() as conn:
        conn.execute('INSERT INTO security_events(occurred_at,username,event_type,details) VALUES(?,?,?,?)', (now, username, kind, json.dumps(safe)))
        conn.execute('DELETE FROM security_events WHERE occurred_at<?', (now - 90 * 86400,))
        conn.execute('DELETE FROM security_events WHERE id NOT IN (SELECT id FROM security_events ORDER BY id DESC LIMIT 10000)')
