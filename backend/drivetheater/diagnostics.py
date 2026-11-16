"""Persist allowlisted diagnostics, never raw ffprobe payloads or exception logs."""
from __future__ import annotations

import contextlib
import json
import math
import shutil
import time


def record_probe(db, video_id: str, probe: dict) -> dict:
    return DiagnosticsService(db, db.path.parent).record_probe(video_id, probe)


def record_sample(db, kind: str, details: dict | None = None) -> None:
    DiagnosticsService(db, db.path.parent).record_sample(kind, details)


def transition_stage(db, job_id: str, stage: str, details: dict | None = None) -> int | None:
    """Worker phase hook. Same-phase progress/heartbeat calls never create rows."""
    if stage not in STAGES:
        return None
    service = DiagnosticsService(db, db.path.parent)
    with db.connect() as conn:
        running = list(conn.execute("SELECT id,stage FROM job_stages WHERE job_id=? AND state='running' ORDER BY id DESC", (job_id,)))
    if len(running) == 1 and running[0]['stage'] == stage:
        return running[0]['id']
    for row in running:
        service.finish_stage(row['id'], details=details)
    return service.start_stage(job_id, stage)


def finish_job(db, job_id: str, state: str = 'completed', details: dict | None = None) -> None:
    service = DiagnosticsService(db, db.path.parent)
    with db.connect() as conn:
        rows = list(conn.execute("SELECT id FROM job_stages WHERE job_id=? AND state='running'", (job_id,)))
    for row in rows:
        service.finish_stage(row['id'], state, details)
from pathlib import Path

import psutil

from .cache_safety import CacheConflict, safe_walk

STAGES = {'download', 'hashing', 'probe', 'package', 'subtitles', 'previews',
          'caption_audio', 'caption_transcribe',
          'transcription', 'integrity', 'cleanup', 'source_sync', 'source_test'}
SAMPLE_KINDS = STAGES | {'queue', 'playback', 'health'}
STATES = {'running', 'completed', 'failed', 'cancelled'}
ERROR_CODES = {'timeout', 'unavailable', 'invalid_media', 'cancelled', 'disk_full', 'unknown'}
NUMBER_FIELDS = {'duration_seconds', 'elapsed_seconds', 'bytes', 'total_bytes', 'files',
                 'reclaimed_bytes', 'queue_seconds', 'progress', 'attempt', 'exit_code',
                 'disk_free_bytes', 'worker_heartbeat_age_seconds', 'queued_jobs'}
VIDEO_CODECS = {'h264', 'hevc', 'av1', 'vp9', 'vp8', 'mpeg4', 'mpeg2video', 'theora', 'prores', 'mjpeg'}
AUDIO_CODECS = {'aac', 'mp3', 'opus', 'vorbis', 'flac', 'alac', 'ac3', 'eac3', 'dts', 'pcm_s16le', 'pcm_s24le'}
SUBTITLE_CODECS = {'subrip', 'ass', 'ssa', 'webvtt', 'mov_text', 'hdmv_pgs_subtitle', 'dvd_subtitle'}


def number(value, maximum=10 ** 15):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and 0 <= value <= maximum else None
    except (TypeError, ValueError, OverflowError):
        return None


def _object(value) -> dict:
    if not isinstance(value, str) or len(value) > 65536:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def safe_details(details: dict | None) -> dict:
    if not isinstance(details, dict):
        return {}
    result = {key: value for key in NUMBER_FIELDS if (value := number(details.get(key))) is not None}
    if isinstance(details.get('error_code'), str) and details['error_code'] in ERROR_CODES:
        result['error_code'] = details['error_code']
    return result


def safe_probe(probe: dict) -> dict:
    if not isinstance(probe, dict):
        raise ValueError('Probe facts must be an object')
    streams = probe.get('streams')
    streams = streams[:64] if isinstance(streams, list) else []
    fmt = probe.get('format') if isinstance(probe.get('format'), dict) else {}
    video, audio, subtitles = [], [], []
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        codec = stream.get('codec_name')
        codec = codec if isinstance(codec, str) else 'unknown'
        kind = stream.get('codec_type')
        if kind == 'video':
            video.append({'codec': codec if codec in VIDEO_CODECS else 'unknown',
                          'width': number(stream.get('width'), 32768),
                          'height': number(stream.get('height'), 32768)})
        elif kind == 'audio':
            audio.append({'codec': codec if codec in AUDIO_CODECS else 'unknown',
                          'channels': number(stream.get('channels'), 64),
                          'sample_rate': number(stream.get('sample_rate'), 768000)})
        elif kind == 'subtitle':
            subtitles.append({'codec': codec if codec in SUBTITLE_CODECS else 'unknown'})
    return {'duration_seconds': number(fmt.get('duration'), 31_536_000),
            'bit_rate': number(fmt.get('bit_rate'), 10 ** 11),
            'video': video, 'audio': audio, 'subtitles': subtitles}


