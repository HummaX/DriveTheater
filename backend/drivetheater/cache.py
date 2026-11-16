"""Local cache eviction that keeps active media and jobs safe."""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from .db import utcnow


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _under_cache(path: Path, cache_root: Path) -> bool:
    try:
        path.resolve().relative_to(cache_root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _cache_bytes(cache_root: Path) -> int:
    try:
        return sum(item.stat().st_size for item in cache_root.rglob("*") if item.is_file())
    except OSError:
        return 0


def automatic_cleanup(db, data_dir: Path, needed_bytes: int = 0, exclude_video_id: str | None = None) -> int:
    """Evict old inactive cached media, returning the number of bytes released.

    Rows are reserved within a write transaction before their directories are removed,
    so concurrent workers cannot select or start using the same item.
    """
    data_dir = Path(data_dir)
    cache_root = data_dir / "cache"
    needed_bytes = max(0, int(needed_bytes))
    with db.connect() as conn:
        settings = {row["key"]: row["value"] for row in conn.execute(
            "SELECT key,value FROM settings WHERE key IN ('auto_cache_cleanup','cache_limit_gb','cache_cleanup_target_percent')"
        )}
    if not _enabled(settings.get("auto_cache_cleanup")):
        return 0
    try:
        limit = max(0, int(float(settings.get("cache_limit_gb", "0")) * 1024 ** 3))
        target = min(100, max(1, int(float(settings.get("cache_cleanup_target_percent", "90")))))
    except ValueError:
        return 0
    if not limit:
        return 0
    keep_at_most = limit * target // 100
    current = _cache_bytes(cache_root)
    required = max(0, current + needed_bytes - keep_at_most)
    if not required:
        return 0

    now = int(time.time())
    with db.transaction() as conn:
        query = """
            SELECT v.id,v.state,v.asset_dir,v.local_path,v.size_bytes,v.watch_position,v.last_played
            FROM videos v
            WHERE v.id != ? AND v.state IN ('cached','playable','cancelled','failed')
              AND COALESCE(v.active_until,0) < ?
              AND NOT EXISTS (
                SELECT 1 FROM jobs j WHERE j.video_id=v.id
                  AND j.state IN ('queued','downloading','preparing')
              )
            ORDER BY CASE WHEN COALESCE(v.watch_position,0)>0 THEN 0 ELSE 1 END,
                     CASE WHEN v.last_played IS NULL THEN 0 ELSE 1 END,
                     v.last_played ASC, v.updated_at ASC
        """
        candidates = list(conn.execute(query, (exclude_video_id or "", now)))
        selected, planned = [], 0
        for row in candidates:
            selected.append(dict(row))
            planned += max(0, int(row["size_bytes"] or 0))
            if planned >= required:
                break
        if not selected:
            return 0
        ids = [row["id"] for row in selected]
        conn.executemany("UPDATE videos SET state='removing',updated_at=? WHERE id=?", [(utcnow(), item) for item in ids])

    freed = 0
    successful: list[str] = []
    for row in selected:
        paths = {Path(value) for value in (row["asset_dir"], row["local_path"]) if value}
        roots = {path if path.is_dir() else path.parent for path in paths}
        removable = [root for root in roots if _under_cache(root, cache_root)]
        try:
            before = sum(_cache_bytes(root) if root.is_dir() else 0 for root in removable)
            for root in removable:
                if root.exists():
                    shutil.rmtree(root)
            # Account by observed global change so unknown asset filenames are safe.
            after = sum(_cache_bytes(root) if root.exists() and root.is_dir() else 0 for root in removable)
            freed += max(0, before - after)
            successful.append(row["id"])
        except OSError:
            continue
    with db.transaction() as conn:
        if successful:
            conn.executemany(
                "UPDATE videos SET state='cloud',local_path=NULL,asset_dir=NULL,playback_mode=NULL,playback_url=NULL,"
                "playback_until=NULL,size_bytes=NULL,bytes_downloaded=0,updated_at=? WHERE id=?",
                [(utcnow(), item) for item in successful],
            )
        reserved = [row for row in selected if row["id"] not in successful]
        if reserved:
            conn.executemany("UPDATE videos SET state=?,updated_at=? WHERE id=?",
                             [(row["state"], utcnow(), row["id"]) for row in reserved])
    return freed
