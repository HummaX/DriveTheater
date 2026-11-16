"""Progress and growing-cache contracts without live media or providers."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

from drivetheater.db import Database, utcnow
from drivetheater.media import stream_cached
from drivetheater.worker import Control, ffmpeg_progress


class JobProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='drivetheater-progress-')
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / 'db.sqlite3')
        self.db.initialize()
        with self.db.transaction() as conn:
            conn.execute('INSERT INTO videos(id,title,source_kind,remote_id,updated_at) VALUES (?,?,?,?,?)',
                         ('video', 'Synthetic', 'http', 'video', utcnow()))
            conn.execute("INSERT INTO jobs(id,video_id,title,state,phase,updated_at) VALUES (?,?,?,'preparing','probe',?)",
                         ('job', 'video', 'Synthetic', utcnow()))
        self.control = Control(self.db, 'job', self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def job(self):
        with self.db.connect() as conn:
            return dict(conn.execute("SELECT * FROM jobs WHERE id='job'").fetchone())

    def test_out_time_us_records_known_media_seconds_progress(self):
        ffmpeg_progress(self.control, 'transcode', 20)('out_time_us=5000000')
        job = self.job()
        self.assertEqual(job['phase'], 'transcode')
        self.assertEqual(job['progress_known'], 1)
        self.assertEqual(job['progress_unit'], 'media_seconds')
        self.assertAlmostEqual(job['progress'], 25.0)

    def test_opaque_phase_is_explicitly_indeterminate(self):
        self.control.progress('subtitles')
        job = self.job()
        self.assertEqual(job['phase'], 'subtitles')
        self.assertEqual(job['progress_known'], 0)
        self.assertEqual(job['progress_unit'], 'indeterminate')
        self.assertEqual(job['progress'], 0)


class GrowingCacheRangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='drivetheater-growing-')
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / 'db.sqlite3')
        self.db.initialize()
        self.partial = self.root / 'source.bin.part'
        self.partial.write_bytes(b'present')
        with self.db.transaction() as conn:
            conn.execute('INSERT INTO videos(id,title,source_kind,remote_id,state,local_path,size_bytes,updated_at) VALUES (?,?,?,?,?,?,?,?)',
                         ('video', 'Synthetic', 'http', 'video', 'playable', str(self.partial), 100, utcnow()))

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def request(range_header):
        return Request({'type': 'http', 'method': 'GET', 'headers': [(b'range', range_header.encode('ascii'))]})

    def test_growing_range_never_advertises_bytes_not_written_yet(self):
        row = {'id': 'video', 'state': 'playable', 'local_path': str(self.partial), 'size_bytes': 100, 'mime_type': 'video/mp4'}
        response = stream_cached(self.db, row, self.request('bytes=0-99'))
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers['content-range'], 'bytes 0-6/100')
        self.assertEqual(response.headers['content-length'], '7')


if __name__ == '__main__':
    unittest.main(verbosity=2)
