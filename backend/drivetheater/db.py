from __future__ import annotations

import contextlib
import json
import math
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterator


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _initial_cache_limit_gb() -> str:
    """Return a safe initial cache limit without changing existing settings."""
    raw = os.environ.get("DT_CACHE_LIMIT_GB", "20")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return "20"
    if not math.isfinite(value) or value <= 0:
        return "20"
    return format(value, "g")


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Open, commit/roll back, and always close a short-lived SQLite connection."""
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            yield conn
            if conn.in_transaction:
                conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS users (
                  username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                  token_hash TEXT PRIMARY KEY, username TEXT NOT NULL REFERENCES users(username), expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                  key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sources (
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, auth_mode TEXT,
                  folder_id TEXT, status TEXT NOT NULL DEFAULT 'ready', video_count INTEGER NOT NULL DEFAULT 0,
                  last_sync TEXT, error TEXT, credential_path TEXT, service_account_email TEXT,
                  oauth_redirect_url TEXT, deleted INTEGER NOT NULL DEFAULT 0,
                  sync_progress TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS videos (
                  id TEXT PRIMARY KEY, title TEXT NOT NULL, source_id TEXT REFERENCES sources(id), source_kind TEXT NOT NULL,
                  remote_id TEXT NOT NULL, source_url TEXT, state TEXT NOT NULL DEFAULT 'cloud', mime_type TEXT,
                  size_bytes INTEGER, bytes_downloaded INTEGER NOT NULL DEFAULT 0, duration REAL, width INTEGER, height INTEGER,
                  thumbnail_url TEXT, pinned INTEGER NOT NULL DEFAULT 0, active_until INTEGER NOT NULL DEFAULT 0,
                  error TEXT, audio_tracks TEXT NOT NULL DEFAULT '[]', subtitle_tracks TEXT NOT NULL DEFAULT '[]',
                  playback_mode TEXT, playback_url TEXT, playback_until INTEGER, local_path TEXT, asset_dir TEXT,
                  removed_from_source INTEGER NOT NULL DEFAULT 0,
                  caption_status TEXT NOT NULL DEFAULT 'none', caption_error TEXT,
                  watch_position REAL, content_sha256 TEXT, duplicate_of TEXT REFERENCES videos(id),
                  last_played TEXT, updated_at TEXT NOT NULL, UNIQUE(source_kind, remote_id)
                );
                CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos(id), title TEXT NOT NULL,
                  state TEXT NOT NULL, phase TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0,
                  bytes_downloaded INTEGER NOT NULL DEFAULT 0, total_bytes INTEGER, error TEXT, attempt INTEGER NOT NULL DEFAULT 0,
                  cancel_requested INTEGER NOT NULL DEFAULT 0, claimed_at INTEGER, updated_at TEXT NOT NULL,
                  progress_known INTEGER NOT NULL DEFAULT 0, progress_unit TEXT NOT NULL DEFAULT 'indeterminate'
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_live_job_per_video ON jobs(video_id)
                  WHERE state IN ('queued','downloading','preparing');
                CREATE INDEX IF NOT EXISTS video_lru ON videos(pinned, active_until, last_played);
                CREATE TABLE IF NOT EXISTS activity_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, occurred_at INTEGER NOT NULL, created_at TEXT NOT NULL,
                  username TEXT, event_type TEXT NOT NULL, video_id TEXT REFERENCES videos(id), details TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS activity_recent ON activity_events(occurred_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS activity_user_recent ON activity_events(username, occurred_at DESC, id DESC);
                CREATE TABLE IF NOT EXISTS watch_history (
                  username TEXT NOT NULL REFERENCES users(username), video_id TEXT NOT NULL REFERENCES videos(id),
                  resume_position REAL NOT NULL DEFAULT 0, duration REAL, last_played TEXT NOT NULL,
                  last_watched_at INTEGER NOT NULL, PRIMARY KEY(username, video_id)
                );
                CREATE INDEX IF NOT EXISTS watch_history_recent ON watch_history(username, last_watched_at DESC);
                """
            )
            # Lightweight migrations for existing runtime databases.
            columns = {row["name"] for row in db.execute("PRAGMA table_info(videos)")}
            if "removed_from_source" not in columns:
                db.execute("ALTER TABLE videos ADD COLUMN removed_from_source INTEGER NOT NULL DEFAULT 0")
            if "caption_status" not in columns:
                db.execute("ALTER TABLE videos ADD COLUMN caption_status TEXT NOT NULL DEFAULT 'none'")
            if "caption_error" not in columns:
                db.execute("ALTER TABLE videos ADD COLUMN caption_error TEXT")
            if "watch_position" not in columns:
                db.execute("ALTER TABLE videos ADD COLUMN watch_position REAL")
            if "content_sha256" not in columns:
                db.execute("ALTER TABLE videos ADD COLUMN content_sha256 TEXT")
            if "duplicate_of" not in columns:
                db.execute("ALTER TABLE videos ADD COLUMN duplicate_of TEXT REFERENCES videos(id)")
            db.execute("CREATE INDEX IF NOT EXISTS video_content_sha256 ON videos(content_sha256) WHERE content_sha256 IS NOT NULL")
            db.execute("CREATE INDEX IF NOT EXISTS video_duplicate_of ON videos(duplicate_of) WHERE duplicate_of IS NOT NULL")
            source_columns = {row["name"] for row in db.execute("PRAGMA table_info(sources)")}
            if "sync_progress" not in source_columns:
                db.execute("ALTER TABLE sources ADD COLUMN sync_progress TEXT NOT NULL DEFAULT '{}'")
            job_columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
            if "progress_known" not in job_columns:
                db.execute("ALTER TABLE jobs ADD COLUMN progress_known INTEGER NOT NULL DEFAULT 0")
            if "progress_unit" not in job_columns:
                db.execute("ALTER TABLE jobs ADD COLUMN progress_unit TEXT NOT NULL DEFAULT 'indeterminate'")
            if "interactive" not in job_columns:
                db.execute("ALTER TABLE jobs ADD COLUMN interactive INTEGER NOT NULL DEFAULT 0")
            defaults = {
                "cache_limit_gb": _initial_cache_limit_gb(), "min_free_gb": "2", "download_concurrency": "1", "max_transcodes": "1",
                "activity_retention_days": "30", "download_schedule_enabled": "false", "download_start_hour": "0",
                "download_end_hour": "6", "download_limit_mbps": "0", "auto_cache_cleanup": "true",
                "cache_cleanup_target_percent": "90",
            }
            for key, value in defaults.items():
                db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)", (key, value))
            from .feature_schema import migrate
            migrate(db)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        db = self._open()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex


