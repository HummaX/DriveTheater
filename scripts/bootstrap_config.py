#!/usr/bin/env python3
"""Installer state and consistent backups. Never execute dotenv as shell code."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import sys
import tarfile
from urllib.parse import urlparse

REQUIRED = ('DT_HOST_DATA_DIR', 'DT_UID', 'DT_GID', 'DT_PORT', 'DT_ADMIN_USER',
            'DT_ADMIN_PASSWORD', 'DT_SECRET', 'DT_BIND')
PUBLIC_FIELDS = ('DT_HOST_DATA_DIR', 'DT_UID', 'DT_GID', 'DT_PORT', 'DT_ADMIN_USER', 'DT_BIND', 'DT_PUBLIC_URL')
BUNDLE_SCHEMA = 1
BUNDLE_MAX_BYTES = 1024 * 1024
BUNDLE_SOURCE_FIELDS = frozenset(('id', 'name', 'kind', 'auth_mode', 'folder_id', 'service_account_email'))
BUNDLE_SETTINGS = frozenset(('cache_limit_gb', 'min_free_gb', 'download_concurrency', 'max_transcodes',
                             'activity_retention_days', 'download_schedule_enabled', 'download_start_hour',
                             'download_end_hour', 'download_limit_mbps', 'auto_cache_cleanup',
                             'cache_cleanup_target_percent', 'torrentio_manifest_url', 'metadata_manifest_url'))


def plain_path(value):
    path = Path(value)
    if not path.is_absolute() or path == Path(path.anchor) or str(path) != os.path.realpath(path):
        raise ValueError('Use an absolute directory without symlinks; not the filesystem root')
    if not re.fullmatch(r'[A-Za-z0-9_./:\\-]+', str(path)):
        raise ValueError('Installation paths must not contain whitespace or shell characters')
    return path


def exclusive(path, data, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def read_env(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('Installation configuration must be a regular, single-link file')
    if os.name == 'posix' and (info.st_mode & 0o077):
        raise ValueError('Installation configuration must be private (mode 600)')
    values = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line or line.startswith('#'):
            continue
        match = re.fullmatch(r'([A-Z_]+)=([^\s\x00$`\'"\\#]*)', line)
        if not match or match[1] in values:
            raise ValueError('Invalid or duplicate installation configuration')
        values[match[1]] = match[2]
    if any(not values.get(key) for key in REQUIRED):
        raise ValueError('Installation configuration is incomplete; preserve it and investigate')
    plain_path(values['DT_HOST_DATA_DIR'])
    if not 1 <= int(values['DT_PORT']) <= 65535 or min(int(values['DT_UID']), int(values['DT_GID'])) < 1:
        raise ValueError('Invalid runtime identity or port')
    return values


def make_dir(path, uid, gid):
    if path.exists() or path.is_symlink():
        if not path.is_dir() or path.is_symlink():
            raise ValueError('Runtime path must be a plain directory')
        return
    path.mkdir(mode=0o750)
    if os.name == 'posix' and os.geteuid() == 0:
        os.chown(path, uid, gid)


def initialize(envfile, data, uid, gid, port=80, bind='0.0.0.0', public_url=''):
    data = plain_path(str(data))
    uid, gid, port = int(uid), int(gid), int(port)
    if min(uid, gid) < 1 or not 1 <= port <= 65535:
        raise ValueError('Runtime must be non-root, and port must be 1..65535')
    if bind not in ('0.0.0.0', '127.0.0.1'):
        raise ValueError('Bind must be 0.0.0.0 or 127.0.0.1')
    if public_url and not re.fullmatch(r'https?://[A-Za-z0-9.:-]+', public_url):
        raise ValueError('Public URL must be an HTTP(S) origin')
    if envfile.exists() or envfile.is_symlink():
        values = read_env(envfile)
        if (Path(values['DT_HOST_DATA_DIR']) != data or int(values['DT_UID']) != uid
                or int(values['DT_GID']) != gid or int(values['DT_PORT']) != port):
            raise ValueError('Existing installation identity differs; no configuration overwritten')
        marker = data / '.drivetheater-install.json'
        if not marker.exists() and not marker.is_symlink():
            expected = {'db', 'config', 'cache', 'backups'}
            if (not data.is_dir() or any(item.name not in expected for item in data.iterdir())
                    or any(not item.is_dir() or item.is_symlink() or any(item.iterdir()) for item in data.iterdir())):
                raise ValueError('Missing installation marker with populated data; recover original configuration')
            exclusive(marker, json.dumps({'application': 'DriveTheater', 'schema': 1,
                                           'data_dir': data.as_posix(), 'recovered': True}).encode())
        verify_installation(envfile)
        return values, False
    if data.is_symlink() or (data.exists() and (not data.is_dir() or any(data.iterdir()))):
        raise ValueError('Fresh setup refuses nonempty data without its original installation configuration')
    if not envfile.parent.is_dir() or not data.parent.is_dir():
        raise ValueError('Configuration and data parent directories must exist')
    free_bytes = shutil.disk_usage(data if data.exists() else data.parent).free
    if free_bytes < 2 * 1024**3:
        raise ValueError('At least 2 GiB of free disk space is required before setup')
    existed = data.exists()
    make_dir(data, uid, gid)
    if existed and os.name == 'posix' and os.geteuid() == 0:
        os.chown(data, uid, gid)
    for name in ('db', 'config', 'cache', 'backups'):
        make_dir(data / name, uid, gid)
    values = {'DT_HOST_DATA_DIR': data.as_posix(), 'DT_UID': str(uid), 'DT_GID': str(gid),
              'DT_PORT': str(port), 'DT_BIND': bind, 'DT_ADMIN_USER': 'admin',
              'DT_ADMIN_PASSWORD': secrets.token_urlsafe(24), 'DT_SECRET': secrets.token_urlsafe(48),
              'DT_PUBLIC_URL': public_url, 'DT_COOKIE_SECURE': 'true' if public_url.startswith('https://') else 'false',
              'DT_MEDIA_CPUS': str(round(max(1, os.cpu_count() or 1) * .9, 2)),
              'DT_CACHE_LIMIT_GB': str(max(1, (free_bytes * 3 // 4) // 1024**3)),
              'DT_AUTO_CAPTIONS': 'true', 'DT_CAPTION_MODEL': 'small'}
    exclusive(envfile, ''.join(key + '=' + value + '\n' for key, value in values.items()).encode())
    exclusive(data / '.drivetheater-install.json', json.dumps({'application': 'DriveTheater', 'schema': 1,
              'data_dir': data.as_posix(), 'created_at': datetime.now(timezone.utc).isoformat()}).encode())
    return values, True


def _private_regular(path, label, require_private=True):
    """Return a local regular file after rejecting links and permissive secrets."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or path.is_symlink():
        raise ValueError(label + ' must be a regular, single-link file')
    if require_private and os.name == 'posix' and (info.st_mode & 0o077):
        raise ValueError(label + ' must be private (mode 600)')
    return info


