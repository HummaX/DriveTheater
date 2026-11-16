"""Real synthetic-media integration. Never accesses Google or user media."""
import concurrent.futures
import contextlib
import importlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg and ffprobe required')
class MediaIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='drivetheater-synthetic-media-')
        cls.root = Path(cls.tmp.name).resolve()
        cls.data = cls.root / 'data'
        cls.data.mkdir()
        cls.old_env = {k: os.environ.get(k) for k in ('DT_DATA_DIR', 'DT_ADMIN_PASSWORD', 'DT_SECRET', 'DT_ALLOW_PRIVATE_SOURCES')}
        os.environ.update(DT_DATA_DIR=str(cls.data), DT_ADMIN_PASSWORD='synthetic-test-password', DT_SECRET='x' * 48, DT_ALLOW_PRIVATE_SOURCES='true')
        from drivetheater import worker
        from drivetheater.db import Database, utcnow
        cls.worker, cls.Database, cls.utcnow = worker, Database, staticmethod(utcnow)
        cls.multi = cls.root / 'multi.mkv'
        cls.single = cls.root / 'faststart.mp4'
        cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=24:duration=12', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=12', '-f', 'lavfi', '-i', 'sine=frequency=880:duration=12', '-map', '0:v', '-map', '1:a', '-map', '2:a', '-c:v', 'libx264', '-threads', '2', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-metadata:s:a:0', 'language=en', '-metadata:s:a:0', 'title=English', '-metadata:s:a:1', 'language=hi', '-metadata:s:a:1', 'title=Hindi', str(cls.multi)]
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(cls.multi), '-map', '0:v:0', '-map', '0:a:0', '-c', 'copy', '-movflags', '+faststart', str(cls.single)], check=True, capture_output=True, timeout=30)

    @classmethod
    def tearDownClass(cls):
        for key, value in cls.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls.tmp.cleanup()

    def database(self, name):
        db = self.Database(self.data / 'db' / (name + '.sqlite3'))
        db.initialize()
        return db

    def video_job(self, db, name):
        with db.transaction() as conn:
            conn.execute('INSERT INTO videos(id,title,source_kind,remote_id,source_url,updated_at) VALUES (?,?,?,?,?,?)', (name, 'Synthetic moving video', 'http', name, 'http://127.0.0.1/fixture', self.utcnow()))
            conn.execute("INSERT INTO jobs(id,video_id,title,state,phase,updated_at) VALUES (?,?,?,'queued','queued',?)", (name, name, 'Synthetic moving video', self.utcnow()))

    def test_faststart_policy_requires_browser_codec_and_single_audio(self):
        probe = self.worker.ffprobe(self.single)
        self.assertTrue(self.worker._faststart_playable(self.single, probe))
        self.assertFalse(self.worker._faststart_playable(self.multi, self.worker.ffprobe(self.multi)))
        altered = json.loads(json.dumps(probe))
        altered['streams'][0]['codec_name'] = 'hevc'
        self.assertFalse(self.worker._faststart_playable(self.single, altered))
        multi_mp4 = self.root / 'multi.mp4'
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(self.multi), '-map', '0', '-c', 'copy', '-movflags', '+faststart', str(multi_mp4)], check=True, capture_output=True, timeout=30)
        self.assertFalse(self.worker._faststart_playable(multi_mp4, self.worker.ffprobe(multi_mp4)))

    def test_real_hls_has_two_language_renditions_and_decodable_segments(self):
        assets = self.root / 'hls-assets'
        assets.mkdir(exist_ok=True)
        seen = []
        tracks, url = self.worker._hls(self.multi, assets, 'synthetic', on_ready=lambda *args: seen.append(args))
        self.assertEqual([track['language'] for track in tracks], ['en', 'hi'])
        self.assertEqual(len(seen), 1)
        master = (assets / 'hls' / 'master.m3u8').read_text()
        self.assertIn('LANGUAGE="en"', master)
        self.assertIn('LANGUAGE="hi"', master)
        for playlist in ('video.m3u8', 'audio-0.m3u8', 'audio-1.m3u8'):
            contents = (assets / 'hls' / playlist).read_text()
            self.assertIn('#EXT-X-ENDLIST', contents)
            segments = [line for line in contents.splitlines() if line and not line.startswith('#')]
            self.assertTrue(segments)
            self.assertTrue((assets / 'hls' / segments[0]).is_file())
        probe = self.worker.ffprobe(assets / 'hls' / 'video-00000.ts')
        self.assertEqual(probe['streams'][0]['codec_type'], 'video')

    def test_hls_preserves_original_video_without_a_quality_ladder(self):
        source = self.root / 'short-hd.mkv'
        subprocess.run([
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
            '-f', 'lavfi', '-i', 'testsrc2=size=1280x720:rate=12:duration=3',
            '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3',
            '-f', 'lavfi', '-i', 'sine=frequency=880:duration=3',
            '-map', '0:v', '-map', '1:a', '-map', '2:a', '-c:v', 'libx264',
            '-threads', '2', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
            '-metadata:s:a:0', 'language=en', '-metadata:s:a:1', 'language=hi', str(source),
        ], check=True, capture_output=True, timeout=60)
        assets = self.root / 'hd-hls-assets'
        assets.mkdir(exist_ok=True)
        with patch.object(self.worker, 'run', wraps=self.worker.run) as invoked:
            tracks, _ = self.worker._hls(source, assets, 'hd-synthetic')
        command = invoked.call_args.args[0]
        self.assertEqual(command[command.index('-c:v') + 1], 'copy')
        for forbidden in ('libx264', '-vf', '-b:v', '-maxrate', '-force_key_frames'):
            self.assertNotIn(forbidden, command)
        master = (assets / 'hls' / 'master.m3u8').read_text()
        self.assertEqual([track['language'] for track in tracks], ['en', 'hi'])
        self.assertIn('RESOLUTION=1280x720', master)
        self.assertEqual(master.count('#EXT-X-STREAM-INF:'), 1)
        self.assertNotIn('video-480', master)
        for playlist in ('video.m3u8', 'audio-0.m3u8', 'audio-1.m3u8'):
            self.assertIn('#EXT-X-ENDLIST', (assets / 'hls' / playlist).read_text())

        def frame_hashes(path):
            result = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', str(path),
                                     '-map', '0:v:0', '-f', 'framemd5', '-'], check=True,
                                    capture_output=True, timeout=30, text=True)
            return [line.rsplit(',', 1)[-1].strip() for line in result.stdout.splitlines()
                    if line and not line.startswith('#')]
        self.assertEqual(frame_hashes(source), frame_hashes(assets / 'hls' / 'video.m3u8'))

    def test_unsupported_codec_never_falls_back_to_video_encoding(self):
        assets = self.root / 'unsupported-assets'
        assets.mkdir(exist_ok=True)
        probe = {'streams': [{'codec_type': 'video', 'codec_name': 'mpeg2video', 'width': 720, 'height': 480}]}
        with patch.object(self.worker, 'run') as invoked, self.assertRaisesRegex(RuntimeError, 'Video conversion is disabled'):
            self.worker._hls(self.multi, assets, 'unsupported', probe=probe)
        invoked.assert_not_called()
        self.assertTrue(self.multi.is_file())

    def test_actual_preview_clip_and_time_aligned_sprite_sheet(self):
        assets = self.root / 'preview-assets'
        assets.mkdir(exist_ok=True)
        self.worker._make_card_assets(self.single, assets, self.worker.ffprobe(self.single))
        for name in ('poster.jpg', 'sprites.jpg', 'sprites.vtt', 'preview.mp4'):
            self.assertGreater((assets / name).stat().st_size, 20)
        preview = self.worker.ffprobe(assets / 'preview.mp4')
        self.assertFalse(any(s['codec_type'] == 'audio' for s in preview['streams']))
        self.assertLessEqual(float(preview['format']['duration']), 6.2)
        vtt = (assets / 'sprites.vtt').read_text()
        duration = float(self.worker.ffprobe(self.single)['format']['duration'])
        self.assertIn(f'00:00:00.000 --> {self.worker._stamp(duration / 2)}', vtt)
        self.assertIn('sprites.jpg#xywh=160,0,160,90', vtt)
        dimensions = self.worker.ffprobe(assets / 'sprites.jpg')['streams'][0]
        self.assertEqual((dimensions['width'], dimensions['height']), (320, 90))

    def test_process_pipeline_publishes_duration_audio_previews_and_cached_state(self):
        db = self.database('pipeline')
        self.video_job(db, 'pipeline')
        folder = self.data / 'cache' / 'pipeline'
        folder.mkdir(parents=True)
        shutil.copyfile(self.multi, folder / 'source.bin')
        self.worker.process_job(db, self.data, 'pipeline')
        with db.connect() as conn:
            video = dict(conn.execute("SELECT * FROM videos WHERE id='pipeline'").fetchone())
            job = dict(conn.execute("SELECT * FROM jobs WHERE id='pipeline'").fetchone())
        self.assertEqual(video['state'], 'cached', video.get('error'))
        self.assertEqual(job['state'], 'completed', job.get('error'))
        self.assertGreater(video['duration'], 11)
        tracks = json.loads(video['audio_tracks'])
        self.assertEqual([(track['language'], track['source_index']) for track in tracks], [('en', 0), ('hi', 1)])
        self.assertEqual(video['playback_mode'], 'hls')
        self.assertTrue((folder / 'sprites.vtt').is_file())

    def test_process_cancel_keeps_source_and_stops_before_ffmpeg(self):
        db = self.database('cancel')
        self.video_job(db, 'cancel')
        with db.transaction() as conn:
            conn.execute("UPDATE jobs SET cancel_requested=1 WHERE id='cancel'")
        self.worker.process_job(db, self.data, 'cancel')
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT state FROM jobs WHERE id='cancel'").fetchone()[0], 'cancelled')


if __name__ == '__main__':
    unittest.main(verbosity=2)
