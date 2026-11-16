"""Opt-in administrator controls, with recent password/factor proof for mutations."""
from __future__ import annotations

import importlib.util
import json
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .access import (Principal, client_address, enforce_network, policy, require_principal,
                     security_event, validate_policy, _within)
from .security import (begin_enrollment, complete_challenge, confirm_enrollment,
                       issue_session, mfa_enabled, rotate_recovery, verify_sensitive)

router = APIRouter()


class Proof(BaseModel):
    password: str = Field(min_length=1, max_length=1024)
    code: str | None = Field(default=None, max_length=8)
    recovery_code: str | None = Field(default=None, max_length=64)


class Challenge(BaseModel):
    challenge: str = Field(min_length=20, max_length=128)
    code: str | None = Field(default=None, max_length=8)
    recovery_code: str | None = Field(default=None, max_length=64)


class Revoke(Proof):
    session_id: str = Field(max_length=64)


class NetworkPreview(BaseModel):
    policy: dict


class NetworkApply(Proof):
    confirmation: str = Field(min_length=20, max_length=64)


def _proof(request, principal, payload: Proof):
    verify_sensitive(request, principal.username, payload.password, payload.code, payload.recovery_code)


@router.post('/api/auth/mfa')
def mfa_login(payload: Challenge, request: Request, response: Response):
    enforce_network(request)
    username = complete_challenge(request, payload.challenge, payload.code, payload.recovery_code)
    token = issue_session(request, username, verified=True)
    response.set_cookie('dt_session', token, httponly=True, samesite='lax', secure=request.app.state.config.cookie_secure, max_age=604800)
    return {'username': username}


@router.get('/api/security')
def status(request: Request, user: Principal = Depends(require_principal)):
    with request.app.state.db.connect() as conn:
        codes = conn.execute('SELECT COUNT(*) FROM recovery_codes WHERE username=? AND consumed_at IS NULL', (user.username,)).fetchone()[0]
    return {'mfa_enabled': mfa_enabled(request.app.state.db, user.username),
            'recovery_codes_remaining': codes,
            'crypto_available': importlib.util.find_spec('cryptography') is not None,
            'network_policy': policy(request.app.state.db)}


@router.post('/api/security/mfa/begin')
def begin(payload: Proof, request: Request, user: Principal = Depends(require_principal)):
    _proof(request, user, payload)
    return begin_enrollment(request, user.username)


@router.post('/api/security/mfa/confirm')
def confirm(payload: Proof, request: Request, user: Principal = Depends(require_principal)):
    _proof(request, user, payload)
    return {'recovery_codes': confirm_enrollment(request, user, payload.code or ''),
            'notice': 'Store these one-time recovery codes privately; they are shown only now.'}


@router.post('/api/security/mfa/disable')
def disable(payload: Proof, request: Request, response: Response, user: Principal = Depends(require_principal)):
    _proof(request, user, payload)
    with request.app.state.db.transaction() as conn:
        conn.execute('DELETE FROM mfa_credentials WHERE username=?', (user.username,))
        conn.execute('DELETE FROM recovery_codes WHERE username=?', (user.username,))
        conn.execute('DELETE FROM auth_challenges WHERE username=?', (user.username,))
        conn.execute('DELETE FROM sessions WHERE username=?', (user.username,))
    response.delete_cookie('dt_session')
    security_event(request.app.state.db, user.username, 'mfa_disabled')
    return {'signed_out': True}


@router.post('/api/security/mfa/recovery-codes')
def recovery(payload: Proof, request: Request, user: Principal = Depends(require_principal)):
    _proof(request, user, payload)
    if not mfa_enabled(request.app.state.db, user.username):
        raise HTTPException(409, 'Enable MFA before creating recovery codes')
    return {'recovery_codes': rotate_recovery(request, user.username)}


