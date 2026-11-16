"""Additive feature migrations; called inside Database.initialize's write transaction."""
from __future__ import annotations

import sqlite3
import time


_TABLES = (
    "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)",
    """CREATE TABLE IF NOT EXISTS mfa_credentials(
        username TEXT PRIMARY KEY REFERENCES users(username), secret_cipher TEXT,
        pending_cipher TEXT, pending_until INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 0, last_counter INTEGER NOT NULL DEFAULT -1)""",
    """CREATE TABLE IF NOT EXISTS recovery_codes(
        username TEXT NOT NULL REFERENCES users(username), code_hash TEXT NOT NULL,
        consumed_at INTEGER, PRIMARY KEY(username,code_hash))""",
    """CREATE TABLE IF NOT EXISTS auth_challenges(
        id TEXT PRIMARY KEY, username TEXT NOT NULL REFERENCES users(username), peer_hash TEXT NOT NULL,
        expires_at INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS security_confirmations(
        id TEXT PRIMARY KEY,username TEXT NOT NULL REFERENCES users(username),kind TEXT NOT NULL,
        body TEXT NOT NULL,expires_at INTEGER NOT NULL)""",
    "CREATE TABLE IF NOT EXISTS security_policies(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS security_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,occurred_at INTEGER NOT NULL,
        username TEXT,event_type TEXT NOT NULL,details TEXT NOT NULL DEFAULT '{}')""",
    "CREATE INDEX IF NOT EXISTS security_events_recent ON security_events(occurred_at DESC,id DESC)",
    """CREATE TABLE IF NOT EXISTS domain_events(
        id TEXT PRIMARY KEY,kind TEXT NOT NULL,entity_id TEXT,dedupe_key TEXT NOT NULL UNIQUE,
        occurred_at INTEGER NOT NULL,payload TEXT NOT NULL DEFAULT '{}')""",
    """CREATE TABLE IF NOT EXISTS notifications(
        id TEXT PRIMARY KEY,username TEXT NOT NULL REFERENCES users(username),event_key TEXT NOT NULL,
        kind TEXT NOT NULL,title TEXT NOT NULL,message TEXT NOT NULL,entity_id TEXT,
        created_at INTEGER NOT NULL,read_at INTEGER,dismissed_at INTEGER,
        UNIQUE(username,event_key))""",
    "CREATE INDEX IF NOT EXISTS notifications_user ON notifications(username,created_at DESC)",
    """CREATE TABLE IF NOT EXISTS notification_channels(
        id TEXT PRIMARY KEY,username TEXT NOT NULL REFERENCES users(username),kind TEXT NOT NULL,
        config_cipher TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 0,created_at INTEGER NOT NULL,
        UNIQUE(username,kind))""",
    """CREATE TABLE IF NOT EXISTS notification_outbox(
        id TEXT PRIMARY KEY,notification_id TEXT NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
        channel_id TEXT NOT NULL REFERENCES notification_channels(id) ON DELETE CASCADE,
        state TEXT NOT NULL DEFAULT 'queued',attempts INTEGER NOT NULL DEFAULT 0,
        available_at INTEGER NOT NULL,claim_until INTEGER NOT NULL DEFAULT 0,last_error TEXT,
        UNIQUE(notification_id,channel_id))""",
    "CREATE INDEX IF NOT EXISTS outbox_ready ON notification_outbox(state,available_at,claim_until)",
    """CREATE TABLE IF NOT EXISTS source_health(
        source_id TEXT PRIMARY KEY REFERENCES sources(id),state TEXT NOT NULL DEFAULT 'unknown',
        last_test_at INTEGER,last_sync_at INTEGER,last_success_at INTEGER,last_sync_success_at INTEGER,
        error_code TEXT,message TEXT,retry_at INTEGER,failures INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS source_health_history(
        id INTEGER PRIMARY KEY AUTOINCREMENT,source_id TEXT NOT NULL REFERENCES sources(id),
        state TEXT NOT NULL,error_code TEXT,occurred_at INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS metadata_records(
        id TEXT PRIMARY KEY,provider TEXT NOT NULL,media_type TEXT NOT NULL,remote_id TEXT NOT NULL,
        language TEXT NOT NULL DEFAULT 'en',body TEXT NOT NULL,fetched_at INTEGER NOT NULL,
        UNIQUE(provider,media_type,remote_id,language))""",
    """CREATE TABLE IF NOT EXISTS video_metadata(
        video_id TEXT PRIMARY KEY REFERENCES videos(id),metadata_id TEXT NOT NULL REFERENCES metadata_records(id))""",
    """CREATE TABLE IF NOT EXISTS video_overrides(
        video_id TEXT PRIMARY KEY REFERENCES videos(id),body TEXT NOT NULL DEFAULT '{}',updated_at INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS collections(
        id TEXT PRIMARY KEY,username TEXT NOT NULL REFERENCES users(username),name TEXT NOT NULL,
        created_at INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS collection_items(
        collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
        video_id TEXT NOT NULL REFERENCES videos(id),position INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(collection_id,video_id))""",
    """CREATE TABLE IF NOT EXISTS saved_views(
        id TEXT PRIMARY KEY,username TEXT NOT NULL REFERENCES users(username),name TEXT NOT NULL,
        filters TEXT NOT NULL DEFAULT '{}',sort TEXT NOT NULL DEFAULT 'title',created_at INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS video_user_state(
        username TEXT NOT NULL REFERENCES users(username),video_id TEXT NOT NULL REFERENCES videos(id),
        watched INTEGER NOT NULL DEFAULT 0,origin TEXT NOT NULL DEFAULT 'automatic',completed_at INTEGER,
        resume_position REAL NOT NULL DEFAULT 0,observed_seconds REAL NOT NULL DEFAULT 0,
        last_position REAL NOT NULL DEFAULT 0,last_observed_at REAL NOT NULL DEFAULT 0,
        PRIMARY KEY(username,video_id))""",
    """CREATE TABLE IF NOT EXISTS user_preferences(
        username TEXT PRIMARY KEY REFERENCES users(username),body TEXT NOT NULL DEFAULT '{}')""",
    """CREATE TABLE IF NOT EXISTS recent_searches(
        id TEXT PRIMARY KEY,username TEXT NOT NULL REFERENCES users(username),query TEXT NOT NULL,
        created_at INTEGER NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS recent_searches_user ON recent_searches(username,created_at DESC)",
    """CREATE TABLE IF NOT EXISTS caption_cues(
        video_id TEXT NOT NULL REFERENCES videos(id),track_id TEXT NOT NULL,cue_index INTEGER NOT NULL,
        start REAL NOT NULL,end REAL NOT NULL,text TEXT NOT NULL,updated_at INTEGER NOT NULL,
        PRIMARY KEY(video_id,track_id,cue_index))""",
    """CREATE TABLE IF NOT EXISTS series_inventory(
        metadata_id TEXT NOT NULL REFERENCES metadata_records(id),season INTEGER NOT NULL,
        episode INTEGER NOT NULL,episode_id TEXT NOT NULL,title TEXT NOT NULL,released TEXT,
        thumbnail TEXT,PRIMARY KEY(metadata_id,season,episode))""",
    """CREATE TABLE IF NOT EXISTS download_plans(
        id TEXT PRIMARY KEY,username TEXT NOT NULL REFERENCES users(username),body TEXT NOT NULL,
        expires_at INTEGER NOT NULL,consumed INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS cache_policies(
        scope_kind TEXT NOT NULL,scope_id TEXT NOT NULL,keep_days INTEGER,
        delete_after_watch INTEGER NOT NULL DEFAULT 0,max_bytes INTEGER,
        PRIMARY KEY(scope_kind,scope_id))""",
    """CREATE TABLE IF NOT EXISTS cleanup_plans(
        id TEXT PRIMARY KEY,username TEXT NOT NULL REFERENCES users(username),body TEXT NOT NULL,
        created_at INTEGER NOT NULL,expires_at INTEGER NOT NULL,consumed INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS probe_diagnostics(
        video_id TEXT PRIMARY KEY REFERENCES videos(id),body TEXT NOT NULL,updated_at INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS job_stages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT NOT NULL REFERENCES jobs(id),stage TEXT NOT NULL,
        started_at REAL NOT NULL,finished_at REAL,state TEXT NOT NULL,details TEXT NOT NULL DEFAULT '{}')""",
    """CREATE TABLE IF NOT EXISTS integrity_checks(
        video_id TEXT PRIMARY KEY REFERENCES videos(id),checksum TEXT,status TEXT NOT NULL,
        checked_at INTEGER NOT NULL,expected_checksum TEXT)""",
    """CREATE TABLE IF NOT EXISTS operation_samples(
        id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,occurred_at INTEGER NOT NULL,body TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS operation_samples_time ON operation_samples(kind,occurred_at DESC)",
    """CREATE TABLE IF NOT EXISTS backup_records(
        id TEXT PRIMARY KEY,created_at INTEGER NOT NULL,verified_at INTEGER,encrypted INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL,revision TEXT)""",
)


def migrate(conn: sqlite3.Connection) -> None:
    """Do not use executescript here: it would commit the caller's migration lock."""
    for statement in _TABLES:
        conn.execute(statement)
    columns = {row['name'] for row in conn.execute('PRAGMA table_info(sessions)')}
    additions = {
        'id': 'TEXT', 'created_at': 'INTEGER NOT NULL DEFAULT 0',
        'last_seen_at': 'INTEGER NOT NULL DEFAULT 0', 'device_label': "TEXT NOT NULL DEFAULT ''",
        'network_hash': "TEXT NOT NULL DEFAULT ''", 'mfa_verified': 'INTEGER NOT NULL DEFAULT 0',
    }
    for name, declaration in additions.items():
        if name not in columns:
            conn.execute(f'ALTER TABLE sessions ADD COLUMN {name} {declaration}')
    conn.execute("UPDATE sessions SET id=lower(hex(randomblob(16))) WHERE id IS NULL")
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS session_public_id ON sessions(id)')
    state_columns = {row['name'] for row in conn.execute('PRAGMA table_info(video_user_state)')}
    if 'furthest_position' not in state_columns:
        conn.execute('ALTER TABLE video_user_state ADD COLUMN furthest_position REAL NOT NULL DEFAULT 0')
        conn.execute('UPDATE video_user_state SET furthest_position=last_position')
    conn.execute('INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(1,?)', (int(time.time()),))
