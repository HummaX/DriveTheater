from __future__ import annotations

import tempfile
import unittest
import os
import sys
import atexit
from pathlib import Path
from unittest.mock import patch

# Keep the module-level ASGI instance isolated as well as each per-test app.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))
_import_runtime = tempfile.TemporaryDirectory(prefix='drivetheater-backend-import-')
atexit.register(_import_runtime.cleanup)
os.environ.setdefault('DT_DATA_DIR', _import_runtime.name)
os.environ.setdefault('DT_ADMIN_PASSWORD', 'isolated-backend-test-password')
os.environ.setdefault('DT_SECRET', 'isolated-backend-test-secret-000000000000')

from fastapi.testclient import TestClient

from drivetheater.config import Config
from drivetheater.main import create_app
from drivetheater.media import parse_range, safe_asset
from drivetheater.sources import GoogleAdapter, normalize_oauth_client_config


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Config(Path(self.tmp.name), "admin", "correct horse battery staple", "x" * 32, False, True, None)
        self.client = TestClient(create_app(cfg))
        result = self.client.post("/api/auth/login", json={"username":"admin","password":"correct horse battery staple"})
        self.assertEqual(result.status_code, 200)

    def tearDown(self): self.tmp.cleanup()

    def add_video(self) -> str:
        result = self.client.post("/api/urls", json={"url":"http://127.0.0.1/example.mp4","engine":"http","title":"Fixture"})
        self.assertEqual(result.status_code, 200)
        return result.json()["id"]

    def test_original_device_download_supports_resume_and_protects_cache(self):
        video = self.add_video()
        app = self.client.app
        path = app.state.config.media_dir / video / 'source.bin'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'original-file-bytes')
        with app.state.db.transaction() as conn:
            conn.execute("UPDATE videos SET local_path=?,state='cached',mime_type='video/mp4',content_sha256=? WHERE id=?", (str(path), 'a' * 64, video))
        url = f'/api/media/{video}/download'
        self.assertEqual(self.client.get(f'/api/videos/{video}').json()['download_url'], url)
        head = self.client.head(url)
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.headers['content-length'], '19')
        self.assertIn('attachment;', head.headers['content-disposition'])
        response = self.client.get(url, headers={'Range': 'bytes=9-12', 'If-Range': head.headers['etag']})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b'file')
        self.assertEqual(response.headers['content-range'], 'bytes 9-12/19')
        stale = self.client.get(url, headers={'Range': 'bytes=9-12', 'If-Range': '"old-file"'})
        self.assertEqual(stale.status_code, 200)
        self.assertEqual(stale.content, b'original-file-bytes')
        self.assertEqual(self.client.delete(f'/api/videos/{video}/cache').status_code, 409)
        with TestClient(app) as anonymous:
            self.assertEqual(anonymous.get(url).status_code, 401)

    def test_device_download_never_queues_cloud_or_incomplete_media(self):
        video = self.add_video()
        url = f'/api/media/{video}/download'
        self.assertEqual(self.client.get(url).status_code, 409)
        self.assertEqual(self.client.get('/api/jobs').json()['items'], [])
        self.assertIsNone(self.client.get(f'/api/videos/{video}').json()['download_url'])
        path = self.client.app.state.config.media_dir / video / 'source.bin.part'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'partial')
        with self.client.app.state.db.transaction() as conn:
            conn.execute("UPDATE videos SET local_path=?,state='playable' WHERE id=?", (str(path), video))
        self.assertEqual(self.client.get(url).status_code, 409)
        self.assertIsNone(self.client.get(f'/api/videos/{video}').json()['download_url'])

    def test_discover_catalog_and_saved_stream_lookup_never_queue_media(self):
        manifest = 'https://provider.example/private-fixture-token/manifest.json'
        with patch('drivetheater.addons.validate_url', side_effect=lambda value, *_: value):
            saved = self.client.post('/api/discover/config', json={'torrentio_manifest_url': manifest})
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()['torrentio_configured'], True)
        self.assertNotIn('private-fixture-token', saved.text)
        self.assertNotIn('private-fixture-token', self.client.get('/api/discover/config').text)
        with patch('drivetheater.addons.bounded_json_fetch', return_value={'metas': [
            {'id': 'tt1234567', 'name': 'Synthetic movie', 'type': 'movie', 'imdbRating': '8.1'},
            {'id': 'not-an-imdb-id', 'name': 'Invalid'},
        ]}) as fetch:
            catalog = self.client.post('/api/discover/catalog', json={'query': 'A & B', 'skip': 0})
        self.assertEqual(catalog.status_code, 200)
        self.assertEqual([item['title'] for item in catalog.json()['items']], ['Synthetic movie'])
        self.assertIn('search=A%20%26%20B.json', fetch.call_args.args[0])
        with patch('drivetheater.addons.bounded_json_fetch', return_value={'streams': [{'infoHash': 'b' * 40, 'title': 'Authorized fixture'}]}) as fetch:
            streams = self.client.post('/api/torrentio/streams', json={'media_type': 'movie', 'media_id': 'tt1234567'})
        self.assertEqual(streams.status_code, 200)
        self.assertIn('/private-fixture-token/stream/movie/tt1234567.json', fetch.call_args.args[0])
        self.assertEqual(self.client.get('/api/jobs').json()['items'], [])

    def test_play_reuses_one_live_job(self):
        video = self.add_video()
        one = self.client.post(f"/api/videos/{video}/play").json()
        two = self.client.post(f"/api/videos/{video}/download").json()
        self.assertEqual(one["job"]["id"], two["job"]["id"])
        self.assertEqual(len(self.client.get("/api/jobs").json()["items"]), 1)

    def test_range_stream_and_pending_cache(self):
        video = self.add_video()
        self.assertEqual(self.client.get(f"/api/media/{video}/stream").status_code, 503)
        app = self.client.app
        path = app.state.config.media_dir / video / "source.mp4"; path.parent.mkdir(parents=True); path.write_bytes(b"abcdef")
        with app.state.db.transaction() as db:
            db.execute("UPDATE videos SET local_path=?,mime_type='video/mp4',state='cached',size_bytes=6 WHERE id=?", (str(path), video))
        response = self.client.get(f"/api/media/{video}/stream", headers={"Range":"bytes=1-3"})
        self.assertEqual(response.status_code, 206); self.assertEqual(response.content, b"bcd")
        self.assertEqual(response.headers["content-range"], "bytes 1-3/6")

    def test_pin_endpoint_is_removed_and_active_cache_is_protected(self):
        video = self.add_video()
        self.assertEqual(self.client.post(f"/api/videos/{video}/pin", json={"pinned": True}).status_code, 404)
        path = self.client.app.state.config.media_dir / video / "source.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"cached")
        with self.client.app.state.db.transaction() as conn:
            conn.execute("UPDATE videos SET local_path=?,state='cached',active_until=? WHERE id=?", (str(path), 2**31, video))
        self.assertEqual(self.client.delete(f"/api/videos/{video}/cache").status_code, 409)

    def test_login_rate_limit_uses_peer_and_username(self):
        for _ in range(5):
            response = self.client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "wrong"},
                headers={"X-Forwarded-For": "198.51.100.10"},
            )
            self.assertEqual(response.status_code, 401)
        blocked = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "wrong"},
            headers={"X-Forwarded-For": "203.0.113.20"},
        )
        self.assertEqual(blocked.status_code, 429)
        self.assertGreaterEqual(int(blocked.headers["retry-after"]), 1)
        self.assertEqual(blocked.json()["detail"], "invalid username or password")

    def test_activity_clear_is_scoped_to_authenticated_user(self):
        video = self.add_video()
        self.client.post(f"/api/videos/{video}/activity", json={"active": False, "position": 3, "duration": 10})
        response = self.client.post("/api/activity/clear")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(self.client.get("/api/watch-history").json()["items"], [])
        self.assertEqual(self.client.get("/api/activity").json()["items"], [])

    def test_play_jobs_are_interactive_but_download_jobs_are_not(self):
        playable = self.add_video()
        play = self.client.post(f"/api/videos/{playable}/play").json()
        self.assertEqual(play["job"]["interactive"], 1)
        second = self.client.post(
            "/api/urls", json={"url": "http://127.0.0.1/non-interactive.mp4", "engine": "http", "title": "Download fixture"}
        ).json()["id"]
        download = self.client.post(f"/api/videos/{second}/download").json()
        self.assertEqual(download["job"]["interactive"], 0)
        promoted = self.client.post(f"/api/videos/{second}/play").json()
        self.assertEqual(promoted["job"]["interactive"], 1)

    def test_mutations_reject_untrusted_origin_but_allow_cli_requests(self):
        rejected = self.client.post(
            "/api/activity/clear", headers={"Origin": "https://attacker.example"}
        )
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(self.client.post("/api/activity/clear").status_code, 200)


        self.assertEqual(parse_range("bytes=-2", 9), (7,8))
        self.assertEqual(parse_range("bytes=2-", 9), (2,8))
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp); (base/"ok.vtt").write_text("ok")
            self.assertEqual(safe_asset(base,"ok.vtt").read_text(),"ok")
            with self.assertRaises(FileNotFoundError): safe_asset(base,"../outside")

    def test_google_source_response_is_an_object_without_credentials(self):
        response = self.client.post("/api/sources/google", json={
            "name": "Metadata fixture",
            "folder": "folder-id",
            "auth_mode": "api_key",
            "api_key": "test-key",
        })
        self.assertEqual(response.status_code, 200)
        source = response.json()
        self.assertIsInstance(source, dict)
        self.assertTrue(source["has_credentials"])
        self.assertNotIn("credential_path", source)
        self.assertEqual(source["kind"], "google_drive")

    def test_spa_root_fallback_and_static_asset(self):
        with tempfile.TemporaryDirectory() as ui_tmp:
            ui = Path(ui_tmp)
            (ui / "assets").mkdir()
            (ui / "index.html").write_text("<main>DriveTheater</main>", encoding="utf-8")
            (ui / "assets" / "app.js").write_text("console.log('ok')", encoding="utf-8")
            with patch.dict("os.environ", {"DT_UI_DIR": str(ui)}):
                app = create_app(self.client.app.state.config)
                client = TestClient(app)
                self.assertEqual(client.get("/").text, "<main>DriveTheater</main>")
                self.assertEqual(client.get("/sources/view").text, "<main>DriveTheater</main>")
                self.assertEqual(client.get("/assets/app.js").text, "console.log('ok')")
                self.assertEqual(client.get("/api/not-a-route").status_code, 404)

    def test_oauth_web_config_refreshes_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            credential_path = Path(tmp) / "private-oauth.json"
            credentials = normalize_oauth_client_config({"web": {
                "client_id": "client-id",
                "client_secret": "client-secret",
                "refresh_token": "old-refresh",
                "access_token": "expired-token",
                "expires_at": 0,
            }})

            class RefreshResponse:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {
                        "access_token": "fresh-token",
                        "refresh_token": "fresh-refresh",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    }

            with patch("drivetheater.sources.httpx.post", return_value=RefreshResponse()) as post:
                token = GoogleAdapter("oauth", credentials, credential_path)._token()

            self.assertEqual(token, "fresh-token")
            self.assertTrue(post.called)
            persisted = __import__("json").loads(credential_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["access_token"], "fresh-token")
            self.assertEqual(persisted["refresh_token"], "fresh-refresh")
            self.assertGreater(persisted["expires_at"], 0)
            self.assertNotIn("web", persisted)

    def test_metadata_listing_only_uses_drive_files_endpoint(self):
        requests: list[tuple[str, dict, dict]] = []

        class MetadataResponse:
            def __init__(self, body):
                self.body = body

            def raise_for_status(self):
                return None

            def json(self):
                return self.body

        class MetadataClient:
            def __init__(self, *args, **kwargs):
                self.page = 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, url, *, params, headers):
                requests.append((url, dict(params), dict(headers)))
                self.page += 1
                if self.page == 1:
                    return MetadataResponse({"files": [{"id": "one", "mimeType": "video/mp4"}], "nextPageToken": "next"})
                return MetadataResponse({"files": [{"id": "two", "mimeType": "video/mp4"}]})

        with patch("drivetheater.sources.httpx.Client", MetadataClient):
            items = list(GoogleAdapter("api_key", {"api_key": "key"}).list_metadata("folder"))

        self.assertEqual([item["id"] for item in items], ["one", "two"])
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(url == "https://www.googleapis.com/drive/v3/files" for url, _, _ in requests))
        self.assertTrue(all("alt=media" not in url for url, _, _ in requests))
        self.assertEqual(requests[0][1]["supportsAllDrives"], "true")
        self.assertEqual(requests[0][1]["includeItemsFromAllDrives"], "true")
        self.assertEqual(requests[1][1]["pageToken"], "next")

    def test_server_activity_history_and_resume_position(self):
        video = self.add_video()
        server = self.client.get("/api/server")
        self.assertEqual(server.status_code, 200)
        self.assertIn("media_cpu", server.json())
        self.assertIn("counts", server.json())
        update = self.client.post(f"/api/videos/{video}/activity", json={"active": True, "position": 12.5, "duration": 60})
        self.assertEqual(update.status_code, 200)
        history = self.client.get("/api/watch-history").json()["items"]
        self.assertEqual(history[0]["video_id"], video)
        self.assertEqual(history[0]["resume_position"], 12.5)
        events = self.client.get("/api/activity?limit=10").json()
        self.assertEqual(events["items"][0]["event_type"], "watch")
        self.assertEqual(events["retention_days"], 30)

    def test_subtitle_upload_normalizes_srt_and_preserves_tracks(self):
        video = self.add_video()
        with self.client.app.state.db.transaction() as conn:
            conn.execute("UPDATE videos SET subtitle_tracks=? WHERE id=?", ('[{"language":"en","url":"/embedded.vtt"}]', video))
        response = self.client.post(
            f"/api/videos/{video}/subtitles?language=en",
            files={"file": ("episode.srt", b"1\n00:00:01,000 --> 00:00:02,500\nHola\n", "application/x-subrip")},
        )
        self.assertEqual(response.status_code, 200)
        tracks = response.json()["subtitle_tracks"]
        self.assertEqual(len(tracks), 2)
        self.assertTrue(tracks[-1]["url"].endswith(".vtt"))
        path = self.client.app.state.config.media_dir / video / "captions"
        content = next(path.glob("*.vtt")).read_text(encoding="utf-8")
        self.assertIn("WEBVTT", content)
        self.assertIn("00:00:01.000 --> 00:00:02.500", content)
        bad = self.client.post(f"/api/videos/{video}/subtitles", files={"file": ("bad.txt", b"no", "text/plain")})
        self.assertEqual(bad.status_code, 400)

    def test_auto_url_classifies_magnet_without_fetching(self):
        magnet = "magnet:?xt=urn:btih:0123456789012345678901234567890123456789"
        response = self.client.post("/api/urls", json={"url": magnet, "engine": "auto"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["source_kind"], "torrent")

    def test_url_registration_is_idempotent_and_reuses_live_job(self):
        payload = {"url": "http://127.0.0.1/repeat.mp4", "engine": "auto", "download": True}
        first = self.client.post("/api/urls", json=payload)
        self.assertEqual(first.status_code, 200)
        second = self.client.post("/api/urls", json=payload)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["reused"])
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(len(self.client.get("/api/videos").json()["items"]), 1)
        self.assertEqual(len(self.client.get("/api/jobs").json()["items"]), 1)

    def test_settings_alerts_and_user_watch_position(self):
        video = self.add_video()
        settings = self.client.get("/api/settings")
        self.assertEqual(settings.status_code, 200)
        self.assertFalse(settings.json()["download_schedule_enabled"])
        self.assertEqual(settings.json()["download_end_hour"], 6)
        self.assertTrue(settings.json()["auto_cache_cleanup"])
        patched = self.client.patch("/api/settings", json={
            "download_schedule_enabled": True, "download_start_hour": 22,
            "download_end_hour": 5, "download_limit_mbps": 12.5,
            "activity_retention_days": 31,
        })
        self.assertEqual(patched.status_code, 200)
        self.assertEqual(patched.json()["download_start_hour"], 22)
        self.assertEqual(self.client.patch("/api/settings", json={"download_start_hour": 24}).status_code, 422)
        self.assertEqual(self.client.patch("/api/settings", json={"activity_retention_days": 0}).status_code, 422)
        self.client.post(f"/api/videos/{video}/activity", json={"active": True, "position": 9.0, "duration": 60})
        with self.client.app.state.db.connect() as conn:
            lease = conn.execute("SELECT active_until,updated_at FROM videos WHERE id=?", (video,)).fetchone()
        self.client.post(f"/api/videos/{video}/activity", json={"active": False})
        with self.client.app.state.db.connect() as conn:
            later = conn.execute("SELECT active_until,updated_at FROM videos WHERE id=?", (video,)).fetchone()
        self.assertEqual(lease["active_until"], later["active_until"])
        self.assertEqual(lease["updated_at"], later["updated_at"])
        listed = self.client.get("/api/videos").json()["items"]
        self.assertEqual(listed[0]["watch_position"], 9.0)
        self.assertEqual(self.client.get("/api/alerts").status_code, 200)

    def test_subtitles_default_to_english_and_reject_other_languages(self):
        video = self.add_video()
        allowed = self.client.post(
            f"/api/videos/{video}/subtitles",
            files={"file": ("episode.srt", b"1\n00:00:01,000 --> 00:00:02,500\nHello\n", "application/x-subrip")},
        )
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.json()["subtitle_tracks"][-1]["language"], "en")
        rejected = self.client.post(
            f"/api/videos/{video}/subtitles?language=fr",
            files={"file": ("episode.srt", b"1\n00:00:01,000 --> 00:00:02,500\nBonjour\n", "application/x-subrip")},
        )
        self.assertEqual(rejected.status_code, 400)
