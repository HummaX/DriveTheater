from __future__ import annotations

import asyncio
import io
import os
import socket
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from fastapi import HTTPException, UploadFile
from starlette.requests import Request

from drivetheater import metadata_features
from drivetheater.db import Database


class _Response:
    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        return None

    def iter_bytes(self):
        yield self.body


class _Client:
    body = b"{}"
    urls: list[str] = []

    def __init__(self, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def stream(self, method, url, **_kwargs):
        self.urls.append(f"{method} {url}")
        return _Response(self.body)


def _chunk(kind: bytes, payload: bytes, crc: int | None = None) -> bytes:
    checksum = zlib.crc32(kind + payload) & 0xFFFFFFFF if crc is None else crc
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)


def _png(width: int = 2, height: int = 3, *, chunks: list[tuple[bytes, bytes]] | None = None, trailing: bytes = b"") -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = b"".join(b"\0" + b"\x11\x22\x33" * width for _ in range(height))
    encoded = [(b"IHDR", ihdr), *(chunks or [(b"IDAT", zlib.compress(pixels))]), (b"IEND", b"")]
    return b"\x89PNG\r\n\x1a\n" + b"".join(_chunk(kind, payload) for kind, payload in encoded) + trailing


class MetadataFeatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "metadata.sqlite3")
        self.db.initialize()
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO sources(id,name,kind) VALUES('source','Drive','drive')")
            for identity, title in (("feature", "Show S00E01 Feature"), ("return", "Show 0x02 Return"), ("other", "Other Movie")):
                conn.execute("INSERT INTO videos(id,title,source_id,source_kind,remote_id,state,updated_at,duration) VALUES(?,?,?,'drive',?,'cloud','now',100)", (identity, title, "source", identity))
            conn.execute("INSERT INTO users(username,password_hash,created_at) VALUES('owner','x','now')")
            conn.execute("INSERT INTO settings(key,value) VALUES('metadata_manifest_url','https://metadata.example/manifest.json')")
            # These are established feature-schema tables. Create their contract here
            # because this focused unit test deliberately initializes only Database.
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS user_preferences(username TEXT PRIMARY KEY, body TEXT);
                CREATE TABLE IF NOT EXISTS metadata_records(
                    id TEXT PRIMARY KEY, provider TEXT NOT NULL, media_type TEXT NOT NULL,
                    remote_id TEXT NOT NULL, language TEXT NOT NULL, body TEXT NOT NULL,
                    fetched_at INTEGER NOT NULL, UNIQUE(provider, media_type, remote_id, language)
                );
                CREATE TABLE IF NOT EXISTS video_metadata(video_id TEXT PRIMARY KEY, metadata_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS series_inventory(
                    metadata_id TEXT NOT NULL, season INTEGER NOT NULL, episode INTEGER NOT NULL,
                    episode_id TEXT NOT NULL, title TEXT NOT NULL, released TEXT, thumbnail TEXT,
                    PRIMARY KEY(metadata_id, season, episode)
                );
                CREATE TABLE IF NOT EXISTS video_overrides(video_id TEXT PRIMARY KEY, body TEXT NOT NULL, updated_at INTEGER NOT NULL);
            """)
        self.config = SimpleNamespace(allow_private_sources=False, data_dir=self.tmp.name)
        self.request = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "app": SimpleNamespace(state=SimpleNamespace(db=self.db, config=self.config))})

    def tearDown(self):
        self.tmp.cleanup()

    def provider_body(self, series: bool = False):
        meta = {
            "id": "tt1234567", "name": "The Show", "releaseInfo": "2025", "genres": ["Drama", "Drama", "x" * 200],
            "cast": ["Alice", "Bob"], "director": ["Director"], "runtime": "60", "imdbRating": "8.4",
            "poster": "https://images.example/poster.jpg", "description": "A test series", "trailers": [{"url": "https://video.example/trailer"}],
        }
        if series:
            meta["videos"] = [
                {"id": "tt1234567:0:1", "season": 0, "episode": 1, "title": "Special", "released": "2020-01-01"},
                {"id": "tt1234567:0:2", "season": 0, "episode": 2, "title": "Future", "released": "2099-01-01"},
                {"id": "tt1234567:0:3", "season": 0, "episode": 3, "title": "Unknown", "released": "not-a-date"},
            ]
        return {"meta": meta}

    def test_preview_uses_mocked_httpx_only_when_explicit(self):
        _Client.urls = []
        _Client.body = __import__("json").dumps(self.provider_body()).encode()
        with patch("drivetheater.addons.httpx.Client", _Client), patch(
            "drivetheater.sources.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
        ):
            candidate = metadata_features.preview(self.db, "owner", self.request, "movie", "tt1234567")
        self.assertEqual(candidate["title"], "The Show")
        self.assertEqual(candidate["requested_language"], "en")
        self.assertEqual(candidate["genre"], ["Drama", "x" * 120])
        self.assertEqual(candidate["rating"], 8.4)
        self.assertEqual(_Client.urls, ["GET https://metadata.example/meta/movie/tt1234567.json"])
        self.assertIsNone(metadata_features.local_metadata(self.db, "feature"))
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)

    def test_confirmed_series_is_persisted_and_inventory_states_are_local(self):
        _Client.urls = []
        _Client.body = __import__("json").dumps(self.provider_body(series=True)).encode()
        with patch("drivetheater.addons.httpx.Client", _Client), patch(
            "drivetheater.sources.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
        ):
            saved = metadata_features.fetch_and_store(self.db, "owner", self.request, "feature", "series", "tt1234567")
        self.assertEqual(saved["episodes"][0]["season"], 0)
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO video_overrides(video_id,body,updated_at) VALUES('feature','{\"series\":\"The Show\",\"season\":0,\"episode\":1}',0)")
        self.assertEqual(metadata_features.local_metadata(self.db, "feature")["media_id"], "tt1234567")
        inventory = metadata_features.series_episodes(self.db, "feature", today=__import__("datetime").date(2026, 1, 1))["episodes"]
        self.assertEqual([item["status"] for item in inventory], ["available", "future", "unknown"])
        # Local inventory rendering does not contact the provider.
        self.assertEqual(_Client.urls, ["GET https://metadata.example/meta/series/tt1234567.json"])
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)

    def test_related_titles_are_local_and_rank_shared_metadata(self):
        first = metadata_features.normalize_metadata(self.provider_body(), "movie", "tt1234567", "en")
        metadata_features.store_fetch(self.db, "feature", first)
        second = {**first, "title": "Related", "media_id": "tt7654321", "imdb_id": "tt7654321", "genre": ["Drama"], "genres": ["Drama"], "cast": ["Alice"]}
        metadata_features.store_fetch(self.db, "other", second)
        related = metadata_features.related_local(self.db, "feature")
        self.assertEqual(related[0]["video_id"], "other")
        self.assertEqual(set(related[0]["shared"]), {"alice", "drama"})

    def test_png_validation_rejects_malicious_or_malformed_inputs(self):
        valid = _png()
        self.assertEqual(metadata_features._image_dimensions(valid), ("png", 2, 3))
        bad_crc = bytearray(valid)
        bad_crc[-1] ^= 1
        pixels = b"\0" + b"\x11\x22\x33" * 2
        expands_too_far = _png(1, 1, chunks=[(b"IDAT", zlib.compress(b"\0\x11\x22\x33" + b"x" * 100_000))])
        invalid = (
            b"<svg></svg>", valid[:-1], bytes(bad_crc), _png(9000, 1),
            _png(chunks=[(b"acTL", b"\0" * 8), (b"IDAT", zlib.compress(pixels))]),
            _png(chunks=[(b"IDAT", b"not-zlib")]), expands_too_far,
            _png(trailing=b"<script>polyglot</script>"),
        )
        for payload in invalid:
            with self.subTest(payload=payload[:12]), self.assertRaises(HTTPException):
                metadata_features._image_dimensions(payload)

    def test_custom_poster_uses_canonical_png_and_replaces_atomically(self):
        first = asyncio.run(metadata_features.save_custom_poster(self.db, self.config, "feature", UploadFile(filename="first.png", file=io.BytesIO(_png()))))
        first_bytes = metadata_features.custom_poster_bytes(self.db, self.config, "feature")
        second = asyncio.run(metadata_features.save_custom_poster(self.db, self.config, "feature", UploadFile(filename="second.png", file=io.BytesIO(_png(3, 2)))))
        second_bytes = metadata_features.custom_poster_bytes(self.db, self.config, "feature")
        artwork = Path(self.tmp.name) / "artwork"
        expected = metadata_features._artwork_name("feature")
        self.assertEqual((first["width"], first["height"]), (2, 3))
        self.assertEqual((second["width"], second["height"]), (3, 2))
        self.assertNotEqual(first_bytes, second_bytes)
        self.assertEqual([entry.name for entry in artwork.iterdir()], [expected])
        with self.db.connect() as conn:
            override = __import__("json").loads(conn.execute("SELECT body FROM video_overrides WHERE video_id='feature'").fetchone()[0])
        self.assertEqual(override["poster_artwork"], expected)

    def test_custom_poster_rejects_hardlinks(self):
        asyncio.run(metadata_features.save_custom_poster(self.db, self.config, "feature", UploadFile(filename="poster.png", file=io.BytesIO(_png()))))
        path, external = Path(self.tmp.name) / "artwork" / metadata_features._artwork_name("feature"), Path(self.tmp.name) / "outside.png"
        external.write_bytes(_png())
        path.unlink()
        os.link(external, path)
        with self.assertRaises(HTTPException):
            metadata_features.custom_poster_bytes(self.db, self.config, "feature")

    def test_custom_poster_rejects_symlinks(self):
        asyncio.run(metadata_features.save_custom_poster(self.db, self.config, "feature", UploadFile(filename="poster.png", file=io.BytesIO(_png()))))
        path, external = Path(self.tmp.name) / "artwork" / metadata_features._artwork_name("feature"), Path(self.tmp.name) / "outside.png"
        external.write_bytes(_png())
        path.unlink()
        try:
            os.symlink(external, path)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaises(HTTPException):
            metadata_features.custom_poster_bytes(self.db, self.config, "feature")


if __name__ == "__main__":
    unittest.main()
