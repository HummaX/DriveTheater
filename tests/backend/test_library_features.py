from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from drivetheater.db import Database
from drivetheater import library
from fastapi import HTTPException


class LibraryFeatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "library.sqlite3")
        self.db.initialize()
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO users(username,password_hash,created_at) VALUES('one','x','now'),('two','x','now')")
            conn.execute("INSERT INTO sources(id,name,kind) VALUES('source','Drive','drive')")
            for identity, title in [
                ("one-video", "Alpha"), ("two-video", "Beta"), ("special", "Show S00E01 Special"),
                ("episode-two", "Show 0x02 Return"), ("movie", "A Movie"),
            ]:
                conn.execute("INSERT INTO videos(id,title,source_id,source_kind,remote_id,state,updated_at,duration) VALUES(?,?,?,'drive',?,'cloud','now',100)", (identity, title, "source", identity))

    def tearDown(self):
        self.tmp.cleanup()

    def item(self, username, video_id):
        return next(item for item in library.list_library(self.db, username) if item["id"] == video_id)

    def test_owner_isolation_collections_views_and_searches(self):
        collection = library.create_collection(self.db, "one", "Personal")
        library.add_collection_item(self.db, "one", collection["id"], "one-video", 0)
        with self.assertRaises(HTTPException):
            library.add_collection_item(self.db, "two", collection["id"], "two-video")
        library.update_collection(self.db, "one", collection["id"], "Renamed")
        self.assertEqual(library.list_collections(self.db, "one")[0]["name"], "Renamed")
        self.assertEqual(library.list_collection_items(self.db, "one", collection["id"]), [{"video_id": "one-video", "position": 0}])
        with self.assertRaises(HTTPException):
            library.list_collection_items(self.db, "two", collection["id"])
        library.remove_collection_item(self.db, "one", collection["id"], "one-video")
        library.add_collection_item(self.db, "one", collection["id"], "one-video")
        library.reorder_collection_items(self.db, "one", collection["id"], ["one-video"])
        view = library.create_saved_view(self.db, "one", "Unwatched", {"unwatched": True}, "title")
        updated = library.update_saved_view(self.db, "one", view["id"], "Movies", {"movies": True}, "year")
        self.assertEqual(updated, {"id": view["id"], "name": "Movies", "filters": {"movies": [True]}, "sort": "year"})
        self.assertEqual(library.list_saved_views(self.db, "one")[0]["name"], "Movies")
        self.assertEqual(library.list_saved_views(self.db, "two"), [])
        with self.assertRaises(HTTPException):
            library.update_saved_view(self.db, "two", view["id"], "Stolen", {}, "title")
        library.delete_saved_view(self.db, "one", view["id"])
        for index in range(24): library.record_search(self.db, "one", f"query {index}")
        searches = library.list_recent_searches(self.db, "one")
        self.assertEqual(len(searches), 20)
        library.delete_recent_search(self.db, "one", searches[0]["id"])
        library.clear_recent_searches(self.db, "one")
        self.assertEqual(library.list_recent_searches(self.db, "one"), [])
        library.delete_collection(self.db, "one", collection["id"])
        self.assertEqual(library.list_collections(self.db, "two"), [])

    def test_override_validation_and_explicit_null_reset(self):
        library.update_override(self.db, "one-video", {"title": "Local Alpha", "type": "series", "season": 0, "episode": 1})
        self.assertEqual(self.item("one", "one-video")["title"], "Local Alpha")
        self.assertEqual(library.update_override(self.db, "one-video", {"title": None}), {"type": "series", "season": 0, "episode": 1})
        self.assertEqual(self.item("one", "one-video")["title"], "Alpha")
        for patch in ({"type": "documentary"}, {"title": " "}, {"episode": 0}, {"season": 1000}, {"year": 1500}, {"genre": ["x"] * 33}, {"cast": [True]}):
            with self.subTest(patch=patch), self.assertRaises(HTTPException): library.update_override(self.db, "one-video", patch)

    def test_observation_requires_real_forward_duration_and_manual_is_authoritative(self):
        # One near-end seek has no preceding forward playback.
        observed = library.observe_playback(self.db, "one", "one-video", 99, 100, now=100)
        self.assertFalse(observed["watched"])
        # Paused ticks and backwards replay cannot add observed duration.
        self.assertEqual(library.observe_playback(self.db, "one", "one-video", 99, 100, now=110)["observed_seconds"], 0)
        self.assertEqual(library.observe_playback(self.db, "one", "one-video", 20, 100, now=111)["observed_seconds"], 0)
        # Ordinary forward playback accrues only moved seconds, then reaches the configured 90% threshold.
        for position in range(30, 101, 10):
            observed = library.observe_playback(self.db, "one", "one-video", position, 100, now=position + 100)
        self.assertFalse(observed["watched"], "short prior playback plus a near-end seek must not complete")
        library.set_watched(self.db, "one", "one-video", False)
        library.observe_playback(self.db, "one", "one-video", 100, 100, now=300)
        self.assertFalse(self.item("one", "one-video")["watched"])
        library.set_watched(self.db, "one", "one-video", True)
        self.assertTrue(self.item("one", "one-video")["watched"])
        for value in (float("nan"), float("inf"), True):
            with self.subTest(value=value), self.assertRaises(HTTPException): library.observe_playback(self.db, "one", "one-video", value, 100)

    def test_boolean_filters_inference_next_and_smart_filters(self):
        special = self.item("one", "special")
        self.assertEqual((special["series"], special["season"], special["episode"]), ("Show", 0, 1))
        self.assertIsNone(library.next_episode(self.db, 'one', 'special'))
        with self.db.transaction() as conn:
            conn.execute("UPDATE videos SET state='cached',playback_mode='direct',playback_url='/api/media/episode-two/stream' WHERE id='episode-two'")
        self.assertEqual(library.next_episode(self.db, "one", "special")["id"], "episode-two")
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
        library.set_watched(self.db, "one", "special", True)
        watched = library.list_library(self.db, "one", {"watched": "true"})
        self.assertIn("special", [item["id"] for item in watched])
        false_items = library.list_library(self.db, "one", {"watched": "false"})
        self.assertNotIn("special", [item["id"] for item in false_items])
        self.assertEqual(library.list_library(self.db, "one", {"series": True})[0]["type"], "series")
        self.assertTrue(all(item["type"] == "movie" for item in library.list_library(self.db, "one", {"movies": True})))
        with self.assertRaises(HTTPException): library.validate_filters({"watched": "maybe"})

    def test_replaying_short_section_and_client_duration_cannot_complete(self):
        library.observe_playback(self.db, 'one', 'one-video', 0, 100, now=100)
        for tick in range(1, 30):
            position = 10 if tick % 2 else 0
            result = library.observe_playback(self.db, 'one', 'one-video', position, 1 if position == 0 else 100, now=100 + tick * 10)
        result = library.observe_playback(self.db, 'one', 'one-video', 99, 100, now=401)
        self.assertFalse(result['watched'])
        self.assertLessEqual(result['observed_seconds'], 10)

    def test_normal_forward_playback_completes_and_resume_is_user_scoped(self):
        library.observe_playback(self.db, 'one', 'one-video', 0, 100, now=100)
        for tick in range(1, 10):
            result = library.observe_playback(self.db, 'one', 'one-video', tick * 10, 100, now=100 + tick * 10)
        self.assertTrue(result['watched'])
        self.assertFalse(self.item('two', 'one-video')['watched'])
        self.assertEqual(self.item('two', 'one-video')['watch_position'], 0)
        library.observe_playback(self.db, 'one', 'two-video', 30, 100, now=300)
        with self.db.transaction() as conn:
            conn.execute('DELETE FROM watch_history')
        self.assertEqual(self.item('one', 'two-video')['watch_position'], 30)
        library.update_override(self.db, 'two-video', {'cast': ['Synthetic Actor'], 'year': 2026})
        self.assertEqual(library.list_library(self.db, 'one', query='Synthetic Actor')[0]['id'], 'two-video')
        self.assertEqual(library.list_library(self.db, 'one', query='2026')[0]['id'], 'two-video')

    def test_download_preview_lists_exact_selection_and_omits_active_or_cached(self):
        with self.db.transaction() as conn:
            conn.execute("UPDATE videos SET source_url='https://private.example/?token=SECRET',size_bytes=123 WHERE id='one-video'")
            conn.execute("UPDATE videos SET state='cached',playback_url='/api/media/movie/stream' WHERE id='movie'")
            conn.execute("INSERT INTO jobs(id,video_id,title,state,phase,updated_at) VALUES('existing','special','Special','queued','queued','now')")
        plan = library.create_download_plan(self.db, 'one', ['two-video', 'one-video', 'special', 'movie', 'two-video'])
        self.assertEqual(plan['video_ids'], ['two-video', 'one-video'])
        self.assertEqual(plan['excluded'], ['special', 'movie'])
        self.assertEqual(plan['known_bytes'], 123)
        self.assertEqual(plan['unknown_count'], 1)
        self.assertEqual([item['id'] for item in plan['items']], ['two-video', 'one-video', 'special', 'movie'])
        self.assertEqual([item['excluded_reason'] for item in plan['items']], [None, None, 'active_job', 'already_playable'])
        self.assertNotIn('source_fingerprints', plan)
        self.assertNotIn('SECRET', str(plan))
        self.assertNotIn('https://private.example', str(plan))
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 1)

    def test_changed_download_source_or_size_rejects_entire_selection(self):
        fields = [('source_url', 'https://changed.example/?token=SECRET'), ('remote_id', 'changed-identity'), ('source_kind', 'http'), ('size_bytes', 999)]
        for field, value in fields:
            with self.subTest(field=field):
                plan = library.create_download_plan(self.db, 'one', ['one-video', 'two-video'])
                with self.db.transaction() as conn:
                    original = conn.execute(f"SELECT {field} FROM videos WHERE id='two-video'").fetchone()[0]
                    conn.execute(f"UPDATE videos SET {field}=? WHERE id='two-video'", (value,))
                with self.assertRaises(HTTPException) as rejected:
                    library.confirm_download_plan(self.db, 'one', plan['id'])
                self.assertEqual(rejected.exception.status_code, 409)
                with self.db.connect() as conn:
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
                with self.db.transaction() as conn:
                    conn.execute(f"UPDATE videos SET {field}=? WHERE id='two-video'", (original,))

    def test_job_started_after_preview_is_excluded_not_reported_as_new(self):
        plan = library.create_download_plan(self.db, 'one', ['one-video'])
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO jobs(id,video_id,title,state,phase,updated_at) VALUES('existing','one-video','Alpha','queued','queued','now')")
        outcome = library.confirm_download_plan(self.db, 'one', plan['id'])
        self.assertEqual(outcome, {'queued_job_ids': [], 'excluded': ['one-video']})
        with self.assertRaises(HTTPException):
            library.confirm_download_plan(self.db, 'one', plan['id'])
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 1)

    def test_download_plan_is_bound_to_its_owner(self):
        plan = library.create_download_plan(self.db, 'one', ['one-video'])
        with self.assertRaises(HTTPException) as rejected:
            library.confirm_download_plan(self.db, 'two', plan['id'])
        self.assertEqual(rejected.exception.status_code, 404)
        self.assertEqual(len(library.confirm_download_plan(self.db, 'one', plan['id'])['queued_job_ids']), 1)

    def test_caption_bounds_replacement_and_plan_replay(self):
        self.assertEqual(library.index_caption(self.db, "one-video", "en", "WEBVTT\n\n00:00.000 --> 00:02.000\nneedle phrase\n"), 1)
        self.assertEqual(library.index_caption(self.db, "one-video", "en", "WEBVTT\n\n00:03.000 --> 00:04.000\nreplacement\n"), 1)
        self.assertEqual(library.list_library(self.db, "one", query="needle"), [])
        self.assertEqual(library.remove_caption_index(self.db, "one-video", "en"), 1)
        with self.assertRaises(HTTPException): library.index_caption(self.db, "one-video", "en", "x" * (512 * 1024 + 1))
        plan = library.create_download_plan(self.db, "one", ["one-video", "one-video"])
        outcome = library.confirm_download_plan(self.db, "one", plan["id"])
        self.assertEqual(len(outcome["queued_job_ids"]), 1)
        with self.assertRaises(HTTPException): library.confirm_download_plan(self.db, "one", plan["id"])
        expired = library.create_download_plan(self.db, "one", ["two-video"], ttl_seconds=30)
        with self.db.transaction() as conn: conn.execute("UPDATE download_plans SET expires_at=0 WHERE id=?", (expired["id"],))
        with self.assertRaises(HTTPException): library.confirm_download_plan(self.db, "one", expired["id"])


if __name__ == "__main__": unittest.main()