def _bundle_path(value, output=False):
    path = Path(value)
    if not path.is_absolute() or path.name in ('', '.', '..') or str(path) != os.path.realpath(path):
        raise ValueError('Setup bundle path must be an absolute path without symlinks')
    if not output:
        info = _private_regular(path, 'Setup bundle')
        if info.st_size > BUNDLE_MAX_BYTES:
            raise ValueError('Setup bundle exceeds the 1 MiB limit')
    elif path.exists() or path.is_symlink():
        raise ValueError('Setup bundle destination already exists')
    return path


def _bundle_string(value, label, limit, pattern=None, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(char) < 32 for char in value):
        raise ValueError('Invalid setup bundle ' + label)
    if pattern and not re.fullmatch(pattern, value):
        raise ValueError('Invalid setup bundle ' + label)
    return value


def validate_setup_setting(key, value):
    if key not in BUNDLE_SETTINGS or not isinstance(value, str):
        raise ValueError('Setup bundle setting is invalid')
    is_addon = key in ('torrentio_manifest_url', 'metadata_manifest_url')
    if is_addon:
        if len(value) > 4096 or any(ord(char) < 32 for char in value):
            raise ValueError('Setup bundle add-on URL is invalid')
    elif len(value) > 64 or not re.fullmatch(r'[A-Za-z0-9._-]+', value):
        raise ValueError('Setup bundle setting is invalid')
    if key in ('torrentio_manifest_url', 'metadata_manifest_url'):
        if value:
            parsed = urlparse(value)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or not parsed.path.endswith('/manifest.json'):
                raise ValueError('Setup bundle add-on URL is invalid')
        return value
    if key in ('download_schedule_enabled', 'auto_cache_cleanup'):
        if value not in ('true', 'false'):
            raise ValueError('Setup bundle boolean setting is invalid')
    elif key in ('cache_limit_gb', 'min_free_gb', 'download_limit_mbps'):
        try:
            number = float(value)
        except ValueError as exc:
            raise ValueError('Setup bundle numeric setting is invalid') from exc
        minimum = 0 if key == 'download_limit_mbps' else .1
        if not math.isfinite(number) or number < minimum or (key == 'cache_limit_gb' and number <= 0):
            raise ValueError('Setup bundle numeric setting is out of range')
    else:
        bounds = {'download_concurrency': (1, 2), 'max_transcodes': (1, 2),
                  'activity_retention_days': (1, 3650), 'download_start_hour': (0, 23),
                  'download_end_hour': (0, 23), 'cache_cleanup_target_percent': (1, 100)}
        if key not in bounds or not value.isdigit() or not bounds[key][0] <= int(value) <= bounds[key][1]:
            raise ValueError('Setup bundle integer setting is out of range')
    return value


