"""Metadata-only source outcomes. No provider access, media fetch or job enqueue."""
from __future__ import annotations

import socket
import time
from email.utils import parsedate_to_datetime

import httpx

from .notifications import emit_event

_ERRORS = {
    'auth_required': ('Authorization is missing or expired. Reconnect this source.', False, None),
    'permission_denied': ('This source denied access. Review folder sharing and account permissions.', False, None),
    'rate_limited': ('This source is rate-limiting requests. Wait before trying again.', True, 60),
    'network_error': ('A secure connection to this source could not be completed. Try again later.', True, 30),
    'provider_unavailable': ('This source is temporarily unavailable. Try again later.', True, 60),
    'not_found': ('The source or folder was not found. Review its settings and permissions.', False, None),
    'configuration_error': ('This source needs its connection settings reviewed.', False, None),
    'unknown_error': ('The source operation failed. Review its settings and try again.', False, None),
}
_DEFAULT = {'state': 'unknown', 'last_test_at': None, 'last_sync_at': None,
            'last_success_at': None, 'last_sync_success_at': None,
            'error_code': None, 'message': None, 'retry_at': None, 'failures': 0}
HISTORY_LIMIT = 100
HISTORY_DAYS = 90


def _retry_header(response, now: int) -> int | None:
    headers = getattr(response, 'headers', None)
    if headers is None:
        return None
    value = headers.get('retry-after')
    if not isinstance(value, str) or len(value) > 80:
        return None
    if value.isascii() and value.isdigit():
        return now + max(1, min(int(value), 3600))
    try:
        stamp = int(parsedate_to_datetime(value).timestamp())
        return max(now + 1, min(stamp, now + 3600))
    except (ValueError, TypeError, OverflowError):
        return None


def classify_error(exc) -> dict:
    """Map known failures to constant, credential-free, actionable public text.

    Never return raw exception strings, provider response bodies, URLs, hostnames,
    credential paths or tokens. The result can be stored/returned without a UI
    redaction step. retry_at is advisory only; this service never retries a job.
    """
    now = int(time.time())
    response = getattr(exc, 'response', None)
    if response is None:
        response = getattr(exc, 'resp', None)
    status = getattr(exc, 'status_code', None)
    if status is None:
        status = getattr(response, 'status_code', getattr(response, 'status', None))
    code = 'unknown_error'
    if status == 401:
        code = 'auth_required'
    elif status == 403:
        code = 'permission_denied'
    elif status == 429:
        code = 'rate_limited'
    elif status == 404:
        code = 'not_found'
    elif isinstance(status, int) and 500 <= status <= 599:
        code = 'provider_unavailable'
    elif isinstance(exc, PermissionError):
        code = 'permission_denied'
    elif isinstance(exc, (httpx.TransportError, TimeoutError, ConnectionError, socket.gaierror)) or type(exc).__name__ in ('TransportError', 'ConnectTimeout', 'ReadTimeout', 'ConnectionError', 'Timeout', 'SSLError'):
        code = 'network_error'
    else:
        # Inspect bounded string arguments solely to recognize adapter/OAuth
        # failures. Never invoke an unknown exception's custom __str__ method.
        text = ' '.join(value[:2048] for value in exc.args[:3] if isinstance(value, str))[:2048].lower() if isinstance(exc, BaseException) else ''
        name = type(exc).__name__
        if name in ('RefreshError', 'InvalidGrantError', 'Unauthorized') or any(item in text for item in (
                'invalid_grant', 'invalid_token', 'oauth authorization', 'oauth token refresh', 'credentials have expired', 'reauthentication')):
            code = 'auth_required'
        elif name in ('Forbidden', 'PermissionDenied') or 'insufficient permission' in text or 'anyone with the link' in text:
            code = 'permission_denied'
        elif name in ('TooManyRequests', 'ResourceExhausted') or 'rate limit exceeded' in text or 'quota exceeded' in text:
            code = 'rate_limited'
        elif isinstance(exc, ValueError) or 'credential configuration is invalid' in text or 'support is not installed' in text:
            code = 'configuration_error'
    message, retryable, delay = _ERRORS[code]
    retry_at = now + delay if delay is not None else None
    if code == 'rate_limited':
        retry_at = _retry_header(response, now) or retry_at
    return {'code': code, 'message': message, 'retry_at': retry_at, 'retryable': retryable}


def _dto(row) -> dict:
    value = dict(row) if row else dict(_DEFAULT)
    # The legacy column name is the last successful explicit Test, NOT a sync.
    value['last_test_success_at'] = value.get('last_success_at')
    value['retryable'] = _ERRORS.get(value.get('error_code'), ('', False, None))[1]
    return value


