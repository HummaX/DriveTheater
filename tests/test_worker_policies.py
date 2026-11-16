import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

from drivetheater.cache import automatic_cleanup
from drivetheater.db import Database, utcnow
from drivetheater.transfer_policy import TransferThrottle, allowed_now, limit_bytes


class PolicyTests(unittest.TestCase):
    def test_schedule_windows_and_interactive_override(self):
        settings = {'download_schedule_enabled': 'true', 'download_start_hour': '22', 'download_end_hour': '6'}
        midnight = time.mktime((2025, 1, 2, 1, 0, 0, 0, 0, -1))
        noon = time.mktime((2025, 1, 2, 12, 0, 0, 0, 0, -1))
        self.assertTrue(allowed_now(settings, now=midnight))
        self.assertFalse(allowed_now(settings, now=noon))
        self.assertTrue(allowed_now(settings, interactive=True, now=noon))
        settings['download_end_hour'] = '22'
        self.assertTrue(allowed_now(settings, now=noon))

    def test_mbps_and_throttle(self):
        self.assertEqual(limit_bytes('0'), 0)
        self.assertEqual(limit_bytes('8'), 1_000_000)
        now = [0.0]
        checks = []
        throttle = TransferThrottle(100, clock=lambda: now[0], sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds))
        throttle.consume(10, lambda: checks.append(True))
        self.assertGreaterEqual(now[0], .1)
        self.assertGreaterEqual(len(checks), 2)

    def test_claim_waits_for_schedule_but_play_job_bypasses_it(self):
        from drivetheater.worker import claim
        with tempfile.TemporaryDirectory() as raw:
            db = Database(Path(raw) / 'db.sqlite')
            db.initialize()
            with db.transaction() as conn:
                for video_id in ('automatic', 'play'):
                    conn.execute('INSERT INTO videos(id,title,source_kind,remote_id,updated_at) VALUES (?,?,?,?,?)',
                                 (video_id, video_id, 'http', video_id, utcnow()))
                conn.execute("INSERT INTO jobs(id,video_id,title,state,phase,interactive,updated_at) VALUES "
                             "('automatic','automatic','automatic','queued','download',0,?),"
                             "('play','play','play','queued','download',1,?)", (utcnow(), utcnow()))
                hour = time.localtime().tm_hour
                conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('download_schedule_enabled','true')")
                conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('download_start_hour',?)", (str((hour + 1) % 24),))
                conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('download_end_hour',?)", (str((hour + 2) % 24),))
            self.assertEqual(claim(db), 'play')


        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db = Database(root / 'db.sqlite')
            db.initialize()
            cache = root / 'cache'
            for video_id, watched in [('old', 10), ('new', 0)]:
                media = cache / video_id
                media.mkdir(parents=True)
                (media / 'unpredictable-source-name').write_bytes(b'x' * 200)
                with db.transaction() as conn:
                    conn.execute(
                        "INSERT INTO videos(id,title,source_kind,remote_id,state,local_path,asset_dir,size_bytes,watch_position,last_played,updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (video_id, video_id, 'http', video_id, 'cached', str(media / 'unpredictable-source-name'), str(media), 200, watched,
                         '2020-01-01T00:00:00Z' if watched else None, utcnow()),
                    )
                    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('cache_limit_gb','0.0000003')")
                    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('cache_cleanup_target_percent','90')")
            automatic_cleanup(db, root)
            with db.connect() as conn:
                old = conn.execute("SELECT state,local_path FROM videos WHERE id='old'").fetchone()
                new = conn.execute("SELECT state FROM videos WHERE id='new'").fetchone()
            self.assertEqual(old['state'], 'cloud')
            self.assertIsNone(old['local_path'])
            self.assertEqual(new['state'], 'cached')


if __name__ == '__main__':
    unittest.main()