def read_setup_bundle(bundle_file):
    path = _bundle_path(bundle_file)
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError('Setup bundle must contain UTF-8 JSON') from exc
    if not isinstance(payload, dict) or set(payload) != {'schema', 'sources', 'credentials', 'settings'}:
        raise ValueError('Setup bundle has an unexpected schema')
    if payload['schema'] != BUNDLE_SCHEMA or not isinstance(payload['sources'], list) or not isinstance(payload['credentials'], dict) or not isinstance(payload['settings'], dict):
        raise ValueError('Setup bundle has an invalid schema')
    if len(payload['sources']) > 100 or len(payload['credentials']) > 100 or len(payload['settings']) > len(BUNDLE_SETTINGS):
        raise ValueError('Setup bundle exceeds bounded limits')

    sources, credentials = [], {}
    seen = set()
    for source in payload['sources']:
        if not isinstance(source, dict) or set(source) - BUNDLE_SOURCE_FIELDS or not {'id', 'name', 'kind', 'auth_mode', 'folder_id'} <= set(source):
            raise ValueError('Setup bundle source fields are invalid')
        source_id = _bundle_string(source['id'], 'source id', 64, r'[a-z0-9]{8,64}')
        if source_id in seen:
            raise ValueError('Setup bundle has duplicate source ids')
        seen.add(source_id)
        name = _bundle_string(source['name'], 'source name', 200)
        kind = _bundle_string(source['kind'], 'source kind', 32)
        auth_mode = _bundle_string(source['auth_mode'], 'source auth mode', 32)
        folder_id = _bundle_string(source['folder_id'], 'folder id', 256, r'[A-Za-z0-9_-]+')
        if kind != 'google_drive' or auth_mode not in ('api_key', 'service_account', 'oauth', 'public_link'):
            raise ValueError('Setup bundle source type is unsupported')
        email = source.get('service_account_email')
        if email is not None:
            email = _bundle_string(email, 'service account email', 320)
        sources.append({'id': source_id, 'name': name, 'kind': kind, 'auth_mode': auth_mode,
                        'folder_id': folder_id, 'service_account_email': email})

    for source_id, credential in payload['credentials'].items():
        _bundle_string(source_id, 'credential id', 64, r'[a-z0-9]{8,64}')
        if source_id not in seen or not isinstance(credential, dict) or not credential:
            raise ValueError('Setup bundle credentials are invalid')
        encoded = json.dumps(credential, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
        if len(encoded) > 65536:
            raise ValueError('Setup bundle credential exceeds the 64 KiB limit')
        credentials[source_id] = credential
    for source in sources:
        has_credential = source['id'] in credentials
        if (source['auth_mode'] == 'public_link') == has_credential:
            raise ValueError('Setup bundle source credential does not match its auth mode')

    settings = {}
    for key, value in payload['settings'].items():
        settings[key] = validate_setup_setting(key, value)
    return {'schema': BUNDLE_SCHEMA, 'sources': sources, 'credentials': credentials, 'settings': settings}


def _setup_db(path):
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sources (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, auth_mode TEXT,
              folder_id TEXT, status TEXT NOT NULL DEFAULT 'ready', video_count INTEGER NOT NULL DEFAULT 0,
              last_sync TEXT, error TEXT, credential_path TEXT, service_account_email TEXT,
              oauth_redirect_url TEXT, deleted INTEGER NOT NULL DEFAULT 0,
              sync_progress TEXT NOT NULL DEFAULT '{}'
            );
        ''')


def export_setup_bundle(envfile, bundle_file):
    values = verify_installation(envfile)
    destination = _bundle_path(bundle_file, output=True)
    data = Path(values['DT_HOST_DATA_DIR'])
    database = data / 'db' / 'drivetheater.sqlite3'
    sources, credentials, settings = [], {}, {}
    if database.exists():
        _private_regular(database, 'Installation database', require_private=False)
        with sqlite3.connect(database) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'sources' in tables:
                rows = db.execute('SELECT id,name,kind,auth_mode,folder_id,credential_path,service_account_email FROM sources WHERE deleted=0').fetchall()
                for source_id, name, kind, auth_mode, folder_id, credential_path, email in rows:
                    source = {'id': source_id, 'name': name, 'kind': kind, 'auth_mode': auth_mode,
                              'folder_id': folder_id, 'service_account_email': email}
                    # Validate before writing: no imported credential path can escape config.
                    expected = data / 'config' / (source_id + '.json')
                    if credential_path:
                        credential = expected if credential_path == f'/data/config/{source_id}.json' else Path(credential_path)
                        if credential != expected or credential.is_symlink():
                            raise ValueError('Source credential path is not managed by this installation')
                        _private_regular(credential, 'Source credential')
                        try:
                            parsed = json.loads(credential.read_text(encoding='utf-8'))
                        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                            raise ValueError('Source credential is not JSON') from exc
                        source['id'] = _bundle_string(source['id'], 'source id', 64, r'[a-z0-9]{8,64}')
                        credentials[source_id] = parsed
                    sources.append(source)
            if 'settings' in tables:
                settings = dict(db.execute('SELECT key,value FROM settings WHERE key IN ({})'.format(','.join('?' * len(BUNDLE_SETTINGS))), tuple(BUNDLE_SETTINGS)))
    bundle = read_setup_bundle_data({'schema': BUNDLE_SCHEMA, 'sources': sources, 'credentials': credentials, 'settings': settings})
    exclusive(destination, json.dumps(bundle, sort_keys=True, separators=(',', ':')).encode('utf-8'))
    return destination


def read_setup_bundle_data(payload):
    """Validate generated data using the same bounded rules as an imported bundle."""
    # A temporary in-memory validation avoids accepting data the import path would reject.
    encoded = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)
    if len(encoded.encode('utf-8')) > BUNDLE_MAX_BYTES:
        raise ValueError('Setup bundle exceeds the 1 MiB limit')
    # Keep validation centralized without trusting a filename from exported source data.
    sources = payload.get('sources')
    if not isinstance(sources, list):
        raise ValueError('Setup bundle has an invalid schema')
    # Duplicate the pure parser's validation through a private, controlled file is unnecessary;
    # export uses the same constraints below to retain no paths or operational state.
    return _validate_setup_bundle_data(payload)


def _validate_setup_bundle_data(payload):
    """Pure equivalent of read_setup_bundle for data already decoded from JSON."""
    # Serialize into a private shape and apply the parser's checks without filesystem access.
    if not isinstance(payload, dict) or set(payload) != {'schema', 'sources', 'credentials', 'settings'}:
        raise ValueError('Setup bundle has an unexpected schema')
    if payload['schema'] != BUNDLE_SCHEMA or not isinstance(payload['sources'], list) or not isinstance(payload['credentials'], dict) or not isinstance(payload['settings'], dict):
        raise ValueError('Setup bundle has an invalid schema')
    # This mirrors read_setup_bundle after JSON decoding.
    if len(payload['sources']) > 100 or len(payload['credentials']) > 100 or len(payload['settings']) > len(BUNDLE_SETTINGS):
        raise ValueError('Setup bundle exceeds bounded limits')
    result_sources, result_credentials, result_settings, seen = [], {}, {}, set()
    for source in payload['sources']:
        if not isinstance(source, dict) or set(source) - BUNDLE_SOURCE_FIELDS or not {'id', 'name', 'kind', 'auth_mode', 'folder_id'} <= set(source):
            raise ValueError('Setup bundle source fields are invalid')
        source_id = _bundle_string(source['id'], 'source id', 64, r'[a-z0-9]{8,64}')
        if source_id in seen:
            raise ValueError('Setup bundle has duplicate source ids')
        seen.add(source_id)
        item = {'id': source_id, 'name': _bundle_string(source['name'], 'source name', 200),
                'kind': _bundle_string(source['kind'], 'source kind', 32),
                'auth_mode': _bundle_string(source['auth_mode'], 'source auth mode', 32),
                'folder_id': _bundle_string(source['folder_id'], 'folder id', 256, r'[A-Za-z0-9_-]+'),
                'service_account_email': source.get('service_account_email')}
        if item['kind'] != 'google_drive' or item['auth_mode'] not in ('api_key', 'service_account', 'oauth', 'public_link'):
            raise ValueError('Setup bundle source type is unsupported')
        if item['service_account_email'] is not None:
            item['service_account_email'] = _bundle_string(item['service_account_email'], 'service account email', 320)
        result_sources.append(item)
    for source_id, credential in payload['credentials'].items():
        _bundle_string(source_id, 'credential id', 64, r'[a-z0-9]{8,64}')
        if source_id not in seen or not isinstance(credential, dict) or not credential:
            raise ValueError('Setup bundle credentials are invalid')
        if len(json.dumps(credential, separators=(',', ':'), ensure_ascii=False).encode('utf-8')) > 65536:
            raise ValueError('Setup bundle credential exceeds the 64 KiB limit')
        result_credentials[source_id] = credential
    for source in result_sources:
        if (source['auth_mode'] == 'public_link') == (source['id'] in result_credentials):
            raise ValueError('Setup bundle source credential does not match its auth mode')
    for key, value in payload['settings'].items():
        result_settings[key] = validate_setup_setting(key, value)
    return {'schema': BUNDLE_SCHEMA, 'sources': result_sources, 'credentials': result_credentials, 'settings': result_settings}


def import_setup_bundle(bundle_file, data_dir, public_url, runtime_data_dir=None):
    bundle = read_setup_bundle(bundle_file)
    data = plain_path(str(data_dir))
    config_dir = data / 'config'
    database = data / 'db' / 'drivetheater.sqlite3'
    if not config_dir.is_dir() or config_dir.is_symlink() or database.is_symlink():
        raise ValueError('Fresh installation runtime directories are invalid')
    created = []
    try:
        for source_id, credential in bundle['credentials'].items():
            destination = config_dir / (source_id + '.json')
            exclusive(destination, json.dumps(credential, separators=(',', ':'), ensure_ascii=False).encode('utf-8'))
            created.append(destination)
        _setup_db(database)
        oauth_callback = public_url.rstrip('/') + '/api/sources/google/callback' if public_url else None
        with sqlite3.connect(database) as db:
            for source in bundle['sources']:
                credential_path = (f"{runtime_data_dir.rstrip('/')}/config/{source['id']}.json" if runtime_data_dir else str(config_dir / (source['id'] + '.json'))) if source['id'] in bundle['credentials'] else None
                redirect = oauth_callback if source['auth_mode'] == 'oauth' else None
                db.execute('INSERT INTO sources(id,name,kind,auth_mode,folder_id,credential_path,service_account_email,oauth_redirect_url) VALUES (?,?,?,?,?,?,?,?)',
                           (source['id'], source['name'], source['kind'], source['auth_mode'], source['folder_id'], credential_path,
                            source['service_account_email'], redirect))
            for key, value in bundle['settings'].items():
                db.execute('INSERT INTO settings(key,value) VALUES (?,?)', (key, value))
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    if os.name == 'posix' and os.geteuid() == 0:
        identity = config_dir.stat()
        for path in [*created, database]:
            os.chown(path, identity.st_uid, identity.st_gid)
            os.chmod(path, 0o600)
    return len(bundle['sources'])


def verify_installation(envfile):
    values = read_env(envfile)
    data = plain_path(values['DT_HOST_DATA_DIR'])
    marker = data / '.drivetheater-install.json'
    if not data.is_dir() or not marker.is_file() or marker.is_symlink():
        raise ValueError('Original installation data and marker must be present')
    identity = json.loads(marker.read_text())
    if identity.get('application') != 'DriveTheater' or identity.get('data_dir') != data.as_posix():
        raise ValueError('Installation marker identity differs')
    for name in ('db', 'config', 'cache', 'backups'):
        path = data / name
        if not path.is_dir() or path.is_symlink():
            raise ValueError('Original runtime directories must be present and not symlinks')
    return values


def backup(envfile, revision='unknown'):
    values = verify_installation(envfile)
    data = Path(values['DT_HOST_DATA_DIR'])
    destination = data / 'backups' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + secrets.token_hex(4))
    destination.mkdir(mode=0o700)
    exclusive(destination / 'install.env', envfile.read_bytes())
    db = data / 'db' / 'drivetheater.sqlite3'
    if db.exists():
        if db.is_symlink():
            raise ValueError('Database path must not be a symlink')
        snapshot = destination / 'drivetheater.sqlite3'
        with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as source:
            with closing(sqlite3.connect(snapshot)) as target:
                source.backup(target)
                # A source using WAL can copy that mode into the destination. Materialize
                # a closed standalone database before hashing; never archive live sidecars.
                target.commit()
                target.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                mode = target.execute('PRAGMA journal_mode=DELETE').fetchone()[0]
                if str(mode).lower() != 'delete':
                    raise ValueError('Database snapshot could not be materialized')
                if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('Database backup integrity check failed')
        if any(destination.glob('drivetheater.sqlite3-*')):
            raise ValueError('Database snapshot contains unexpected sidecars')
        os.chmod(snapshot, 0o600)
    configs = data / 'config'
    if configs.is_dir() and not configs.is_symlink():
        with tarfile.open(destination / 'config.tar.gz', 'w:gz', dereference=False) as archive:
            for entry in configs.rglob('*'):
                if entry.is_symlink() or not entry.is_file():
                    continue
                archive.add(entry, arcname=str(entry.relative_to(data)), recursive=False)
            artwork = data / 'artwork'
            if artwork.exists() or artwork.is_symlink():
                if not artwork.is_dir() or artwork.is_symlink():
                    raise ValueError('Artwork storage must be a plain directory')
                for entry in artwork.iterdir():
                    info = entry.lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not re.fullmatch(r'[a-f0-9]{64}\.png', entry.name):
                        raise ValueError('Artwork contains an unsafe or incomplete file; retry after uploads finish')
                    archive.add(entry, arcname='artwork/' + entry.name, recursive=False)
        os.chmod(destination / 'config.tar.gz', 0o600)
    files = {item.name: hashlib.sha256(item.read_bytes()).hexdigest() for item in destination.iterdir() if item.is_file()}
    exclusive(destination / 'manifest.json', json.dumps({'revision': revision, 'files': files, 'media_included': False}, indent=2).encode())
    if db.exists():
        with closing(sqlite3.connect(db.as_uri() + '?mode=rw', uri=True, timeout=15)) as live:
            if live.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='backup_records'").fetchone():
                with live:
                    live.execute('INSERT INTO backup_records(id,created_at,encrypted,status,revision) VALUES(?,?,0,?,?)', (destination.name, int(datetime.now(timezone.utc).timestamp()), 'created', str(revision)[:128]))
    return destination


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('init', 'verify', 'field', 'credentials', 'backup', 'export-setup', 'import-setup'))
    parser.add_argument('--env-file', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--uid', type=int)
    parser.add_argument('--gid', type=int)
    parser.add_argument('--port', type=int, default=80)
    parser.add_argument('--bind', default='0.0.0.0')
    parser.add_argument('--public-url', default='')
    parser.add_argument('--field', choices=PUBLIC_FIELDS)
    parser.add_argument('--revision', default='unknown')
    parser.add_argument('--bundle-file', type=Path)
    args = parser.parse_args()
    if args.command == 'init':
        values, created = initialize(args.env_file, args.data_dir, args.uid, args.gid, args.port, args.bind, args.public_url)
        print('Created new private configuration' if created else 'Preserved existing configuration and credentials')
    elif args.command == 'verify':
        verify_installation(args.env_file)
        print('Installation identity verified')
    elif args.command == 'field':
        if not args.field:
            parser.error('--field required')
        print(read_env(args.env_file).get(args.field, ''))
    elif args.command == 'credentials':
        values = read_env(args.env_file)
        print('Initial username: ' + values['DT_ADMIN_USER'])
        print('Initial password: ' + values['DT_ADMIN_PASSWORD'])
        print('After changing the password in the app, the original bootstrap password no longer logs in.')
    elif args.command == 'export-setup':
        if not args.bundle_file:
            parser.error('--bundle-file required')
        print(export_setup_bundle(args.env_file, args.bundle_file))
    elif args.command == 'import-setup':
        if not args.bundle_file or not args.data_dir:
            parser.error('--bundle-file and --data-dir required')
        print(import_setup_bundle(args.bundle_file, args.data_dir, args.public_url, runtime_data_dir='/data'))
    else:
        print(backup(args.env_file, args.revision))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        print('Installer configuration check failed. Existing configuration/data were not replaced. Review paths, permissions and installation identity.', file=sys.stderr)
        sys.exit(1)
