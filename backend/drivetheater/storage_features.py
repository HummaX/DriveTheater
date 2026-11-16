"""Explicit storage policies, snapshot cleanup plans and bounded integrity checks."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import time
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

from .cache_safety import (CacheConflict, CacheManager, artifact_roots, file_identity,
                    inode_key, row_snapshot, safe_path, safe_walk)

from .diagnostics import record_sample

KINDS = ('original', 'derived', 'previews', 'captions')
SHA256 = re.compile(r'[a-fA-F0-9]{64}\Z')


def artifact_kind(path: str, originals: set[str]) -> str:
    item = Path(path)
    if path in originals or item.name in {'source.bin', 'source.bin.part'}:
        return 'original'
    if item.name in {'poster.jpg', 'preview.mp4', 'sprites.vtt', 'sprites.jpg'} or item.name.startswith('sprite') and item.suffix in {'.jpg', '.png'}:
        return 'previews'
    if item.suffix.lower() in {'.srt', '.vtt', '.ass', '.ssa'} or item.name.startswith(('subtitle-', 'caption-', 'captions.')) or any(part in {'captions', 'subtitles'} for part in item.parts):
        return 'captions'
    return 'derived'


def summarize(files: list[dict]) -> dict:
    unique = {inode_key(item): item for item in files}
    counts = Counter(inode_key(item) for item in files)
    return {'logical_bytes': sum(item['size'] for item in files),
            'unique_bytes': sum(item['size'] for item in unique.values()),
            'physical_bytes': sum(item['physical'] for item in unique.values()),
            'reclaimable_bytes': sum(item['physical'] for key, item in unique.items() if item['links'] <= counts[key]),
            'shared_inode_count': sum(item['links'] > 1 for item in unique.values()),
            'file_count': len(files)}


def storage_maintenance(db, data_dir: Path, *, now: int | None = None, limit: int = 20) -> dict:
    """Worker/operator entry point; call only from an explicit maintenance tick."""
    return StorageService(db, Path(data_dir) / 'cache').maintenance(now=now, limit=limit)


class StorageService:
    def __init__(self, db, media_dir: Path, cache_manager: CacheManager | None = None):
        self.db, self.media_dir = db, Path(os.path.abspath(media_dir))
        self.cache = cache_manager or CacheManager(db, self.media_dir.parent)
        if Path(os.path.abspath(self.cache.root)) != self.media_dir:
            raise ValueError('Cache manager root must match the configured media directory')

    def inventory(self) -> dict:
        files = safe_walk(self.media_dir, self.media_dir)
        with self.db.connect() as conn:
            rows = [dict(row) for row in conn.execute('SELECT * FROM videos WHERE local_path IS NOT NULL OR asset_dir IS NOT NULL ORDER BY id LIMIT 2001')]
            if len(rows) > 2000:
                raise CacheConflict('Cached title scan limit exceeded')
            running = {row[0] for row in conn.execute("SELECT DISTINCT video_id FROM jobs WHERE state IN ('queued','downloading','preparing')")}
        originals, owners, row_files = set(), {}, {}
        for row in rows:
            if row['local_path']:
                try:
                    originals.add(str(safe_path(Path(row['local_path']), self.media_dir).relative_to(self.media_dir)))
                except FileNotFoundError:
                    pass
            roots = artifact_roots(row, self.media_dir)
            owned = [item for item in files if any(self.media_dir / item['path'] == root or root in (self.media_dir / item['path']).parents for root in roots)]
            row_files[row['id']] = owned
            for item in owned:
                owners.setdefault(item['path'], set()).add(row['id'])
        artifacts, charged = {}, set()
        for kind in KINDS:
            category = [item for item in files if artifact_kind(item['path'], originals) == kind]
            artifacts[kind] = summarize(category)
            unique = {inode_key(item): item for item in category}
            artifacts[kind]['physical_bytes'] = sum(item['physical'] for key, item in unique.items() if key not in charged)
            charged.update(unique)
        hashes = Counter(row['content_sha256'] for row in rows if row['content_sha256'])
        items = []
        for row in rows:
            owned = row_files[row['id']]
            sizes = {kind: sum(item['size'] for item in owned if artifact_kind(item['path'], originals) == kind) for kind in KINDS}
            totals = summarize(owned)
            # A shared path cannot be reclaimed by removing this row alone.
            if any(len(owners[item['path']]) > 1 for item in owned):
                totals['reclaimable_bytes'] = 0
            items.append({'video_id': row['id'], 'title': row['title'], 'source_id': row['source_id'],
                          'artifacts': sizes, **totals,
                          'active': row['active_until'] >= time.time() or row['id'] in running,
                          'shared_hash_count': hashes.get(row['content_sha256'], 0)})
        return {'totals': summarize(files), 'artifacts': artifacts, 'items': items,
                'physical_measurement': 'allocated_blocks' if any(hasattr((self.media_dir / item['path']).lstat(), 'st_blocks') for item in files[:1]) else 'file_size_fallback',
                'note': 'Logical sizes count paths; physical sizes charge unique inodes once, preferring originals. Reclaim estimates exclude links outside the selected files; open OS handles can delay actual space release.'}

    def policies(self) -> list[dict]:
        with self.db.connect() as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM cache_policies ORDER BY scope_kind,scope_id')]

    def set_policy(self, scope_kind: str, scope_id: str, keep_days: int | None = None, delete_after_watch: bool = False) -> dict:
        if scope_kind not in ('source', 'title') or not isinstance(scope_id, str) or not 1 <= len(scope_id) <= 128:
            raise ValueError('Choose a source or title policy')
        if keep_days is not None and (type(keep_days) is not int or keep_days not in (7, 30, 90)):
            raise ValueError('Retention must be 7, 30, 90 days or off')
        if type(delete_after_watch) is not bool:
            raise ValueError('Delete after watch must be a boolean')
        table = 'sources' if scope_kind == 'source' else 'videos'
        with self.db.transaction() as conn:
            if not conn.execute(f'SELECT 1 FROM {table} WHERE id=?', (scope_id,)).fetchone():
                raise ValueError('Policy target is unavailable')
            conn.execute('INSERT INTO cache_policies(scope_kind,scope_id,keep_days,delete_after_watch) VALUES(?,?,?,?) ON CONFLICT(scope_kind,scope_id) DO UPDATE SET keep_days=excluded.keep_days,delete_after_watch=excluded.delete_after_watch', (scope_kind, scope_id, keep_days, int(delete_after_watch)))
        return {'scope_kind': scope_kind, 'scope_id': scope_id, 'keep_days': keep_days, 'delete_after_watch': delete_after_watch}

    def create_cleanup_plan(self, username: str, video_ids: list[str], *, ttl_seconds: int = 300) -> dict:
        if not isinstance(video_ids, list) or not 1 <= len(video_ids) <= 100 or any(not isinstance(item, str) or not 1 <= len(item) <= 128 for item in video_ids):
            raise ValueError('Choose between one and 100 cached titles')
        snapshots, names, files = {}, [], {}
        with self.db.transaction() as conn:
            for video_id in sorted(set(video_ids)):
                row = conn.execute('SELECT * FROM videos WHERE id=?', (video_id,)).fetchone()
                if not row or row['state'] not in ('cached', 'playable', 'failed', 'cancelled', 'cloud', 'removed') or not (row['local_path'] or row['asset_dir']):
                    raise CacheConflict('Cached title is unavailable')
                snapshots[video_id] = self.cache.snapshot(row, conn)
                self.cache.guard_snapshot(conn, row, snapshots[video_id], set(video_ids))
                names.append({'video_id': video_id, 'title': row['title']})
                files.update({item['path']: item for item in snapshots[video_id]['files']})
                if len(files) > 100000:
                    raise CacheConflict('Cleanup file limit exceeded')
            body = json.dumps(snapshots)
            if len(body) > 16 * 1024 * 1024:
                raise CacheConflict('Cleanup snapshot is too large; choose fewer titles')
            now, plan_id = int(time.time()), uuid.uuid4().hex
            expiry = now + max(1, min(600, int(ttl_seconds)))
            conn.execute('DELETE FROM cleanup_plans WHERE expires_at<=? OR consumed=1', (now,))
            conn.execute('INSERT INTO cleanup_plans(id,username,body,created_at,expires_at) VALUES(?,?,?,?,?)', (plan_id, username, body, now, expiry))
            conn.execute('DELETE FROM cleanup_plans WHERE username=? AND id NOT IN (SELECT id FROM cleanup_plans WHERE username=? ORDER BY rowid DESC LIMIT 20)', (username, username))
            conn.execute('DELETE FROM cleanup_plans WHERE id NOT IN (SELECT id FROM cleanup_plans ORDER BY rowid DESC LIMIT 200)')
        return {'plan_id': plan_id, 'expires_at': expiry, 'items': names, **summarize(list(files.values()))}

    def execute_cleanup_plan(self, username: str, plan_id: str) -> dict:
        # Consuming is committed separately: even an invalidated attempted plan is single use.
        with self.db.transaction() as conn:
            row = conn.execute('SELECT * FROM cleanup_plans WHERE id=? AND username=? AND consumed=0 AND expires_at>?', (plan_id, username, int(time.time()))).fetchone()
            if not row:
                raise CacheConflict('Cleanup plan is unavailable or expired')
            conn.execute('UPDATE cleanup_plans SET consumed=1 WHERE id=?', (plan_id,))
            snapshots = json.loads(row['body'])
        started = time.monotonic()
        reservation = self.cache.reserve_many(list(snapshots), expected_snapshots=snapshots)
        result = self.cache.remove_reserved(reservation)
        record_sample(self.db, 'cleanup', {'elapsed_seconds': time.monotonic() - started,
                                          'files': result['files_removed'], 'reclaimed_bytes': result['freed_bytes']})
        return result

    def maintenance(self, *, now: int | None = None, limit: int = 20) -> dict:
        """Explicit operator/worker hook only. Reading or changing policies never deletes."""
        now = int(time.time()) if now is None else now
        limit = min(100, max(1, limit))
        policies = {(item['scope_kind'], item['scope_id']): item for item in self.policies()}
        with self.db.connect() as conn:
            rows = list(conn.execute("SELECT * FROM videos WHERE state IN ('cached','playable') ORDER BY last_played,updated_at LIMIT 10000"))
            watchers = {}
            for watched in conn.execute('SELECT video_id,watched FROM video_user_state'):
                watchers.setdefault(watched['video_id'], []).append(bool(watched['watched']))
        removed, freed, skipped = 0, 0, 0
        for row in rows:
            policy = policies.get(('title', row['id']), policies.get(('source', row['source_id'])))
            if not policy:
                continue
            try:
                reference = row['last_played'] or row['updated_at']
                last_used = datetime.fromisoformat(reference.replace('Z', '+00:00')).timestamp()
            except (ValueError, AttributeError):
                continue
            aged = policy['keep_days'] is not None and last_used <= now - policy['keep_days'] * 86400
            watched = policy['delete_after_watch'] and bool(watchers.get(row['id'])) and all(watchers[row['id']])
            if not (aged or watched):
                continue
            try:
                result = self.cache.remove_reserved(self.cache.reserve(row['id'], automatic=True))
                removed += result['removed']
                freed += result['freed_bytes']
            except (CacheConflict, OSError):
                skipped += 1
            if removed + skipped >= limit:
                break
        return {'removed': removed, 'skipped': skipped, 'freed_bytes': freed}

    def integrity_status(self, video_id: str) -> dict:
        with self.db.connect() as conn:
            if not conn.execute('SELECT 1 FROM videos WHERE id=?', (video_id,)).fetchone():
                raise ValueError('Title is unavailable')
            row = conn.execute('SELECT * FROM integrity_checks WHERE video_id=?', (video_id,)).fetchone()
        return dict(row) if row else {'video_id': video_id, 'status': 'not_checked', 'checked_at': None}

    def verify_integrity(self, video_id: str, *, provider_sha256: str | None = None,
                         max_bytes: int = 8 * 1024 ** 3, max_seconds: float = 120) -> dict:
        """Hash only on explicit invocation. Provider checksum is a trusted internal input.

        The HTTP action never accepts an expected checksum from the caller.
        """
        if provider_sha256 is not None and not SHA256.fullmatch(provider_sha256):
            raise ValueError('Provider checksum must be SHA256')
        with self.db.connect() as conn:
            row = conn.execute('SELECT * FROM videos WHERE id=?', (video_id,)).fetchone()
            if not row or not row['local_path'] or row['state'] not in ('cached', 'playable'):
                raise CacheConflict('A completed local original is required')
            if conn.execute("SELECT 1 FROM jobs WHERE video_id=? AND state IN ('queued','downloading','preparing')", (video_id,)).fetchone():
                raise CacheConflict('Wait until the ongoing job has finished')
            row = dict(row)
        path = safe_path(Path(row['local_path']), self.media_dir)
        before = file_identity(path, self.media_dir)
        if before['size'] > max_bytes:
            raise CacheConflict('Original exceeds the integrity verification size limit')
        digest, count, started = hashlib.sha256(), 0, time.monotonic()
        # no-follow plus post-open identity protects the final component; parent
        # paths are checked before/after and the opened inode is rechecked below.
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
        with os.fdopen(descriptor, 'rb') as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino, opened.st_size) != (before['dev'], before['ino'], before['size']):
                raise CacheConflict('Original changed before verification')
            while chunk := handle.read(1024 * 1024):
                count += len(chunk)
                if count > max_bytes or time.monotonic() - started > max_seconds:
                    raise CacheConflict('Integrity verification limit exceeded')
                digest.update(chunk)
            after_open = os.fstat(handle.fileno())
            if (after_open.st_size, after_open.st_mtime_ns, after_open.st_ctime_ns) != (before['size'], before['mtime_ns'], before['ctime_ns']):
                raise CacheConflict('Original changed during verification')
        if file_identity(path, self.media_dir) != before:
            raise CacheConflict('Original changed during verification')
        checksum = digest.hexdigest()
        expected = provider_sha256 or row['content_sha256']
        status = ('provider_verified' if provider_sha256 else 'local_match') if expected and hmac.compare_digest(checksum, expected.lower()) else 'mismatch' if expected else 'locally_computed'
        with self.db.transaction() as conn:
            current = conn.execute('SELECT * FROM videos WHERE id=?', (video_id,)).fetchone()
            if not current or row_snapshot(current) != row_snapshot(row):
                raise CacheConflict('Original changed during verification')
            conn.execute('INSERT INTO integrity_checks(video_id,checksum,status,checked_at,expected_checksum) VALUES(?,?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET checksum=excluded.checksum,status=excluded.status,checked_at=excluded.checked_at,expected_checksum=excluded.expected_checksum', (video_id, checksum, status, int(time.time()), expected))
        record_sample(self.db, 'integrity', {'elapsed_seconds': time.monotonic() - started, 'bytes': count})
        return self.integrity_status(video_id)
