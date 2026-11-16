"""Throttled local HTTP and concurrent play tests, no real provider requests."""
import concurrent.futures
import contextlib
import importlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))


class DownloadCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='drivetheater-http-tests-')
        cls.root = Path(cls.tmp.name).resolve()
        cls.env = {key: os.environ.get(key) for key in ('DT_DATA_DIR', 'DT_ADMIN_PASSWORD', 'DT_SECRET', 'DT_ALLOW_PRIVATE_SOURCES')}
        os.environ.update(DT_DATA_DIR=str(cls.root / 'runtime'), DT_ADMIN_PASSWORD='test-long-password', DT_SECRET='q' * 48, DT_ALLOW_PRIVATE_SOURCES='true')
        from drivetheater import worker
        from drivetheater.db import Database, utcnow
        cls.worker, cls.Database, cls.utcnow = worker, Database, staticmethod(utcnow)
        cls.payload = b'fixed-media-byte-' * 40000
        cls.requests = []
        cls.etag = '"fixture-v1"'
        cls.ignore_range = False
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                cls.requests.append(dict(self.headers))
                start = 0
                match = re.fullmatch(r'bytes=(\d+)-', self.headers.get('Range', ''))
                if match and not cls.ignore_range:
                    start = int(match[1])
                    self.send_response(206)
                    self.send_header('Content-Range', f'bytes {start}-{len(cls.payload)-1}/{len(cls.payload)}')
                else:
                    self.send_response(200)
                self.send_header('ETag', cls.etag)
                self.send_header('Content-Length', str(len(cls.payload) - start))
                self.end_headers()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    for offset in range(start, len(cls.payload), 32 * 1024):
                        self.wfile.write(cls.payload[offset:offset + 32 * 1024])
                        self.wfile.flush()
                        time.sleep(.002)
            def log_message(self, *_):
                pass
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}/source.mp4'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        for key, value in cls.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls.tmp.cleanup()

    def setUp(self):
        self.case = self.root / self.id().rsplit('.', 1)[-1]
        self.case.mkdir()
        self.db = self.Database(self.case / 'db.sqlite3')
        self.db.initialize()
        self.media = self.case / 'cache' / 'video'
        self.media.mkdir(parents=True)
        self.target = self.media / 'source.bin'
        self.requests.clear()
        type(self).etag = '"fixture-v1"'
        type(self).ignore_range = False
        with self.db.transaction() as conn:
            conn.execute('INSERT INTO videos(id,title,source_kind,remote_id,source_url,updated_at) VALUES (?,?,?,?,?,?)', ('video', 'Fixture', 'http', self.url, self.url, self.utcnow()))
            conn.execute("INSERT INTO jobs(id,video_id,title,state,phase,updated_at) VALUES ('job','video','Fixture','downloading','download',?)", (self.utcnow(),))
        self.control = self.worker.Control(self.db, 'job', self.case)

    def partial(self, validator='"fixture-v1"'):
        path = self.target.with_suffix('.bin.part')
        path.write_bytes(self.payload[:10000])
        path.with_suffix('.part.json').write_text(json.dumps({'validator': validator, 'total': len(self.payload)}))
        return path

    def download(self):
        self.worker._download_http(self.url, self.target, 'job', self.db, control=self.control, allow_private=True)

    def test_matching_validator_resumes_without_duplicate_bytes(self):
        self.partial()
        self.download()
        self.assertEqual(self.target.read_bytes(), self.payload)
        self.assertEqual(self.requests[0]['Range'], 'bytes=10000-')
        self.assertEqual(self.requests[0]['If-Range'], '"fixture-v1"')

    def test_changed_validator_restarts_instead_of_appending(self):
        self.partial('"old-version"')
        self.download()
        self.assertEqual(self.target.read_bytes(), self.payload)
        self.assertEqual(len(self.requests), 2)
        self.assertNotIn('Range', self.requests[-1])

    def test_server_ignoring_range_restarts_cleanly(self):
        self.partial()
        type(self).ignore_range = True
        self.download()
        self.assertEqual(self.target.read_bytes(), self.payload)
        self.assertEqual(len(self.requests), 2)

    def test_unvalidated_partial_is_never_resumed(self):
        self.partial(None)
        self.download()
        self.assertEqual(self.target.read_bytes(), self.payload)
        self.assertNotIn('Range', self.requests[0])

    def test_cancelled_download_keeps_existing_partial_bytes(self):
        partial = self.partial()
        with self.db.transaction() as conn:
            conn.execute("UPDATE jobs SET cancel_requested=1 WHERE id='job'")
        with self.assertRaises(self.worker.Cancelled):
            self.download()
        self.assertEqual(partial.read_bytes(), self.payload[:10000])
        self.assertFalse(self.target.exists())

    def test_range_parser_rejects_malformed_multiple_and_empty(self):
        from drivetheater.media import parse_range
        self.assertEqual(parse_range('bytes=-20', 100), (80, 99))
        self.assertEqual(parse_range('bytes=20-', 100), (20, 99))
        for header in ('bytes=-0', 'bytes=-', 'bytes=0-4,6-9', 'bytes=4-3', 'bytes=100-'):
            with self.subTest(header=header), self.assertRaises((ValueError, IndexError)):
                parse_range(header, 100)


class PlayDeduplicationTests(unittest.TestCase):
    def test_concurrent_play_and_browsing_do_not_duplicate_download_jobs(self):
        with tempfile.TemporaryDirectory(prefix='drivetheater-api-tests-') as tmp:
            from drivetheater.config import Config
            path = Path(tmp).resolve()
            with patch.dict(os.environ, {'DT_DATA_DIR': str(path), 'DT_ADMIN_PASSWORD': 'long-test-password', 'DT_SECRET': 'r' * 48}):
                from drivetheater.main import create_app
                cfg = Config(path, 'admin', 'long-test-password', 'r' * 48, False, True, None)
                app = create_app(cfg)
                from fastapi.testclient import TestClient
                with TestClient(app) as client:
                    self.assertEqual(client.post('/api/auth/login', json={'username': 'admin', 'password': 'long-test-password'}).status_code, 200)
                    registered = client.post('/api/urls', json={'url': 'http://127.0.0.1:9999/fixture.mp4', 'engine': 'http'}).json()
                    for _ in range(3):
                        client.get('/api/videos')
                        client.get('/api/jobs')
                        client.get('/api/videos/' + registered['id'])
                    self.assertEqual(client.get('/api/jobs').json()['items'], [])
                    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                        responses = list(pool.map(lambda _: client.post('/api/videos/' + registered['id'] + '/play'), range(8)))
                    self.assertTrue(all(response.status_code == 200 for response in responses))
                    jobs = client.get('/api/jobs').json()['items']
                    self.assertEqual(len(jobs), 1)
                    self.assertEqual({r.json()['job']['id'] for r in responses}, {jobs[0]['id']})


if __name__ == '__main__':
    unittest.main(verbosity=2)
