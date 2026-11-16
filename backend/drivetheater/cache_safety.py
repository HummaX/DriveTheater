"""Bounded no-follow cache walks and transactional removal reservations."""
from __future__ import annotations

import os
import stat
import time
from pathlib import Path

from .db import utcnow

MAX_FILES = 100_000
ROW_IDENTITY = ('id', 'state', 'local_path', 'asset_dir', 'content_sha256', 'duplicate_of', 'updated_at')


class CacheConflict(ValueError):
    pass


def _enabled(value):
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on'}


def safe_path(path: Path, root: Path) -> Path:
    """Validate every lexical component without resolving/following links."""
    root, path = Path(os.path.abspath(root)), Path(os.path.abspath(path))
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise CacheConflict('Artifact is outside the media directory') from None
    current = Path(root.anchor)
    for component in (*root.parts[1:], *relative.parts):
        current /= component
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(current, 'is_junction', lambda: False)():
            raise CacheConflict('Linked artifact paths are not supported')
    return path


def file_identity(path: Path, root: Path) -> dict:
    path = safe_path(path, root)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise CacheConflict('Artifact is not a regular file')
    return {'path': str(path.relative_to(Path(os.path.abspath(root)))), 'dev': info.st_dev,
            'ino': info.st_ino, 'size': info.st_size, 'mtime_ns': info.st_mtime_ns,
            'ctime_ns': info.st_ctime_ns, 'links': info.st_nlink,
            'physical': info.st_blocks * 512 if hasattr(info, 'st_blocks') else info.st_size}


def inode_key(item: dict):
    return (item['dev'], item['ino']) if item['ino'] else item['path']


def safe_walk(path: Path, root: Path, maximum: int = MAX_FILES) -> list[dict]:
    files, pending, seen = [], [safe_path(path, root)], 0
    while pending:
        item = safe_path(pending.pop(), root)
        info = item.lstat()
        seen += 1
        if seen > maximum:
            raise CacheConflict('Artifact scan limit exceeded')
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(item) as entries:
                for entry in entries:
                    if len(pending) + seen >= maximum:
                        raise CacheConflict('Artifact scan limit exceeded')
                    pending.append(Path(entry.path))
        elif stat.S_ISREG(info.st_mode):
            files.append(file_identity(item, root))
        else:
            raise CacheConflict('Unsupported artifact type')
    return sorted(files, key=lambda item: item['path'])


def artifact_roots(row, root: Path) -> list[Path]:
    paths = []
    for name in ('local_path', 'asset_dir'):
        if row[name]:
            try:
                path = safe_path(Path(row[name]), root)
            except FileNotFoundError:
                continue  # Recover safely from interrupted/partial removals.
            if path == Path(os.path.abspath(root)):
                raise CacheConflict('The media root cannot be removed')
            paths.append(path)
    result = []
    for path in sorted(set(paths), key=lambda item: len(item.parts)):
        if not any(path == parent or parent in path.parents for parent in result):
            result.append(path)
    return result


