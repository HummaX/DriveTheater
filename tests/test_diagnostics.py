"""Synthetic diagnostics, persistence, privacy and route-policy tests."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from drivetheater.db import Database, utcnow
from drivetheater.feature_schema import migrate
from drivetheater.diagnostics import (DiagnosticsService, finish_job, record_probe,
                                     safe_details, safe_probe, transition_stage)


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dt-diagnostics-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.db = Database(self.root / 'db.sqlite3')
        self.db.initialize()
        with self.db.transaction() as conn:
            migrate(conn)
            conn.execute("INSERT INTO videos(id,title,source_kind,remote_id,source_url,updated_at) VALUES('video','PRIVATE-TITLE','http','private-remote','https://secret.example/token',?)", (utcnow(),))
            conn.execute("INSERT INTO jobs(id,video_id,title,state,phase,updated_at) VALUES('job','video','PRIVATE-JOB','downloading','download',?)", (utcnow(),))
        self.service = DiagnosticsService(self.db, self.root)

    def test_probe_keeps_only_safe_bounded_media_facts(self):
        raw = {'format': {'filename': '/private/source', 'duration': '12.5', 'bit_rate': '1000', 'tags': {'title': 'PRIVATE-TITLE'}},
               'streams': [{'codec_type': 'video', 'codec_name': 'h264', 'width': 1920, 'height': 1080, 'tags': {'token': 'SECRET'}},
                           {'codec_type': 'audio', 'codec_name': 'https://private/token', 'channels': 2, 'sample_rate': '48000'},
                           {'codec_type': 'subtitle', 'codec_name': ['malformed']}], 'error': 'RAW PRIVATE LOG'}
        facts = record_probe(self.db, 'video', raw)
        self.assertEqual(facts['video'][0]['codec'], 'h264')
        self.assertEqual(facts['duration_seconds'], 12.5)
        self.assertEqual(facts['audio'][0]['codec'], 'unknown')
        result = self.service.probe('video')
        self.assertEqual(result['facts'], facts)
        serialized = json.dumps(result)
        for secret in ('PRIVATE', 'SECRET', '/private', 'https://', 'token', 'filename', 'tags'):
            self.assertNotIn(secret, serialized)
        self.assertLessEqual(len(safe_probe({'streams': raw['streams'] * 100})['video']), 64)

    def test_stage_phase_hooks_are_idempotent_and_finish_terminal(self):
        identifier = transition_stage(self.db, 'job', 'download')
        for _ in range(10):
            self.assertEqual(transition_stage(self.db, 'job', 'download'), identifier)
        next_identifier = transition_stage(self.db, 'job', 'hashing', {'bytes': 100, 'secret': 'RAW-TOKEN'})
        self.assertNotEqual(identifier, next_identifier)
        transition_stage(self.db, 'job', 'caption_audio')
        transition_stage(self.db, 'job', 'caption_transcribe')
        finish_job(self.db, 'job', 'failed', {'error_code': 'timeout', 'error': 'PRIVATE LOG'})
        stages = self.service.job_stages('job')
        self.assertEqual(len(stages), 4)
        self.assertTrue(all(item['finished_at'] is not None for item in stages))
        self.assertEqual(stages[0]['state'], 'failed')
        self.assertEqual(stages[0]['details']['error_code'], 'timeout')
        self.assertNotIn('PRIVATE', json.dumps(stages))
        self.assertNotIn('RAW-TOKEN', json.dumps(stages))
        self.assertIsNone(transition_stage(self.db, 'job', 'https://secret'))
        self.assertEqual(len(self.service.job_stages('job')), 4)

    def test_stage_context_records_failure_without_exception_text(self):
        with self.assertRaises(RuntimeError):
            with self.service.stage('job', 'probe'):
                raise RuntimeError('/private/path token=SECRET')
        result = self.service.job_stages('job')[0]
        self.assertEqual(result['state'], 'failed')
        self.assertNotIn('SECRET', json.dumps(result))

    def test_samples_and_stages_are_bounded(self):
        for _ in range(205):
            identifier = self.service.start_stage('job', 'probe')
            self.service.finish_stage(identifier)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM job_stages').fetchone()[0], 200)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM operation_samples').fetchone()[0], 200)
        self.assertEqual(len(self.service.samples()), 200)

    def test_active_stage_bound_prevents_unbounded_running_records(self):
        for _ in range(16):
            self.service.start_stage('job', 'probe')
        with self.assertRaises(ValueError):
            self.service.start_stage('job', 'probe')
        finish_job(self.db, 'job', 'cancelled')
        self.assertTrue(all(row['state'] == 'cancelled' for row in self.service.job_stages('job')))

    def test_support_export_allowlist_even_with_legacy_poisoned_rows(self):
        secret = 'PRIVATE-SECRET-TOKEN'
        self.service.record_sample('download', {'bytes': 1234, 'elapsed_seconds': 2, 'source_url': secret, 'title': secret, 'raw_logs': secret, 'path': secret, 'error_code': secret})
        with self.db.transaction() as conn:
            conn.execute('INSERT INTO operation_samples(kind,occurred_at,body) VALUES(?,?,?)', ('probe', secret, json.dumps({'bytes': secret, 'title': secret, 'error_code': 'timeout'})))
            conn.execute('INSERT INTO operation_samples(kind,occurred_at,body) VALUES(?,?,?)', (secret, 1, json.dumps({'bytes': 1})))
            conn.execute('INSERT INTO backup_records(id,created_at,verified_at,encrypted,status,revision) VALUES(?,?,?,?,?,?)', (secret, 1, secret, 1, secret, secret))
            conn.execute('INSERT INTO integrity_checks VALUES(?,?,?,?,?)', ('video', secret, secret, 1, secret))
            conn.execute('INSERT INTO probe_diagnostics VALUES(?,?,?)', ('video', json.dumps({'raw_logs': secret}), 1))
        report = self.service.support_export()
        serialized = json.dumps(report)
        for forbidden in (secret, 'PRIVATE-TITLE', 'PRIVATE-JOB', 'https://', '/private', 'source_url', 'raw_logs', 'password', 'revision'):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(report['monitor']['backup_count'], 1)
        self.assertIsNone(report['monitor']['last_backup_verified_at'])
        self.assertEqual(report['samples'][-1]['bytes'], 1234)
        self.assertIsNone(report['samples'][0]['occurred_at'])

    def test_numeric_detail_validation_rejects_nonfinite_and_booleans(self):
        self.assertEqual(safe_details({'bytes': float('nan'), 'files': True, 'duration_seconds': -1, 'progress': '25', 'error_code': []}), {'progress': 25})

    def test_monitor_reports_unknown_host_and_container_counts_and_best_effort(self):
        import psutil
        with patch('drivetheater.diagnostics.psutil.pids', side_effect=psutil.AccessDenied()), patch('drivetheater.diagnostics.shutil.disk_usage', side_effect=OSError('private path')):
            monitor = self.service.monitor()
        self.assertIsNone(monitor['host_process_count'])
        self.assertIsNone(monitor['container_count'])
        self.assertIsNone(monitor['visible_process_count'])
        self.assertIsNone(monitor['disk_free_bytes'])
        self.assertNotIn('private path', json.dumps(monitor))
        self.assertEqual(monitor['running_jobs'], 1)

    def test_worker_failure_maps_only_allowlisted_categories(self):
        from drivetheater import worker
        cases = [
            (RuntimeError('Free-space headroom reached /private/path token=SECRET'), 'disk_full'),
            (RuntimeError('The downloaded source does not contain a video stream /private/path'), 'invalid_media'),
            (RuntimeError('execution deadline token=SECRET'), 'timeout'),
            (OSError('https://private.example/?token=SECRET /private/path'), 'unknown'),
        ]
        for failure, expected in cases:
            with self.subTest(expected=expected), patch.object(worker, 'automatic_cleanup'), patch.object(worker.Control, 'budget'), patch.object(worker, '_download_with_retry', side_effect=failure), patch.object(worker, 'run') as subprocess_run:
                worker.process_job(self.db, self.root, 'job')
                subprocess_run.assert_not_called()
            with self.db.connect() as conn:
                job = dict(conn.execute("SELECT state,error FROM jobs WHERE id='job'").fetchone())
            stage = self.service.job_stages('job')[0]
            self.assertEqual(job['state'], 'failed')
            self.assertEqual(stage['state'], 'failed')
            self.assertEqual(stage['details']['error_code'], expected)
            for forbidden in ('SECRET', '/private/path', 'https://private.example'):
                self.assertNotIn(forbidden, json.dumps({'job': job, 'stage': stage, 'support': self.service.support_export()}))

    def test_worker_cancellation_records_category_without_exception_text(self):
        from drivetheater import worker
        with patch.object(worker, 'automatic_cleanup'), patch.object(worker.Control, 'budget'), patch.object(worker, '_download_with_retry', side_effect=worker.Cancelled('PRIVATE-REASON')):
            worker.process_job(self.db, self.root, 'job')
        stage = self.service.job_stages('job')[0]
        self.assertEqual(stage['state'], 'cancelled')
        self.assertEqual(stage['details']['error_code'], 'cancelled')
        self.assertNotIn('PRIVATE-REASON', json.dumps(stage))

    def test_all_routes_use_unified_principal_without_server_requests(self):
        from drivetheater.access import require_principal
        from drivetheater.storage_features import StorageService
        from drivetheater.storage_routes import create_storage_router
        (self.root / 'cache').mkdir()
        router = create_storage_router(StorageService(self.db, self.root / 'cache'), self.service)
        self.assertGreaterEqual(len(router.routes), 10)
        for route in router.routes:
            self.assertTrue(any(dependency.call is require_principal for dependency in route.dependant.dependencies), route.path)
        verify = next(route for route in router.routes if route.path.endswith('/integrity') and 'POST' in route.methods)
        self.assertEqual(verify.dependant.body_params, [])  # Caller cannot claim a provider checksum.


if __name__ == '__main__':
    unittest.main()