def decode_video(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if not row:
        return None
    r = dict(row)
    playback = {"mode": r.pop("playback_mode"), "url": r.pop("playback_url"), "available_until": r.pop("playback_until"), "duration": r["duration"]}
    original = Path(r['local_path']) if r.get('local_path') else None
    if original and original.suffix == '.part' and not original.is_file():
        original = original.with_suffix('')
    download_url = f"/api/media/{r['id']}/download" if original and original.suffix != '.part' and original.is_file() and r['state'] != 'removing' else None
    return {
        "id": r["id"], "title": r["title"], "source_id": r["source_id"], "source_kind": r["source_kind"],
        "remote_id": r["remote_id"], "state": r["state"], "mime_type": r["mime_type"], "size_bytes": r["size_bytes"],
        "bytes_downloaded": r["bytes_downloaded"], "duration": r["duration"], "width": r["width"], "height": r["height"],
        "thumbnail_url": r["thumbnail_url"], "poster_url": _asset_url(r, "poster.jpg"), "preview_url": _asset_url(r, "preview.mp4"),
        "sprites_vtt_url": _asset_url(r, "sprites.vtt"), "removed_from_source": bool(r["removed_from_source"]), "error": r["error"],
        "audio_tracks": json.loads(r["audio_tracks"]), "subtitle_tracks": json.loads(r["subtitle_tracks"]),
        "caption_status": r.get("caption_status", "none"), "caption_error": r.get("caption_error"),
        "watch_position": r.get("watch_position"), "content_sha256": r.get("content_sha256"),
        "duplicate_of": r.get("duplicate_of"), "download_url": download_url,
        "playback": playback, "last_played": r["last_played"], "updated_at": r["updated_at"],
    }


def _asset_url(row: dict[str, Any], name: str) -> str | None:
    return f"/api/media/{row['id']}/assets/{name}" if row.get("asset_dir") and (Path(row["asset_dir"]) / name).exists() else None


def decode_job(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if not row:
        return None
    r = dict(row)
    r["progress"] = float(r["progress"])
    r["progress_known"] = bool(r.get("progress_known", 0))
    r["progress_unit"] = r.get("progress_unit", "indeterminate")
    r.pop("cancel_requested", None); r.pop("claimed_at", None)
    return r


def decode_source(row: sqlite3.Row) -> dict[str, Any]:
    r = dict(row)
    credential_path = r.pop("credential_path", None)
    r.pop("deleted", None)
    try:
        r["sync_progress"] = json.loads(r.get("sync_progress") or "{}")
    except (TypeError, ValueError):
        r["sync_progress"] = {}
    return {**r, "has_credentials": bool(credential_path)}