def record_result(db, source_id: str, action: str, success: bool, error=None) -> dict:
    """Persist an explicit Test/Sync/OAuth outcome and failure/recovery transitions.

    last_test_at/last_sync_at record attempts; last_success_at exclusively records
    a successful Test. Successful OAuth does not pretend a Test or Sync happened.
    Notifications are transactionally deduped by transition history identity.
    """
    if action not in ('test', 'sync', 'oauth') or type(success) is not bool:
        raise ValueError('Use an explicit source outcome action and boolean success')
    if not isinstance(source_id, str) or not 1 <= len(source_id) <= 200:
        raise ValueError('Invalid source identity')
    now = int(time.time())
    failure = classify_error(error) if not success else None
    with db.transaction() as conn:
        if not conn.execute('SELECT 1 FROM sources WHERE id=? AND deleted=0', (source_id,)).fetchone():
            raise ValueError('Source not found')
        previous = conn.execute('SELECT * FROM source_health WHERE source_id=?', (source_id,)).fetchone()
        value = {**_DEFAULT, **(dict(previous) if previous else {}), 'source_id': source_id}
        if action == 'test':
            value['last_test_at'] = now
            if success:
                value['last_success_at'] = now
        if action == 'sync':
            value['last_sync_at'] = now
            if success:
                value['last_sync_success_at'] = now
        value.update(state='healthy' if success else 'error', failures=0 if success else value['failures'] + 1,
                     error_code=failure['code'] if failure else None, message=failure['message'] if failure else None,
                     retry_at=failure['retry_at'] if failure else None)
        columns = ('source_id', 'state', 'last_test_at', 'last_sync_at', 'last_success_at', 'last_sync_success_at',
                   'error_code', 'message', 'retry_at', 'failures')
        conn.execute('''INSERT INTO source_health(source_id,state,last_test_at,last_sync_at,last_success_at,last_sync_success_at,error_code,message,retry_at,failures)
            VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET state=excluded.state,last_test_at=excluded.last_test_at,
            last_sync_at=excluded.last_sync_at,last_success_at=excluded.last_success_at,last_sync_success_at=excluded.last_sync_success_at,
            error_code=excluded.error_code,message=excluded.message,retry_at=excluded.retry_at,failures=excluded.failures''', tuple(value[key] for key in columns))
        cursor = conn.execute('INSERT INTO source_health_history(source_id,state,error_code,occurred_at) VALUES(?,?,?,?)', (source_id, value['state'], value['error_code'], now))
        prior_state = previous['state'] if previous else 'unknown'
        if value['state'] == 'error' and prior_state != 'error':
            emit_event(conn, f'source-health:{source_id}:{cursor.lastrowid}:failed', 'source_failure',
                       'Source needs attention', failure['message'], entity_id=source_id)
        elif value['state'] == 'healthy' and prior_state == 'error':
            emit_event(conn, f'source-health:{source_id}:{cursor.lastrowid}:recovered', 'source_recovered',
                       'Source connection recovered', 'A previously failing source completed an operation successfully.', entity_id=source_id)
        conn.execute('DELETE FROM source_health_history WHERE occurred_at<?', (now - HISTORY_DAYS * 86400,))
        conn.execute('DELETE FROM source_health_history WHERE source_id=? AND id NOT IN (SELECT id FROM source_health_history WHERE source_id=? ORDER BY id DESC LIMIT ?)', (source_id, source_id, HISTORY_LIMIT))
        conn.execute('DELETE FROM source_health_history WHERE id NOT IN (SELECT id FROM source_health_history ORDER BY id DESC LIMIT 10000)')
    return _dto(value)


def get_health(db, source_id: str) -> dict:
    with db.connect() as conn:
        row = conn.execute('SELECT * FROM source_health WHERE source_id=?', (source_id,)).fetchone()
    return _dto(row) if row else {'source_id': source_id, **_dto(None)}


def delete_health(conn, source_id: str) -> None:
    """Remove source FK dependents inside the caller's hard-delete transaction.

    Historical inbox/domain events have no source FK and deliberately remain.
    This does not remove a source, provider configuration, video, or job.
    """
    if not conn.in_transaction:
        raise ValueError('delete_health requires the caller\'s transaction')
    conn.execute('DELETE FROM source_health_history WHERE source_id=?', (source_id,))
    conn.execute('DELETE FROM source_health WHERE source_id=?', (source_id,))


def attach_health(db, source_dict: dict) -> dict:
    """Return a copy with health metadata; never mutate source fields or enqueue."""
    return {**source_dict, 'health': get_health(db, source_dict['id'])}
