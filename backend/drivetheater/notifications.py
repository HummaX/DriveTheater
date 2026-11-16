"""Durable, opt-in notifications. External delivery is at-least-once, never exactly-once.

Call emit_event inside the transaction which changes the domain state. Only the
worker calls process_outbox; request handlers only enqueue. Dedupe is bounded by
the 90-day/10,000-event retention window. Configurations are never returned.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import re
import smtplib
import socket
import ssl
import time
import uuid
from email.message import EmailMessage
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException

from .crypto_utils import open_secret, seal_secret

CHANNEL_KINDS = ('telegram', 'discord', 'smtp')
RETENTION_DAYS = 90
MAX_INBOX = 500
MAX_ATTEMPTS = 5
MAX_TICK = 5
LEASE_SECONDS = 120
MAX_RESPONSE = 8192
RATE_PER_MINUTE = 20
CHANNEL_RATE_PER_MINUTE = 3
_ERRORS = {
    'configuration_invalid': 'Notification configuration needs to be saved again.',
    'target_blocked': 'The notification destination is not permitted.',
    'authentication_failed': 'The notification service rejected its credentials.',
    'permission_denied': 'The notification service denied this request.',
    'rate_limited': 'The notification service requested a later retry.',
    'network_error': 'The notification service could not be reached securely.',
    'provider_error': 'The notification service is temporarily unavailable.',
    'response_invalid': 'The notification service returned an invalid response.',
    'delivery_rejected': 'The notification service rejected this message.',
}


class _CredentialURLFilter(logging.Filter):
    def filter(self, record):
        # httpx logs its complete request URL at INFO, including bot/webhook
        # credentials. Redact just these endpoints without changing log levels.
        message = record.getMessage()
        safe = re.sub(r'https://api\.telegram\.org/bot[^\s/]+', 'https://api.telegram.org/bot[redacted]', message)
        safe = re.sub(r'https://discord\.com/api/webhooks/[0-9]+/[^\s?\"\']+', 'https://discord.com/api/webhooks/[redacted]', safe)
        if safe != message:
            record.msg, record.args = safe, ()
        return True


logging.getLogger('httpx').addFilter(_CredentialURLFilter())


def _id() -> str:
    return uuid.uuid4().hex


def _text(value: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError('Notification text must be a string')
    # Source/provider errors and URLs must not leak bearer material into an inbox.
    value = re.sub(r'https?://[^\s<>]+', '[link redacted]', value, flags=re.I)
    value = re.sub(r'(?i)\b(password|token|secret|api[_ -]?key|authorization)\s*[:=]\s*\S+', r'\1=[redacted]', value)
    value = ''.join(c for c in value if c in '\n\t' or ord(c) >= 32)
    return value.strip()[:limit]


def _prune(conn, now: int) -> None:
    cutoff = now - RETENTION_DAYS * 86400
    conn.execute('DELETE FROM notifications WHERE created_at<?', (cutoff,))
    conn.execute('''DELETE FROM notifications WHERE id IN (
        SELECT id FROM (SELECT id,ROW_NUMBER() OVER(PARTITION BY username ORDER BY created_at DESC,rowid DESC) AS rank FROM notifications) WHERE rank>?)''', (MAX_INBOX,))
    conn.execute('DELETE FROM domain_events WHERE occurred_at<?', (cutoff,))
    conn.execute('DELETE FROM domain_events WHERE id NOT IN (SELECT id FROM domain_events ORDER BY occurred_at DESC,rowid DESC LIMIT 10000)')


def emit_event(conn, event_key: str, kind: str, title: str, message: str, *, username: str | None = None, entity_id: str | None = None) -> list[str]:
    """Insert the event, per-user inbox rows and enabled-channel outbox atomically.

    username=None snapshots *existing* users, not future accounts. Returns newly
    created inbox IDs; a duplicate returns []. No network I/O occurs here.
    """
    if not isinstance(event_key, str) or not 1 <= len(event_key) <= 512:
        raise ValueError('Use a bounded notification event key')
    if not isinstance(kind, str) or not re.fullmatch(r'[a-z][a-z0-9_]{1,63}', kind):
        raise ValueError('Invalid notification kind')
    title, message = _text(title, 160).replace('\n', ' ').replace('\t', ' '), _text(message, 2000)
    if not title:
        raise ValueError('A notification title is required')
    if entity_id is not None and (not isinstance(entity_id, str) or len(entity_id) > 200):
        raise ValueError('Invalid notification entity')
    if not conn.in_transaction:
        raise ValueError('emit_event requires the caller\'s transaction')
    now = int(time.time())
    scope = ['user', username] if username is not None else ['all']
    dedupe = 'notification:' + hashlib.sha256(json.dumps([scope, event_key]).encode()).hexdigest()
    inserted = conn.execute('INSERT OR IGNORE INTO domain_events(id,kind,entity_id,dedupe_key,occurred_at,payload) VALUES(?,?,?,?,?,?)',
                            (_id(), kind, entity_id, dedupe, now, json.dumps({'audience': 'user' if username is not None else 'all'}))).rowcount
    if not inserted:
        return []
    users = conn.execute('SELECT username FROM users' + (' WHERE username=?' if username is not None else ''), (username,) if username is not None else ()).fetchall()
    identities = []
    for user in users:
        identity = _id()
        added = conn.execute('INSERT OR IGNORE INTO notifications(id,username,event_key,kind,title,message,entity_id,created_at) VALUES(?,?,?,?,?,?,?,?)',
                             (identity, user['username'], event_key, kind, title, message, entity_id, now)).rowcount
        if not added:
            continue
        identities.append(identity)
        conn.execute('''INSERT OR IGNORE INTO notification_outbox(id,notification_id,channel_id,available_at)
            SELECT lower(hex(randomblob(16))),?,id,? FROM notification_channels WHERE username=? AND enabled=1''', (identity, now, user['username']))
    _prune(conn, now)
    return identities


def notify(db, username: str | None, kind: str, title: str, message: str, event_key: str) -> list[str]:
    """Convenience boundary for hooks which do not already have a transaction."""
    with db.transaction() as conn:
        return emit_event(conn, event_key, kind, title, message, username=username)


def list_inbox(db, username: str, *, limit: int = 100, before: int | None = None) -> dict:
    limit = max(1, min(int(limit), 200))
    cutoff = int(time.time()) - RETENTION_DAYS * 86400
    with db.connect() as conn:
        rows = conn.execute('''SELECT id,kind,title,message,entity_id,created_at,read_at FROM notifications
            WHERE username=? AND dismissed_at IS NULL AND created_at>=? AND (? IS NULL OR created_at<?)
            ORDER BY created_at DESC,rowid DESC LIMIT ?''', (username, cutoff, before, before, limit)).fetchall()
        unread = conn.execute('SELECT COUNT(*) FROM notifications WHERE username=? AND read_at IS NULL AND dismissed_at IS NULL AND created_at>=?', (username, cutoff)).fetchone()[0]
    return {'items': [{**dict(row), 'read': row['read_at'] is not None} for row in rows], 'unread_count': unread, 'retention_days': RETENTION_DAYS}


def update_inbox(db, username: str, identity: str, *, dismiss: bool = False) -> dict:
    now = int(time.time())
    with db.transaction() as conn:
        column = 'dismissed_at' if dismiss else 'read_at'
        changed = conn.execute(f'UPDATE notifications SET {column}=COALESCE({column},?) WHERE id=? AND username=?', (now, identity, username)).rowcount
        if not changed:
            raise HTTPException(404, 'Notification not found')
        if dismiss:
            conn.execute('UPDATE notifications SET read_at=COALESCE(read_at,?) WHERE id=? AND username=?', (now, identity, username))
    return {'ok': True}


def _kind(kind: str) -> None:
    if kind not in CHANNEL_KINDS:
        raise HTTPException(400, 'Unsupported notification channel')


def channel_status(db, username: str) -> dict:
    with db.connect() as conn:
        rows = {row['kind']: row for row in conn.execute('SELECT kind,enabled FROM notification_channels WHERE username=?', (username,))}
    return {'items': [{'kind': kind, 'configured': kind in rows, 'enabled': bool(rows[kind]['enabled']) if kind in rows else False} for kind in CHANNEL_KINDS]}


def _hostname(value: str) -> str:
    if not isinstance(value, str) or len(value) > 253:
        raise ValueError('Use a public SMTP hostname')
    try:
        host = value.encode('idna').decode('ascii').lower()
    except UnicodeError:
        raise ValueError('Use a public SMTP hostname') from None
    if not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}', host):
        raise ValueError('Use a public SMTP hostname')
    if host.endswith(('.localhost', '.local', '.internal', '.home', '.lan', '.test', '.invalid', '.onion')):
        raise ValueError('Use a public SMTP hostname')
    return host


def _plain(value, label: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Invalid ' + label)
    return value


def validate_channel(kind: str, value: dict) -> dict:
    """Syntax-only validation. Saving never resolves or contacts a destination."""
    _kind(kind)
    if not isinstance(value, dict):
        raise ValueError('Invalid notification configuration')
    allowed = {
        'telegram': {'bot_token', 'chat_id'},
        'discord': {'webhook_url'},
        'smtp': {'host', 'port', 'tls_mode', 'username', 'password', 'from_address', 'to_address'},
    }[kind]
    if set(value) != allowed:
        raise ValueError('Complete all fields for this notification channel')
    if kind == 'telegram':
        if not isinstance(value['bot_token'], str) or not re.fullmatch(r'[0-9]{5,16}:[A-Za-z0-9_-]{20,128}', value['bot_token']):
            raise ValueError('Enter a valid Telegram bot token')
        if not isinstance(value['chat_id'], str) or not re.fullmatch(r'-?[0-9]{1,20}', value['chat_id']):
            raise ValueError('Enter a numeric Telegram chat ID')
        return dict(value)
    if kind == 'discord':
        url = value['webhook_url']
        if not isinstance(url, str) or len(url) > 350 or not re.fullmatch(r'https://discord\.com/api/webhooks/[0-9]{15,24}/[A-Za-z0-9_-]{20,200}', url):
            raise ValueError('Use an HTTPS discord.com webhook URL without query parameters')
        parsed = urlsplit(url)
        if parsed.hostname != 'discord.com' or parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment:
            raise ValueError('Use an HTTPS discord.com webhook URL without query parameters')
        return {'webhook_url': url}
    result = dict(value)
    result['host'] = _hostname(value['host'])
    if type(value['port']) is not int or (value['tls_mode'], value['port']) not in (('implicit', 465), ('starttls', 587)):
        raise ValueError('Use verified TLS on port 465 or STARTTLS on port 587')
    result['username'] = _plain(value['username'], 'SMTP username', 254)
    result['password'] = _plain(value['password'], 'SMTP password')
    for field in ('from_address', 'to_address'):
        address = _plain(value[field], 'email address', 254)
        if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}", address):
            raise ValueError('Use a single email address without a display name')
        result[field] = address
    return result


def _purpose(username: str, kind: str) -> str:
    return 'notifications:' + hashlib.sha256(username.encode()).hexdigest() + ':' + kind


def _configuration(row, cfg) -> dict:
    return validate_channel(row['kind'], json.loads(open_secret(row['config_cipher'], cfg.secret, _purpose(row['username'], row['kind']))))


def _channel_event(conn, username: str, kind: str, action: str) -> None:
    # A settings action creates an inbox/audit event, not an implicit external test.
    ids = emit_event(conn, 'channel:' + _id(), 'notification_settings', 'Notification settings changed',
                     f'The {kind} notification channel was {action}.', username=username)
    for identity in ids:
        conn.execute('DELETE FROM notification_outbox WHERE notification_id=?', (identity,))


def save_channel(db, cfg, username: str, kind: str, config: dict, *, enabled: bool = False) -> dict:
    _kind(kind)
    if type(enabled) is not bool or not isinstance(config, dict) or len(config) > 7:
        raise ValueError('Invalid notification configuration')
    with db.transaction() as conn:
        row = conn.execute('SELECT * FROM notification_channels WHERE username=? AND kind=?', (username, kind)).fetchone()
        old = {}
        if row:
            try:
                old = _configuration(row, cfg)
            except Exception:
                # A complete replacement is still possible after tamper/key rotation.
                old = {}
        value = validate_channel(kind, {**old, **config})
        cipher = seal_secret(json.dumps(value, separators=(',', ':')), cfg.secret, _purpose(username, kind))
        identity = row['id'] if row else _id()
        conn.execute('''INSERT INTO notification_channels(id,username,kind,config_cipher,enabled,created_at) VALUES(?,?,?,?,?,?)
            ON CONFLICT(username,kind) DO UPDATE SET config_cipher=excluded.config_cipher,enabled=excluded.enabled''', (identity, username, kind, cipher, int(enabled), int(time.time())))
        # Do not replay stale queued notifications with newly supplied credentials.
        conn.execute("UPDATE notification_outbox SET state='cancelled',claim_until=0 WHERE channel_id=? AND state NOT IN ('sent','dead','cancelled')", (identity,))
        _channel_event(conn, username, kind, 'saved and enabled' if enabled else 'saved and disabled')
    return {'kind': kind, 'configured': True, 'enabled': enabled}


def set_channel_enabled(db, cfg, username: str, kind: str, enabled: bool) -> dict:
    _kind(kind)
    if type(enabled) is not bool:
        raise ValueError('Enabled must be a boolean')
    with db.transaction() as conn:
        row = conn.execute('SELECT * FROM notification_channels WHERE username=? AND kind=?', (username, kind)).fetchone()
        if not row:
            raise HTTPException(404, 'Notification channel not configured')
        if enabled:
            try:
                _configuration(row, cfg)
            except Exception:
                raise HTTPException(400, _ERRORS['configuration_invalid']) from None
        conn.execute('UPDATE notification_channels SET enabled=? WHERE id=?', (int(enabled), row['id']))
        if not enabled:
            conn.execute("UPDATE notification_outbox SET state='cancelled',claim_until=0 WHERE channel_id=? AND state NOT IN ('sent','dead','cancelled')", (row['id'],))
        _channel_event(conn, username, kind, 'enabled' if enabled else 'disabled')
    return {'kind': kind, 'configured': True, 'enabled': enabled}


def delete_channel(db, username: str, kind: str) -> dict:
    _kind(kind)
    with db.transaction() as conn:
        conn.execute('DELETE FROM notification_channels WHERE username=? AND kind=?', (username, kind))
        _channel_event(conn, username, kind, 'deleted')
    return {'kind': kind, 'configured': False, 'enabled': False}


def queue_test(db, username: str, kind: str) -> dict:
    _kind(kind)
    now = int(time.time())
    with db.transaction() as conn:
        row = conn.execute('SELECT id,enabled FROM notification_channels WHERE username=? AND kind=?', (username, kind)).fetchone()
        if not row or not row['enabled']:
            raise HTTPException(409, 'Save and explicitly enable this channel before testing it')
        key = 'notification-test:' + hashlib.sha256((username + ':' + kind).encode()).hexdigest() + ':' + str(now // 60)
        ids = emit_event(conn, key, 'notification_test', 'DriveTheater notification test',
                         'This test was requested from your notification settings.', username=username)
        if not ids:
            raise HTTPException(429, 'Wait a minute before requesting another channel test', headers={'Retry-After': '60'})
        conn.execute('DELETE FROM notification_outbox WHERE notification_id=? AND channel_id!=?', (ids[0], row['id']))
    return {'queued': True, 'notification_id': ids[0]}


class _DeliveryFailure(Exception):
    def __init__(self, code: str, *, retryable: bool = False, retry_after: int | None = None):
        super().__init__(_ERRORS[code])
        self.code, self.retryable, self.retry_after = code, retryable, retry_after


def _is_public(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
        if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
            parsed = parsed.ipv4_mapped
        return parsed.is_global and not (parsed.is_multicast or parsed.is_reserved or parsed.is_unspecified)
    except ValueError:
        return False


def _public_addresses(host: str, port: int) -> list[str]:
    try:
        addresses = sorted({str(ipaddress.ip_address(entry[4][0])) for entry in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    except (OSError, ValueError):
        raise _DeliveryFailure('network_error', retryable=True) from None
    if not addresses or len(addresses) > 16 or any(not _is_public(address) for address in addresses):
        raise _DeliveryFailure('target_blocked')
    return addresses


def _peer(sock, addresses: list[str]) -> None:
    try:
        address = str(ipaddress.ip_address(sock.getpeername()[0]))
    except (OSError, ValueError, IndexError):
        raise _DeliveryFailure('target_blocked') from None
    if not _is_public(address) or address not in addresses:
        raise _DeliveryFailure('target_blocked')


class _PinnedSMTPMixin:
    """Resolve once, connect only to validated numeric IPs, retain TLS hostname."""
    def _get_socket(self, host, port, timeout):
        self._deadline = time.monotonic() + 40
        for address in self._public_ips[:3]:
            sock = None
            try:
                sock = socket.create_connection((address, port), timeout)
                _peer(sock, self._public_ips)
                if self._implicit_tls:
                    sock = self.context.wrap_socket(sock, server_hostname=host)
                    _peer(sock, self._public_ips)
                return sock
            except _DeliveryFailure:
                if sock:
                    sock.close()
                raise
            except OSError:
                if sock:
                    sock.close()
        raise _DeliveryFailure('network_error', retryable=True) from None

    def getreply(self):
        # smtplib's default parser allows unbounded multiline replies. Limit both
        # line/body size and elapsed connection time before any AUTH exchange.
        if self.file is None:
            self.file = self.sock.makefile('rb')
        lines, size = [], 0
        for _ in range(20):
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise _DeliveryFailure('network_error', retryable=True)
            self.sock.settimeout(min(5, remaining))
            line = self.file.readline(4097)
            size += len(line)
            if not line or len(line) > 4096 or size > MAX_RESPONSE:
                raise _DeliveryFailure('response_invalid')
            if len(line) < 4 or not line[:3].isdigit():
                raise _DeliveryFailure('response_invalid')
            code = int(line[:3])
            lines.append(line[4:].strip(b' \t\r\n'))
            if line[3:4] != b'-':
                return code, b'\n'.join(lines)
        raise _DeliveryFailure('response_invalid')


class _PinnedSMTP(_PinnedSMTPMixin, smtplib.SMTP):
    _implicit_tls = False

    def __init__(self, host, port, addresses):
        self._public_ips = addresses
        super().__init__(host, port, timeout=5)


class _PinnedSMTPSSL(_PinnedSMTPMixin, smtplib.SMTP_SSL):
    _implicit_tls = True

    def __init__(self, host, port, addresses, context):
        self._public_ips = addresses
        super().__init__(host, port, timeout=5, context=context)


def _send_smtp(config: dict, title: str, message: str) -> None:
    addresses = _public_addresses(config['host'], config['port'])
    context = ssl.create_default_context()
    smtp = (_PinnedSMTPSSL(config['host'], config['port'], addresses, context) if config['tls_mode'] == 'implicit'
            else _PinnedSMTP(config['host'], config['port'], addresses))
    try:
        smtp.ehlo()
        if config['tls_mode'] == 'starttls':
            if not smtp.has_extn('starttls'):
                raise _DeliveryFailure('network_error')
            smtp.starttls(context=context)
            smtp.ehlo()
        # Check the actual encrypted connection BEFORE sending AUTH or message data.
        _peer(smtp.sock, addresses)
        if not isinstance(smtp.sock, ssl.SSLSocket):
            raise _DeliveryFailure('network_error')
        smtp.login(config['username'], config['password'])
        email = EmailMessage()
        email['From'], email['To'], email['Subject'] = config['from_address'], config['to_address'], title
        email.set_content(message)
        refused = smtp.send_message(email)
        if refused:
            raise _DeliveryFailure('delivery_rejected')
    finally:
        # Once DATA was acknowledged, a failing QUIT must not turn success into
        # a retry (and a needless duplicate). Close the bounded TLS connection.
        try:
            smtp.close()
        except OSError:
            pass


def _retry_after(value) -> int | None:
    if isinstance(value, (str, int)) and re.fullmatch(r'[0-9]{1,6}', str(value)):
        return max(1, min(int(value), 3600))
    return None


def _send_http(kind: str, config: dict, title: str, message: str) -> None:
    text = (title + '\n\n' + message)[:1900]
    if kind == 'telegram':
        url = 'https://api.telegram.org/bot' + config['bot_token'] + '/sendMessage'
        payload = {'chat_id': config['chat_id'], 'text': text, 'link_preview_options': {'is_disabled': True}}
    else:
        url = config['webhook_url']
        payload = {'content': text, 'allowed_mentions': {'parse': []}}
    # Host names and paths above are allowlisted; never accept generic webhooks.
    host = urlsplit(url).hostname
    if host not in ('api.telegram.org', 'discord.com'):
        raise _DeliveryFailure('target_blocked')
    _public_addresses(host, 443)
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=httpx.Timeout(5, connect=3),
                      limits=httpx.Limits(max_connections=1, max_keepalive_connections=0)) as client:
        with client.stream('POST', url, json=payload, headers={'Accept-Encoding': 'identity'}) as response:
            status = response.status_code
            if status == 429:
                raise _DeliveryFailure('rate_limited', retryable=True, retry_after=_retry_after(response.headers.get('retry-after')))
            if status == 401:
                raise _DeliveryFailure('authentication_failed')
            if status == 403:
                raise _DeliveryFailure('permission_denied')
            if status >= 500:
                raise _DeliveryFailure('provider_error', retryable=True)
            if not 200 <= status < 300:
                raise _DeliveryFailure('delivery_rejected')
            if response.headers.get('content-encoding', 'identity').lower() not in ('', 'identity'):
                raise _DeliveryFailure('response_invalid')
            body = bytearray()
            for chunk in response.iter_raw(chunk_size=4096):
                body.extend(chunk)
                if len(body) > MAX_RESPONSE:
                    raise _DeliveryFailure('response_invalid')
            if kind == 'telegram':
                try:
                    value = json.loads(body)
                    accepted = isinstance(value, dict) and value.get('ok') is True
                except (ValueError, UnicodeError):
                    accepted = False
                if not accepted:
                    raise _DeliveryFailure('response_invalid')


def _deliver(row, cfg) -> None:
    try:
        config = _configuration(row, cfg)
    except Exception:
        raise _DeliveryFailure('configuration_invalid') from None
    try:
        if row['kind'] == 'smtp':
            _send_smtp(config, row['title'], row['message'])
        else:
            _send_http(row['kind'], config, row['title'], row['message'])
    except _DeliveryFailure:
        raise
    except smtplib.SMTPAuthenticationError:
        raise _DeliveryFailure('authentication_failed') from None
    except smtplib.SMTPResponseException as exc:
        raise _DeliveryFailure('provider_error' if 400 <= exc.smtp_code < 500 else 'delivery_rejected', retryable=400 <= exc.smtp_code < 500) from None
    except (httpx.HTTPError, OSError, smtplib.SMTPException):
        raise _DeliveryFailure('network_error', retryable=True) from None
    except Exception:
        # Neither exception text nor request URLs (which contain tokens) escape.
        raise _DeliveryFailure('response_invalid') from None


def process_outbox(db, cfg, *, now: int | None = None) -> dict:
    """One bounded worker tick; safe to invoke from multiple worker processes.

    A unique state token fences stale lease holders. A crash after remote receipt
    but before local acknowledgment can duplicate delivery; providers do not all
    support idempotency. Disabling cancels queued work, not an in-flight request.
    """
    base_clock = int(time.time() if now is None else now)
    started = time.monotonic()

    def tick_time() -> int:
        return base_clock + int(max(0, time.monotonic() - started))

    clock = tick_time()
    result = {'claimed': 0, 'sent': 0, 'retried': 0, 'dead': 0}
    with db.transaction() as conn:
        result['dead'] += conn.execute("UPDATE notification_outbox SET state='dead',claim_until=0,last_error='attempts_exhausted' WHERE attempts>=? AND (state IN ('queued','retry','sending') OR state LIKE 'sending:%') AND claim_until<=?", (MAX_ATTEMPTS, clock)).rowcount
        conn.execute('''UPDATE notification_outbox SET state='cancelled',claim_until=0
            WHERE state NOT IN ('sent','dead','cancelled') AND channel_id IN (SELECT id FROM notification_channels WHERE enabled=0)''')
        _prune(conn, clock)
    for _ in range(MAX_TICK):
        token = 'sending:' + _id()
        with db.transaction() as conn:
            clock = tick_time()
            count = conn.execute("SELECT COUNT(*) FROM domain_events WHERE kind='notification_attempt' AND occurred_at>?", (clock - 60,)).fetchone()[0]
            if count >= RATE_PER_MINUTE:
                break
            row = conn.execute('''SELECT o.id,o.attempts,o.channel_id FROM notification_outbox o
                JOIN notification_channels c ON c.id=o.channel_id JOIN notifications n ON n.id=o.notification_id
                WHERE c.enabled=1 AND c.username=n.username AND o.attempts<? AND o.available_at<=?
                  AND (o.state IN ('queued','retry') OR ((o.state='sending' OR o.state LIKE 'sending:%') AND o.claim_until<=?))
                  AND (SELECT COUNT(*) FROM domain_events e WHERE e.kind='notification_attempt' AND e.entity_id=c.id AND e.occurred_at>?)<?
                ORDER BY o.available_at,o.rowid LIMIT 1''', (MAX_ATTEMPTS, clock, clock, clock - 60, CHANNEL_RATE_PER_MINUTE)).fetchone()
            if not row:
                break
            attempt = row['attempts'] + 1
            conn.execute('UPDATE notification_outbox SET state=?,attempts=?,claim_until=?,last_error=NULL WHERE id=?', (token, attempt, clock + LEASE_SECONDS, row['id']))
            conn.execute('INSERT OR IGNORE INTO domain_events(id,kind,entity_id,dedupe_key,occurred_at) VALUES(?,?,?,?,?)', (_id(), 'notification_attempt', row['channel_id'], 'delivery:' + row['id'] + ':' + str(attempt), clock))
        result['claimed'] += 1
        with db.connect() as conn:
            delivery = conn.execute('''SELECT c.*,n.title,n.message FROM notification_outbox o JOIN notification_channels c ON c.id=o.channel_id
                JOIN notifications n ON n.id=o.notification_id WHERE o.id=? AND o.state=? AND c.enabled=1 AND c.username=n.username''', (row['id'], token)).fetchone()
        if not delivery:
            continue
        failure = None
        try:
            _deliver(delivery, cfg)
        except _DeliveryFailure as exc:
            failure = exc
        with db.transaction() as conn:
            clock = tick_time()
            if failure:
                retry = failure.retryable and attempt < MAX_ATTEMPTS
                state = 'retry' if retry else 'dead'
                delay = max(min(30 * (2 ** (attempt - 1)), 1800), failure.retry_after or 0)
                changed = conn.execute('UPDATE notification_outbox SET state=?,available_at=?,claim_until=0,last_error=? WHERE id=? AND state=?', (state, clock + delay if retry else clock, failure.code, row['id'], token)).rowcount
                if changed:
                    result['retried' if retry else 'dead'] += 1
            else:
                changed = conn.execute("UPDATE notification_outbox SET state='sent',available_at=?,claim_until=0,last_error=NULL WHERE id=? AND state=?", (clock, row['id'], token)).rowcount
                result['sent'] += changed
    return result