@router.get('/api/security/sessions')
def sessions(request: Request, user: Principal = Depends(require_principal)):
    with request.app.state.db.connect() as conn:
        rows = conn.execute('SELECT id,device_label,created_at,last_seen_at,expires_at FROM sessions WHERE username=? AND expires_at>? ORDER BY created_at DESC', (user.username, int(time.time()))).fetchall()
    return {'items': [{**dict(row), 'current': row['id'] == user.session_id} for row in rows]}


@router.post('/api/security/sessions/revoke')
def revoke(payload: Revoke, request: Request, response: Response, user: Principal = Depends(require_principal)):
    _proof(request, user, payload)
    with request.app.state.db.transaction() as conn:
        if payload.session_id == 'others':
            count = conn.execute('DELETE FROM sessions WHERE username=? AND id!=?', (user.username, user.session_id)).rowcount
        elif payload.session_id == 'all':
            count = conn.execute('DELETE FROM sessions WHERE username=?', (user.username,)).rowcount
        else:
            count = conn.execute('DELETE FROM sessions WHERE username=? AND id=?', (user.username, payload.session_id)).rowcount
    signed_out = payload.session_id in ('all', user.session_id)
    if signed_out:
        response.delete_cookie('dt_session')
    security_event(request.app.state.db, user.username, 'sessions_revoked', count=count)
    return {'revoked': count, 'signed_out': signed_out}


@router.get('/api/security/events')
def events(request: Request, user: Principal = Depends(require_principal)):
    with request.app.state.db.connect() as conn:
        rows = conn.execute('SELECT occurred_at,event_type,details FROM security_events WHERE username=? ORDER BY id DESC LIMIT 200', (user.username,)).fetchall()
    return {'items': [{**dict(row), 'details': json.loads(row['details'])} for row in rows], 'retention_days': 90}


def _would_allow(request: Request, value: dict) -> bool:
    peer = request.client.host if request.client else 'unknown'
    if value['allowlist_enabled'] and not _within(client_address(request, value), value['allowed_cidrs']):
        return False
    if value['proxy_gate_enabled']:
        return _within(peer, value['trusted_proxy_cidrs']) and request.headers.get('x-drivetheater-user') == request.app.state.config.admin_user
    return True


@router.post('/api/security/network/preview')
def network_preview(payload: NetworkPreview, request: Request, user: Principal = Depends(require_principal)):
    value = validate_policy(payload.policy)
    confirmation = secrets.token_hex(16)
    now = int(time.time())
    with request.app.state.db.transaction() as conn:
        conn.execute('DELETE FROM security_confirmations WHERE expires_at<?', (now,))
        conn.execute('DELETE FROM security_confirmations WHERE username=? AND kind=?', (user.username, 'network'))
        conn.execute('INSERT INTO security_confirmations(id,username,kind,body,expires_at) VALUES(?,?,?,?,?)', (confirmation, user.username, 'network', json.dumps(value), now + 300))
    return {'confirmation': confirmation, 'expires_in': 300, 'current_network_permitted': _would_allow(request, value),
            'recovery': 'Use the root-only security recovery command if network access is lost.'}


@router.post('/api/security/network/apply')
def network_apply(payload: NetworkApply, request: Request, user: Principal = Depends(require_principal)):
    _proof(request, user, payload)
    with request.app.state.db.transaction() as conn:
        row = conn.execute("SELECT body FROM security_confirmations WHERE id=? AND username=? AND kind='network' AND expires_at>?", (payload.confirmation, user.username, int(time.time()))).fetchone()
        if not row:
            raise HTTPException(409, 'Preview expired; review the policy again')
        value = validate_policy(json.loads(row['body']))
        if not _would_allow(request, value):
            raise HTTPException(409, 'This policy would block the current request; it was not applied')
        conn.execute("INSERT INTO security_policies(key,value) VALUES('network',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(value),))
        conn.execute('DELETE FROM security_confirmations WHERE id=?', (payload.confirmation,))
    security_event(request.app.state.db, user.username, 'network_policy_changed')
    return {'ok': True}