class DiagnosticsService:
    def __init__(self, db, data_dir: Path):
        self.db, self.data_dir = db, Path(data_dir)

    def record_probe(self, video_id: str, probe: dict) -> dict:
        facts = safe_probe(probe)
        with self.db.transaction() as conn:
            conn.execute('INSERT INTO probe_diagnostics(video_id,body,updated_at) VALUES(?,?,?) ON CONFLICT(video_id) DO UPDATE SET body=excluded.body,updated_at=excluded.updated_at', (video_id, json.dumps(facts), int(time.time())))
        return facts

    def probe(self, video_id: str) -> dict:
        with self.db.connect() as conn:
            row = conn.execute('SELECT body,updated_at FROM probe_diagnostics WHERE video_id=?', (video_id,)).fetchone()
        if not row:
            return {'facts': None, 'updated_at': None}
        # Reconstruct the allowlist even if a legacy payload was written elsewhere.
        body = _object(row['body'])
        streams = [dict(stream, codec_type=kind) for key, kind in [('video', 'video'), ('audio', 'audio'), ('subtitles', 'subtitle')] for stream in (body.get(key) if isinstance(body.get(key), list) else [])[:64] if isinstance(stream, dict)]
        for stream in streams:
            stream['codec_name'] = stream.pop('codec', None)
        return {'facts': safe_probe({'format': {'duration': body.get('duration_seconds'), 'bit_rate': body.get('bit_rate')}, 'streams': streams}), 'updated_at': row['updated_at']}

    def start_stage(self, job_id: str, stage: str, details: dict | None = None) -> int:
        if stage not in STAGES:
            raise ValueError('Unsupported diagnostic stage')
        with self.db.transaction() as conn:
            if conn.execute("SELECT COUNT(*) FROM job_stages WHERE job_id=? AND state='running'", (job_id,)).fetchone()[0] >= 16 or conn.execute("SELECT COUNT(*) FROM job_stages WHERE state='running'").fetchone()[0] >= 256:
                raise ValueError('Too many active diagnostic stages')
            cursor = conn.execute('INSERT INTO job_stages(job_id,stage,started_at,state,details) VALUES(?,?,?,?,?)', (job_id, stage, time.time(), 'running', json.dumps(safe_details(details))))
            return cursor.lastrowid

    def finish_stage(self, stage_id: int, state: str = 'completed', details: dict | None = None) -> None:
        if state not in STATES - {'running'}:
            raise ValueError('Unsupported diagnostic state')
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM job_stages WHERE id=? AND state='running'", (stage_id,)).fetchone()
            if not row:
                return
            safe = {**safe_details(_object(row['details'])), **safe_details(details)}
            safe['elapsed_seconds'] = max(0, time.time() - row['started_at'])
            conn.execute('UPDATE job_stages SET finished_at=?,state=?,details=? WHERE id=?', (time.time(), state, json.dumps(safe), stage_id))
            conn.execute("DELETE FROM job_stages WHERE job_id=? AND state!='running' AND id NOT IN (SELECT id FROM job_stages WHERE job_id=? ORDER BY id DESC LIMIT 200)", (row['job_id'], row['job_id']))
            conn.execute("DELETE FROM job_stages WHERE state!='running' AND id NOT IN (SELECT id FROM job_stages ORDER BY id DESC LIMIT 5000)")
        self.record_sample(row['stage'], safe)

    @contextlib.contextmanager
    def stage(self, job_id: str, stage: str, details: dict | None = None):
        identifier = self.start_stage(job_id, stage, details)
        try:
            yield identifier
        except Exception:
            self.finish_stage(identifier, 'failed', {'error_code': 'unknown'})
            raise
        else:
            self.finish_stage(identifier)

    def job_stages(self, job_id: str) -> list[dict]:
        with self.db.connect() as conn:
            rows = list(conn.execute('SELECT stage,started_at,finished_at,state,details FROM job_stages WHERE job_id=? ORDER BY id DESC LIMIT 200', (job_id,)))
        return [{'stage': row['stage'] if row['stage'] in STAGES else 'unknown',
                 'started_at': number(row['started_at']), 'finished_at': number(row['finished_at']),
                 'state': row['state'] if row['state'] in STATES else 'unknown',
                 'details': safe_details(_object(row['details']))} for row in rows]

    def record_sample(self, kind: str, details: dict | None = None) -> None:
        if kind not in SAMPLE_KINDS:
            raise ValueError('Unsupported operation sample')
        with self.db.transaction() as conn:
            conn.execute('INSERT INTO operation_samples(kind,occurred_at,body) VALUES(?,?,?)', (kind, int(time.time()), json.dumps(safe_details(details))))
            conn.execute('DELETE FROM operation_samples WHERE kind=? AND id NOT IN (SELECT id FROM operation_samples WHERE kind=? ORDER BY id DESC LIMIT 200)', (kind, kind))
            conn.execute('DELETE FROM operation_samples WHERE id NOT IN (SELECT id FROM operation_samples ORDER BY id DESC LIMIT 1000)')

    def samples(self) -> list[dict]:
        with self.db.connect() as conn:
            rows = list(conn.execute('SELECT kind,occurred_at,body FROM operation_samples ORDER BY id DESC LIMIT 200'))
        return [{'kind': row['kind'], 'occurred_at': number(row['occurred_at']), **safe_details(_object(row['body']))} for row in rows if row['kind'] in SAMPLE_KINDS]

    def monitor(self) -> dict:
        """Local visible-namespace observations; never contact a Docker socket."""
        facts = {'scope': 'visible_runtime_namespace', 'host_process_count': None,
                 'container_count': None, 'visible_process_count': None,
                 'process_rss_bytes': None, 'process_cpu_seconds': None,
                 'disk_free_bytes': None, 'temp_bytes': None,
                 'ffmpeg_available': bool(shutil.which('ffmpeg')),
                 'ffprobe_available': bool(shutil.which('ffprobe'))}
        try:
            facts['visible_process_count'] = len(psutil.pids())
            process = psutil.Process()
            facts['process_rss_bytes'] = process.memory_info().rss
            cpu = process.cpu_times()
            facts['process_cpu_seconds'] = cpu.user + cpu.system
        except (psutil.Error, OSError):
            pass
        try:
            facts['disk_free_bytes'] = shutil.disk_usage(self.data_dir).free
        except OSError:
            pass
        temporary = self.data_dir / 'tmp'
        if temporary.exists():
            try:
                facts['temp_bytes'] = sum(item['size'] for item in safe_walk(temporary, temporary, maximum=10000))
            except (OSError, CacheConflict):
                pass
        now = int(time.time())
        with self.db.connect() as conn:
            facts['active_sessions'] = conn.execute('SELECT COUNT(*) FROM sessions WHERE expires_at>?', (now,)).fetchone()[0]
            facts['running_jobs'] = conn.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','downloading','preparing')").fetchone()[0]
            heartbeat = conn.execute("SELECT value FROM settings WHERE key='worker_heartbeat'").fetchone()
            age = number(now - float(heartbeat[0])) if heartbeat and number(heartbeat[0]) is not None else None
            facts['worker_heartbeat_age_seconds'] = age
            facts['worker_alive'] = age is not None and age < 30
            backup = conn.execute('SELECT COUNT(*),MAX(verified_at),SUM(CASE WHEN encrypted=1 THEN 1 ELSE 0 END) FROM backup_records').fetchone()
            facts['backup_count'], facts['last_backup_verified_at'], facts['encrypted_backup_count'] = backup[0], number(backup[1]), backup[2] or 0
        return facts

    def support_export(self) -> dict:
        """Schema allowlist, not redaction: no identifiers, logs, URLs or config dump."""
        with self.db.connect() as conn:
            states = dict(conn.execute('SELECT status,COUNT(*) FROM integrity_checks GROUP BY status'))
            probe_count = conn.execute('SELECT COUNT(*) FROM probe_diagnostics').fetchone()[0]
            stage_rows = list(conn.execute('SELECT stage,state,COUNT(*) FROM job_stages GROUP BY stage,state'))
        allowed_integrity = {'not_checked', 'locally_computed', 'local_match', 'provider_verified', 'mismatch'}
        return {'schema_version': 1, 'generated_at': int(time.time()),
                'monitor': self.monitor(), 'samples': self.samples(), 'probe_count': probe_count,
                'integrity_counts': {key: value for key, value in states.items() if key in allowed_integrity},
                'stage_counts': [{'stage': stage, 'state': state, 'count': count} for stage, state, count in stage_rows if stage in STAGES and state in STATES]}
