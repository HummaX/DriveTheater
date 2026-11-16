"""Periodic observed health transitions. GET alert endpoints remain read-only."""
from __future__ import annotations
import json
from pathlib import Path
import shutil
import time
from .notifications import emit_event, _prune
from .diagnostics import record_sample


def observe_conditions(db, data_dir: Path, *, now: int | None = None) -> None:
    now = int(time.time()) if now is None else int(now)
    free = shutil.disk_usage(data_dir).free
    with db.transaction() as conn:
        values = dict(conn.execute("SELECT key,value FROM settings WHERE key IN ('min_free_gb','worker_heartbeat')"))
        conditions = {
            'low_disk': (free < float(values.get('min_free_gb', '2')) * 1024 ** 3, 'Low disk space', 'Free space is below the configured minimum. Review storage before starting more transfers.'),
            'worker_offline': (int(values.get('worker_heartbeat', '0') or 0) < now - 30, 'Worker is offline', 'No worker heartbeat was observed in the last 30 seconds. Check the worker service.'),
        }
        _prune(conn, now)
        conn.execute('DELETE FROM security_events WHERE occurred_at<?', (now - 90 * 86400,))
        for table in ('auth_challenges', 'security_confirmations', 'download_plans', 'cleanup_plans'):
            conn.execute(f'DELETE FROM {table} WHERE expires_at<?', (now,))
        queued = conn.execute("SELECT COUNT(*) FROM jobs WHERE state='queued'").fetchone()[0]
        for backup in conn.execute('SELECT id,status FROM backup_records WHERE created_at>? ORDER BY created_at DESC LIMIT 100', (now - 90 * 86400,)).fetchall():
            if backup['status'] == 'created':
                emit_event(conn, 'backup:' + backup['id'] + ':created', 'backup_created', 'Private backup created', 'A local configuration/database backup was recorded. Encryption and disaster restore are not implied.')
        for kind, (unhealthy, title, message) in conditions.items():
            key = 'observed_condition:' + kind
            previous = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            try:
                old = json.loads(previous['value']) if previous else None
            except (ValueError, TypeError):
                old = None
            if not isinstance(old, dict):
                old = None
            if old and old.get('unhealthy') == unhealthy:
                continue
            sequence = int(old.get('sequence', 0)) + 1 if isinstance(old, dict) else 1
            conn.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, json.dumps({'unhealthy': unhealthy, 'sequence': sequence})))
            if unhealthy or old is not None:
                emit_event(conn, f'condition:{kind}:{sequence}', kind if unhealthy else kind + '_recovered', title if unhealthy else title + ' — recovered', message if unhealthy else 'The next scheduled health check observed this condition recover.')
    sample = {'disk_free_bytes': free, 'queued_jobs': queued}
    heartbeat = int(values.get('worker_heartbeat', '0') or 0)
    if heartbeat > 0:
        sample['worker_heartbeat_age_seconds'] = max(0, now - heartbeat)
    record_sample(db, 'health', sample)
