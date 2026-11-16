"""Authenticated inbox and opt-in channels. No route sends a notification."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .access import Principal, require_principal, security_event
from .security import verify_sensitive
from . import notifications as service

router = APIRouter()


async def _body(request: Request) -> dict:
    """Bound JSON before parsing; validation errors never echo credential input."""
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 16384:
            raise HTTPException(413, 'Notification settings are too large')
        raw.extend(chunk)
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(400, 'Use a valid JSON settings object') from None
    if not isinstance(payload, dict):
        raise HTTPException(400, 'Use a valid JSON settings object')
    return payload


def _proof(request: Request, user: Principal, payload: dict, extra: set[str] | None = None) -> None:
    if set(payload) - {'password', 'code', 'recovery_code'} - (extra or set()):
        raise HTTPException(400, 'Unsupported notification settings field')
    password, code, recovery = payload.get('password'), payload.get('code'), payload.get('recovery_code')
    if (not isinstance(password, str) or not 1 <= len(password) <= 1024
            or code is not None and (not isinstance(code, str) or len(code) > 8)
            or recovery is not None and (not isinstance(recovery, str) or len(recovery) > 64)):
        raise HTTPException(400, 'Current password and an unused MFA/recovery code are required')
    verify_sensitive(request, user.username, password, code, recovery)


def _audit(request: Request, user: Principal, kind: str, action: str, **details) -> None:
    security_event(request.app.state.db, user.username, 'notification_channel_' + action, channel=kind, **details)


@router.get('/api/notifications')
def inbox(request: Request, user: Principal = Depends(require_principal), limit: int = Query(100, ge=1, le=200), before: int | None = Query(None, ge=0)):
    return service.list_inbox(request.app.state.db, user.username, limit=limit, before=before)


@router.get('/api/notifications/channels')
def channels(request: Request, user: Principal = Depends(require_principal)):
    # Only kind/configured/enabled leave the server, never sealed or open config.
    return service.channel_status(request.app.state.db, user.username)


@router.post('/api/notifications/{identity}/read')
def read(identity: str, request: Request, user: Principal = Depends(require_principal)):
    return service.update_inbox(request.app.state.db, user.username, identity)


@router.post('/api/notifications/{identity}/dismiss')
def dismiss(identity: str, request: Request, user: Principal = Depends(require_principal)):
    return service.update_inbox(request.app.state.db, user.username, identity, dismiss=True)


@router.post('/api/notifications/channels/{kind}')
def save(kind: str, request: Request, user: Principal = Depends(require_principal), payload: dict = Depends(_body)):
    service._kind(kind)
    _proof(request, user, payload, {'config', 'enabled'})
    enabled, config = payload.get('enabled', False), payload.get('config')
    if type(enabled) is not bool or not isinstance(config, dict):
        raise HTTPException(400, 'Provide channel settings and an explicit boolean enabled switch')
    try:
        result = service.save_channel(request.app.state.db, request.app.state.config, user.username, kind, config, enabled=enabled)
    except ValueError:
        raise HTTPException(400, 'Check the notification channel fields; no changes were saved') from None
    except RuntimeError:
        raise HTTPException(503, 'Notification secret encryption is unavailable; no changes were saved') from None
    _audit(request, user, kind, 'saved', enabled=enabled)
    return result


@router.post('/api/notifications/channels/{kind}/enabled')
def enable(kind: str, request: Request, user: Principal = Depends(require_principal), payload: dict = Depends(_body)):
    service._kind(kind)
    _proof(request, user, payload, {'enabled'})
    if type(payload.get('enabled')) is not bool:
        raise HTTPException(400, 'Provide an explicit boolean enabled switch')
    result = service.set_channel_enabled(request.app.state.db, request.app.state.config, user.username, kind, payload['enabled'])
    _audit(request, user, kind, 'enabled' if payload['enabled'] else 'disabled', enabled=payload['enabled'])
    return result


@router.post('/api/notifications/channels/{kind}/delete')
def delete(kind: str, request: Request, user: Principal = Depends(require_principal), payload: dict = Depends(_body)):
    service._kind(kind)
    _proof(request, user, payload)
    result = service.delete_channel(request.app.state.db, user.username, kind)
    _audit(request, user, kind, 'deleted')
    return result


@router.post('/api/notifications/channels/{kind}/test')
def test(kind: str, request: Request, user: Principal = Depends(require_principal), payload: dict = Depends(_body)):
    service._kind(kind)
    _proof(request, user, payload)
    result = service.queue_test(request.app.state.db, user.username, kind)
    _audit(request, user, kind, 'test_queued')
    return result
