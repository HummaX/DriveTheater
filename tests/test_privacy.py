from __future__ import annotations

import math
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from drivetheater.audit import clear_user_activity, prune_activity, record_watch
from drivetheater.db import Database, _initial_cache_limit_gb, decode_video
from drivetheater.subtitle_utils import normalize_caption, validated_language


class PrivacyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "db.sqlite3")
        self.db.initialize()
        now = "2026-01-01T00:00:00Z"
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO users(username,password_hash,created_at) VALUES ('alice','x',?)", (now,))
            conn.execute("INSERT INTO users(username,password_hash,created_at) VALUES ('bob','x',?)", (now,))
            conn.executemany(
                "INSERT INTO videos(id,title,source_kind,remote_id,updated_at) VALUES (?,?,?,?,?)",
                [("one", "One", "http", "one", now), ("two", "Two", "http", "two", now)],
            )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_cache_limit_accepts_only_finite_positive_environment_values(self) -> None:
        for raw, expected in (("12.5", "12.5"), ("0", "20"), ("-1", "20"), ("nan", "20"), ("inf", "20"), ("broken", "20")):
            with self.subTest(raw=raw), patch.dict(os.environ, {"DT_CACHE_LIMIT_GB": raw}):
                self.assertEqual(_initial_cache_limit_gb(), expected)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT value FROM settings WHERE key='cache_limit_gb'").fetchone()[0], "20")

    def test_clear_user_activity_is_user_scoped(self) -> None:
        record_watch(self.db, "alice", "one", 3.0, 10.0)
        record_watch(self.db, "bob", "two", 4.0, 10.0)
        cleared = clear_user_activity(self.db, "alice")
        self.assertEqual(cleared, {"watch_history": 1, "activity_events": 1})
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM watch_history WHERE username='alice'").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM activity_events WHERE username='alice'").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM watch_history WHERE username='bob'").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM activity_events WHERE username='bob'").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0], 2)

    def test_retention_prunes_stored_history_and_events(self) -> None:
        cutoff = int(time.time()) - 2 * 86400
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO activity_events(occurred_at,created_at,username,event_type,details) VALUES (?,?,'alice','watch','{}')", (cutoff, "old"))
            conn.execute("INSERT INTO watch_history(username,video_id,resume_position,last_played,last_watched_at) VALUES ('alice','one',0,'old',?)", (cutoff,))
            prune_activity(conn, 1)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM watch_history").fetchone()[0], 0)

    def test_watch_positions_reject_nonfinite_or_out_of_range_values(self) -> None:
        for position, duration in ((math.nan, 10), (math.inf, 10), (-1, 10), (12, 10), (0, math.nan), (0, math.inf), (0, 0)):
            with self.subTest(position=position, duration=duration):
                with self.assertRaises(ValueError):
                    record_watch(self.db, "alice", "one", position, duration)

    def test_video_api_excludes_legacy_pin_flag(self) -> None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM videos WHERE id='one'").fetchone()
        self.assertNotIn("pinned", decode_video(row))

    def test_captions_are_english_utf8_vtt_with_valid_cues(self) -> None:
        name, data = normalize_caption("episode.srt", b"1\n00:00:01,000 --> 00:00:02,500\nHello\n")
        self.assertEqual(name, "episode.vtt")
        self.assertEqual(data, b"WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.500\nHello\n")
        self.assertEqual(validated_language("EN"), "en")
        for language in ("fr", "und", "en-US"):
            with self.subTest(language=language), self.assertRaises(ValueError):
                validated_language(language)
        for malformed in (b"WEBVTT\n\n00:00:02.000 --> 00:00:01.000\nNo\n", b"WEBVTT\n\ntext only\n", b"WEBVTT\n\n00:00:01,000 --> 00:00:02,000\nNo\n"):
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                normalize_caption("episode.vtt", malformed)


if __name__ == "__main__":
    unittest.main()