def row_snapshot(row) -> dict:
    return {key: row[key] for key in ROW_IDENTITY}


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _windows_unlink(root: Path, item: dict) -> int:
    """Delete the checked Windows file handle, not a re-resolved pathname."""
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    final_path = kernel.GetFinalPathNameByHandleW
    final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    final_path.restype = wintypes.DWORD
    disposition = kernel.SetFileInformationByHandle
    disposition.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    disposition.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    path = safe_path(root / item['path'], root)
    # DELETE + GENERIC_READ, shared read/write/delete, OPEN_EXISTING,
    # FILE_FLAG_OPEN_REPARSE_POINT (do not follow a final symlink).
    handle = create(str(path), 0x10000 | 0x80000000, 7, None, 3, 0x00200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    descriptor = None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = final_path(handle, buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise CacheConflict('Cannot establish artifact handle location')
        location = buffer.value
        if location.startswith('\\\\?\\UNC\\'):
            location = '\\\\' + location[8:]
        elif location.startswith('\\\\?\\'):
            location = location[4:]
        if Path(location) != path:
            raise CacheConflict('Artifact handle resolved through a linked path')
        safe_path(Path(location), root)
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (item['dev'], item['ino'], item['size'], item['mtime_ns'], item['ctime_ns']):
            raise CacheConflict('Artifact changed after reservation')
        delete = ctypes.c_ubyte(1)  # FILE_DISPOSITION_INFO.DeleteFile is BOOLEAN.
        if not disposition(handle, 4, ctypes.byref(delete), ctypes.sizeof(delete)):
            raise ctypes.WinError(ctypes.get_last_error())
        return item['physical'] if info.st_nlink == 1 else 0
    finally:
        if descriptor is not None:
            os.close(descriptor)  # Owns and closes the underlying handle.
        else:
            close(handle)


def _safe_unlink(root: Path, item: dict) -> int:
    """Delete a checked handle, never recursively follow artifact paths."""
    if os.name == 'nt':
        return _windows_unlink(root, item)
    if not (hasattr(os, 'O_NOFOLLOW') and os.open in os.supports_dir_fd and os.unlink in os.supports_dir_fd):
        raise CacheConflict('Safe removal is unavailable on this runtime')
    path = safe_path(root / item['path'], root)
    if file_identity(path, root) != item:
        raise CacheConflict('Artifact changed after reservation')
    descriptors = []
    try:
        descriptor = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(descriptor)
        # Pin root ancestors too: opening the root by absolute path could follow
        # an ancestor swapped to a symlink after safe_path returned.
        for part in root.parts[1:]:
            descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            descriptors.append(descriptor)
        parts = Path(item['path']).parts
        for part in parts[:-1]:
            descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            descriptors.append(descriptor)
        info = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (item['dev'], item['ino'], item['size'], item['mtime_ns'], item['ctime_ns']):
            raise CacheConflict('Artifact changed after reservation')
        os.unlink(parts[-1], dir_fd=descriptor)
        return item['physical'] if info.st_nlink == 1 else 0
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


class CacheManager:
    def __init__(self, db, data_dir: Path):
        self.db, self.root = db, Path(data_dir) / 'cache'

    def snapshot(self, row, conn=None) -> dict:
        roots = artifact_roots(row, self.root)
        files = {item['path']: item for path in roots for item in safe_walk(path, self.root)}
        inodes = {inode_key(item) for item in files.values()}

        def references(connection):
            peers, scanned = [], len(files)
            for peer in connection.execute('SELECT * FROM videos WHERE id!=? AND (local_path IS NOT NULL OR asset_dir IS NOT NULL)', (row['id'],)):
                same_hash = bool(row['content_sha256'] and row['content_sha256'] == peer['content_sha256'])
                peer_paths = [Path(os.path.abspath(peer[key])) for key in ('local_path', 'asset_dir') if peer[key]]
                overlaps = any(_overlap(left, right) for left in roots for right in peer_paths)
                peer_files = []
                if not same_hash and not overlaps:
                    try:
                        for path in artifact_roots(peer, self.root):
                            peer_files.extend(safe_walk(path, self.root, MAX_FILES - scanned))
                            scanned += len(peer_files)
                            if scanned > MAX_FILES:
                                raise CacheConflict('Artifact scan limit exceeded')
                    except FileNotFoundError:
                        pass
                if same_hash or overlaps or any(inode_key(item) in inodes for item in peer_files):
                    peers.append(row_snapshot(peer))
            return sorted(peers, key=lambda item: item['id'])
        if conn is None:
            with self.db.connect() as connection:
                peers = references(connection)
        else:
            peers = references(conn)
        return {'row': row_snapshot(row), 'files': sorted(files.values(), key=lambda item: item['path']), 'peers': peers}

    def guard_snapshot(self, conn, row, snapshot: dict, selected_ids: set[str]) -> None:
        """Called under a DB transaction for both preview and final reservation."""
        guarded = {row['id'], *(peer['id'] for peer in snapshot['peers'])}
        for guarded_id in guarded:
            active = conn.execute('SELECT active_until FROM videos WHERE id=?', (guarded_id,)).fetchone()
            job = conn.execute("SELECT 1 FROM jobs WHERE video_id=? AND state IN ('queued','downloading','preparing')", (guarded_id,)).fetchone()
            if (active and active[0] >= time.time()) or job:
                raise CacheConflict('Active media or an ongoing job cannot be removed')
        roots = artifact_roots(row, self.root)
        for peer in snapshot['peers']:
            if peer['id'] not in selected_ids and any(_overlap(left, Path(os.path.abspath(peer[key]))) for left in roots for key in ('local_path', 'asset_dir') if peer[key]):
                raise CacheConflict('Another title references these artifact paths')

    def reserve(self, video_id: str, *, automatic: bool = False, expected: dict | None = None):
        return self.reserve_many([video_id], automatic=automatic,
                                 expected_snapshots={video_id: expected} if expected is not None else None)

    def reserve_many(self, video_ids: list[str], *, automatic: bool = False, expected_snapshots: dict | None = None):
        ids = set(video_ids)
        if not ids or len(ids) > 100:
            raise CacheConflict('Choose between one and 100 cached titles')
        with self.db.transaction() as conn:
            rows, files, roots = [], {}, set()
            for video_id in sorted(ids):
                found = conn.execute('SELECT * FROM videos WHERE id=?', (video_id,)).fetchone()
                if not found:
                    raise CacheConflict('Cached title is unavailable')
                row = dict(found)
                if row['state'] == 'removing' or automatic and row['state'] not in ('cached', 'playable'):
                    raise CacheConflict('Cached title is unavailable')
                snapshot = self.snapshot(row, conn)
                if expected_snapshots is not None and snapshot != expected_snapshots.get(video_id):
                    raise CacheConflict('Cleanup preview is stale; preview again')
                self.guard_snapshot(conn, row, snapshot, ids)
                item_roots = artifact_roots(row, self.root)
                roots.update(item_roots)
                files.update({item['path']: item for item in snapshot['files']})
                rows.append(row)
            conn.executemany("UPDATE videos SET state='removing',local_path=NULL,asset_dir=NULL,playback_mode=NULL,playback_url=NULL,playback_until=NULL,updated_at=? WHERE id=?", [(utcnow(), row['id']) for row in rows])
        return {'rows': rows, 'files': sorted(files.values(), key=lambda item: item['path']), 'roots': roots}

    def remove_reserved(self, reservation) -> dict:
        removed, freed, content_freed = 0, 0, 0
        try:
            current = {item['path']: item for root in reservation['roots'] for item in safe_walk(root, self.root)}
            if sorted(current.values(), key=lambda item: item['path']) != reservation['files']:
                raise CacheConflict('Artifacts changed after reservation')
            for item in reservation['files']:
                # Unlinking one hard link changes ctime/nlink on its siblings.
                current_item = file_identity(self.root / item['path'], self.root)
                if any(current_item[key] != item[key] for key in ('dev', 'ino', 'size', 'mtime_ns')):
                    raise CacheConflict('Artifact changed after reservation')
                freed += _safe_unlink(self.root, current_item)
                content_freed += current_item['size'] if current_item['links'] == 1 else 0
                removed += 1
        except Exception:
            with self.db.transaction() as conn:
                for row in reservation['rows']:
                    if not removed:
                        conn.execute("UPDATE videos SET state=?,local_path=?,asset_dir=?,playback_mode=?,playback_url=?,playback_until=?,updated_at=? WHERE id=? AND state='removing'", (row['state'], row['local_path'], row['asset_dir'], row['playback_mode'], row['playback_url'], row['playback_until'], utcnow(), row['id']))
                    else:
                        conn.execute("UPDATE videos SET state='failed',local_path=?,asset_dir=?,content_sha256=NULL,duplicate_of=NULL,error='Cache removal was interrupted; download again',updated_at=? WHERE id=? AND state='removing'", (row['local_path'], row['asset_dir'], utcnow(), row['id']))
                        self._invalidate(conn, row['id'])
            raise
        with self.db.transaction() as conn:
            for row in reservation['rows']:
                conn.execute("UPDATE videos SET state=?,size_bytes=NULL,bytes_downloaded=0,content_sha256=NULL,duplicate_of=NULL,audio_tracks='[]',subtitle_tracks='[]',caption_status='none',caption_error=NULL,error=NULL,updated_at=? WHERE id=? AND state='removing'", ('removed' if row['removed_from_source'] else 'cloud', utcnow(), row['id']))
                self._invalidate(conn, row['id'])
        # Leave empty directories: no recursive removal or following of links.
        return {'status': 'removed', 'removed': len(reservation['rows']), 'files_removed': removed,
                'reclaimed_bytes': freed, 'freed_bytes': freed, 'reclaimed_content_bytes': content_freed}

    @staticmethod
    def _invalidate(conn, video_id):
        conn.execute('UPDATE videos SET duplicate_of=NULL WHERE duplicate_of=?', (video_id,))
        # Feature tables may not exist on a pre-migration runtime.
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ('integrity_checks', 'probe_diagnostics', 'caption_cues'):
            if table in tables:
                conn.execute(f'DELETE FROM {table} WHERE video_id=?', (video_id,))


def automatic_cleanup(db, data_dir: Path, needed_bytes: int = 0, exclude_video_id: str | None = None) -> int:
    """Legacy capacity cleanup through the same reservations as manual cleanup."""
    manager = CacheManager(db, data_dir)
    with db.connect() as conn:
        settings = {row['key']: row['value'] for row in conn.execute("SELECT key,value FROM settings WHERE key IN ('auto_cache_cleanup','cache_limit_gb','cache_cleanup_target_percent')")}
        candidates = list(conn.execute("SELECT * FROM videos WHERE id!=? AND state IN ('cached','playable','cancelled','failed') ORDER BY COALESCE(watch_position,0)>0 DESC,last_played IS NOT NULL,last_played,updated_at", (exclude_video_id or '',)))
    if not _enabled(settings.get('auto_cache_cleanup')):
        return 0
    try:
        limit = int(float(settings.get('cache_limit_gb', '0')) * 1024 ** 3)
        target = min(100, max(1, int(float(settings.get('cache_cleanup_target_percent', '90')))))
        files = safe_walk(manager.root, manager.root)
    except (OSError, ValueError, OverflowError):
        return 0
    unique = {inode_key(item): item for item in files}
    # Preserve the configured content-size budget, deduplicated by inode.
    # The return value still reports physical blocks actually unlinked.
    content = sum(item['size'] for item in unique.values())
    required = max(0, content + max(0, int(needed_bytes)) - limit * target // 100) if limit > 0 else 0
    freed, content_freed = 0, 0
    for row in candidates:
        if content_freed >= required:
            break
        try:
            result = manager.remove_reserved(manager.reserve(row['id']))
            freed += result['freed_bytes']
            content_freed += result['reclaimed_content_bytes']
        except (OSError, CacheConflict):
            continue
    return freed
