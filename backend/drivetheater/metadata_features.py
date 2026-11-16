"""Bounded, explicit metadata enrichment backed by existing metadata tables."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import struct
import tempfile
import time
import uuid
import zlib
from datetime import date
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import HTTPException, Request, UploadFile

from . import addons

_PROVIDER = "metadata-addon"
_MAX_TEXT = 6_000
_MAX_LIST = 32
_MAX_EPISODES = 2_000
_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_MAX_ARTWORK_BYTES = 16 * 1024 * 1024
_MAX_IMAGE_SIDE = 8_192
_MAX_IMAGE_PIXELS = 40_000_000
_IMDB_ID = re.compile(r"^tt\d{4,12}$")
_FILENAME_EPISODE = re.compile(r"^\s*(?P<series>.+?)[. _-]+(?:S(?P<season>\d{1,2})E(?P<episode>\d{1,3})|(?P<season_x>\d{1,2})x(?P<episode_x>\d{1,3}))(?:[. _-]|$)", re.I)


def _json(value: Any, default: Any) -> Any:
    try:
        parsed = json.loads(value or "")
        return parsed if isinstance(parsed, type(default)) else default
    except (TypeError, ValueError):
        return default


def _text(value: Any, limit: int = _MAX_TEXT) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value[:limit] if value else None


def _items(value: Any, limit: int = _MAX_LIST, item_limit: int = 120) -> list[str]:
    source = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in source[:limit]:
        cleaned = _text(item, item_limit)
        if cleaned and cleaned.casefold() not in {known.casefold() for known in result}:
            result.append(cleaned)
    return result


def _web_url(value: Any, limit: int = 4096) -> str | None:
    if not isinstance(value, str) or len(value) > limit:
        return None
    parsed = urlparse(value.strip())
    return value.strip() if parsed.scheme in {"https", "http"} and bool(parsed.netloc) and not parsed.username and not parsed.password else None


def _number(value: Any, low: float, high: float) -> float | int | None:
    if type(value) not in (int, float, str):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not low <= number <= high:
        return None
    return int(number) if number.is_integer() else round(number, 2)


def _trailers(meta: dict) -> list[str]:
    values = meta.get("trailerStreams", meta.get("trailers", meta.get("trailer")))
    values = values if isinstance(values, list) else [values]
    result: list[str] = []
    for item in values[:_MAX_LIST]:
        if isinstance(item, dict):
            item = item.get("url") or item.get("externalUrl")
        link = _web_url(item)
        if link and link not in result:
            result.append(link)
    return result[:8]


def _episodes(meta: dict, media_id: str) -> list[dict]:
    raw = meta.get("videos")
    if not isinstance(raw, list):
        return []
    result: list[dict] = []
    seen: set[tuple[int, int]] = set()
    for item in raw[:10_000]:
        if not isinstance(item, dict):
            continue
        season, episode = item.get("season"), item.get("episode")
        if type(season) is not int or type(episode) is not int or not 0 <= season <= 10_000 or not 1 <= episode <= 10_000:
            continue
        episode_id = item.get("id")
        if not isinstance(episode_id, str) or episode_id != f"{media_id}:{season}:{episode}" or (season, episode) in seen:
            continue
        seen.add((season, episode))
        result.append({"id": episode_id, "season": season, "episode": episode,
                       "title": _text(item.get("title"), 240) or f"S{season:02d}E{episode:02d}",
                       "released": _text(item.get("released"), 40), "thumbnail": _web_url(item.get("thumbnail"))})
        if len(result) >= _MAX_EPISODES:
            break
    return sorted(result, key=lambda item: (item["season"], item["episode"]))


def normalize_metadata(body: dict, media_type: Literal["movie", "series"], media_id: str, language: str) -> dict:
    """Normalize a Stremio-compatible response without trusting provider fields."""
    meta = body.get("meta")
    if not isinstance(meta, dict) or meta.get("id") != media_id:
        raise ValueError("Metadata add-on returned invalid metadata")
    title = _text(meta.get("name"), 500)
    if not title:
        raise ValueError("Metadata add-on did not provide a title")
    year_match = re.search(r"\b(18\d{2}|19\d{2}|20\d{2}|21\d{2}|22\d{2}|23\d{2}|24\d{2}|25\d{2}|26\d{2}|27\d{2}|28\d{2}|29\d{2}|3000)\b", str(meta.get("year", meta.get("releaseInfo", ""))))
    imdb_id = meta.get("imdb_id") if isinstance(meta.get("imdb_id"), str) else media_id
    normalized: dict[str, Any] = {"provider": _PROVIDER, "media_id": media_id, "imdb_id": imdb_id if _IMDB_ID.fullmatch(imdb_id) else media_id,
        "type": media_type, "requested_language": language, "title": title, "year": int(year_match.group(1)) if year_match else None,
        "genre": _items(meta.get("genres")), "cast": _items(meta.get("cast")), "director": _items(meta.get("director")),
        "runtime": _number(meta.get("runtime"), 0, 100_000), "rating": _number(meta.get("imdbRating", meta.get("rating")), 0, 10),
        "poster_url": _web_url(meta.get("poster")), "description": _text(meta.get("description")), "trailer_links": _trailers(meta)}
    normalized["genres"] = list(normalized["genre"])
    if media_type == "series":
        normalized["series"] = title
        normalized["episodes"] = _episodes(meta, media_id)
    return {key: value for key, value in normalized.items() if value is not None}


def _language(db, username: str, requested: str | None) -> str:
    candidate = requested
    if candidate is None:
        with db.connect() as conn:
            row = conn.execute("SELECT body FROM user_preferences WHERE username=?", (username,)).fetchone()
        candidate = _json(row["body"], {}).get("metadata_language", "en") if row else "en"
    if not isinstance(candidate, str) or not re.fullmatch(r"[a-z]{2,8}", candidate):
        raise HTTPException(422, "metadata language is invalid")
    return candidate


def _configured_manifest(request: Request) -> str:
    with request.app.state.db.connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='metadata_manifest_url'").fetchone()
    manifest = row["value"].strip() if row and isinstance(row["value"], str) else ""
    if not manifest:
        raise HTTPException(409, "A metadata add-on must be configured before lookup")
    try:
        addons._manifest_endpoint(manifest, "/manifest.json")
        addons.validate_url(manifest, request.app.state.config.allow_private_sources)
    except ValueError:
        raise HTTPException(400, "Configured metadata add-on is invalid") from None
    return manifest


def _remote_metadata(request: Request, media_type: Literal["movie", "series"], media_id: str) -> dict:
    manifest = _configured_manifest(request)
    endpoint = addons._manifest_endpoint(manifest, f"/meta/{media_type}/{media_id}.json")
    return addons.bounded_json_fetch(endpoint, request)


def preview(db, username: str, request: Request, media_type: Literal["movie", "series"], media_id: str, language: str | None = None) -> dict:
    """Explicit remote lookup. It never writes metadata or schedules media work."""
    if not _IMDB_ID.fullmatch(media_id):
        raise HTTPException(422, "Invalid IMDb ID")
    selected_language = _language(db, username, language)
    try:
        return normalize_metadata(_remote_metadata(request, media_type, media_id), media_type, media_id, selected_language)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, "Metadata lookup failed. Check the configured metadata add-on.") from exc


def _video_exists(conn: sqlite3.Connection, video_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT id,title FROM videos WHERE id=?", (video_id,)).fetchone()
    if not row:
        raise HTTPException(404, "video not found")
    return row


def store_fetch(db, video_id: str, metadata: dict) -> dict:
    """Persist a confirmed preview using metadata_records/video_metadata/inventory only."""
    media_id, media_type, language = metadata.get("media_id"), metadata.get("type"), metadata.get("requested_language")
    if not isinstance(media_id, str) or not _IMDB_ID.fullmatch(media_id) or media_type not in {"movie", "series"} or not isinstance(language, str):
        raise HTTPException(422, "Invalid metadata candidate")
    stored = dict(metadata)
    episodes = stored.pop("episodes", [])
    now = int(time.time())
    with db.transaction() as conn:
        _video_exists(conn, video_id)
        record = conn.execute("SELECT id FROM metadata_records WHERE provider=? AND media_type=? AND remote_id=? AND language=?", (_PROVIDER, media_type, media_id, language)).fetchone()
        record_id = record["id"] if record else uuid.uuid4().hex
        if record:
            conn.execute("UPDATE metadata_records SET body=?,fetched_at=? WHERE id=?", (json.dumps(stored, separators=(",", ":")), now, record_id))
        else:
            conn.execute("INSERT INTO metadata_records(id,provider,media_type,remote_id,language,body,fetched_at) VALUES(?,?,?,?,?,?,?)", (record_id, _PROVIDER, media_type, media_id, language, json.dumps(stored, separators=(",", ":")), now))
        conn.execute("INSERT INTO video_metadata(video_id,metadata_id) VALUES(?,?) ON CONFLICT(video_id) DO UPDATE SET metadata_id=excluded.metadata_id", (video_id, record_id))
        if media_type == "series":
            conn.execute("DELETE FROM series_inventory WHERE metadata_id=?", (record_id,))
            conn.executemany("INSERT INTO series_inventory(metadata_id,season,episode,episode_id,title,released,thumbnail) VALUES(?,?,?,?,?,?,?)", [(record_id, item["season"], item["episode"], item["id"], item["title"], item.get("released"), item.get("thumbnail")) for item in episodes])
    return {**stored, "episodes": episodes, "fetched_at": now}


def fetch_and_store(db, username: str, request: Request, video_id: str, media_type: Literal["movie", "series"], media_id: str, language: str | None = None) -> dict:
    with db.connect() as conn:
        _video_exists(conn, video_id)
    return store_fetch(db, video_id, preview(db, username, request, media_type, media_id, language))


def local_metadata(db, video_id: str) -> dict | None:
    with db.connect() as conn:
        _video_exists(conn, video_id)
        row = conn.execute("SELECT m.id,m.provider,m.media_type,m.remote_id,m.language,m.body,m.fetched_at FROM video_metadata vm JOIN metadata_records m ON m.id=vm.metadata_id WHERE vm.video_id=?", (video_id,)).fetchone()
    if not row:
        return None
    return {"record_id": row["id"], "provider": row["provider"], "media_type": row["media_type"], "media_id": row["remote_id"], "requested_language": row["language"], "fetched_at": row["fetched_at"], **_json(row["body"], {})}


def _episode_hint(title: Any) -> tuple[str, int, int] | None:
    if not isinstance(title, str): return None
    match = _FILENAME_EPISODE.match(title)
    if not match: return None
    series = re.sub(r"[._-]+", " ", match.group("series")).strip()
    return (series.casefold(), int(match.group("season") or match.group("season_x")), int(match.group("episode") or match.group("episode_x"))) if series else None


def _released_state(released: str | None, today: date) -> str:
    if not released: return "unknown"
    try: release_date = date.fromisoformat(released[:10])
    except ValueError: return "unknown"
    return "future" if release_date > today else "released"


def series_episodes(db, video_id: str, today: date | None = None) -> dict:
    metadata = local_metadata(db, video_id)
    if not metadata or metadata.get("media_type") != "series" or not isinstance(metadata.get("record_id"), str): raise HTTPException(404, "No local series metadata")
    today, series_name = today or date.today(), str(metadata.get("series") or metadata.get("title") or "").casefold()
    with db.connect() as conn:
        inventory = conn.execute("SELECT season,episode,episode_id,title,released,thumbnail FROM series_inventory WHERE metadata_id=? ORDER BY season,episode", (metadata["record_id"],)).fetchall()
        videos = conn.execute("SELECT id,title FROM videos WHERE removed_from_source=0").fetchall()
        override_rows = conn.execute("SELECT video_id,body FROM video_overrides").fetchall()
    overrides, available = {row["video_id"]: _json(row["body"], {}) for row in override_rows}, {}
    for video in videos:
        override, hint = overrides.get(video["id"], {}), _episode_hint(video["title"])
        candidate_series = override.get("series") if isinstance(override.get("series"), str) else (hint[0] if hint else "")
        season = override.get("season") if type(override.get("season")) is int else (hint[1] if hint else None)
        episode = override.get("episode") if type(override.get("episode")) is int else (hint[2] if hint else None)
        if isinstance(candidate_series, str) and candidate_series.casefold() == series_name and type(season) is int and type(episode) is int: available[(season, episode)] = video["id"]
    episodes = []
    for item in inventory:
        release_state, local_id = _released_state(item["released"], today), available.get((item["season"], item["episode"]))
        status = "available" if local_id else ("future" if release_state == "future" else "missing" if release_state == "released" else "unknown")
        episodes.append({**dict(item), "video_id": local_id, "status": status})
    return {"series": metadata.get("series") or metadata.get("title"), "episodes": episodes}


def related_local(db, video_id: str, limit: int = 12) -> list[dict]:
    if type(limit) is not int or not 1 <= limit <= 24: raise HTTPException(422, "Invalid related-title limit")
    selected = local_metadata(db, video_id)
    if not selected: return []
    wanted = {value.casefold() for value in _items(selected.get("genre")) + _items(selected.get("cast"))}
    if not wanted: return []
    with db.connect() as conn:
        rows = conn.execute("SELECT vm.video_id,m.body FROM video_metadata vm JOIN metadata_records m ON m.id=vm.metadata_id WHERE vm.video_id<>? ORDER BY m.fetched_at DESC LIMIT 1000", (video_id,)).fetchall()
    related: list[tuple[int, dict]] = []
    for row in rows:
        item = _json(row["body"], {})
        shared = wanted & {value.casefold() for value in _items(item.get("genre")) + _items(item.get("cast"))}
        if shared: related.append((len(shared), {"video_id": row["video_id"], "title": item.get("title"), "year": item.get("year"), "poster_url": item.get("poster_url"), "shared": sorted(shared)}))
    return [item for _, item in sorted(related, key=lambda candidate: (-candidate[0], str(candidate[1].get("title") or "").casefold(), candidate[1]["video_id"]))[:limit]]


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def _sanitize_png(data: bytes) -> tuple[bytes, int, int]:
    """Accept one bounded, non-interlaced RGB/RGBA PNG and write canonical chunks."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) < 45 or len(data) > _MAX_IMAGE_BYTES:
        raise HTTPException(422, "Poster must be a bounded PNG image")
    offset, ihdr, compressed, saw_idat, closed = 8, None, bytearray(), False, False
    while offset + 12 <= len(data):
        size = struct.unpack(">I", data[offset:offset + 4])[0]
        end = offset + 12 + size
        if end > len(data):
            raise HTTPException(422, "Invalid PNG poster")
        kind, payload = data[offset + 4:offset + 8], data[offset + 8:offset + 8 + size]
        supplied_crc = struct.unpack(">I", data[offset + 8 + size:end])[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != supplied_crc:
            raise HTTPException(422, "PNG checksum is invalid")
        offset = end
        if ihdr is None:
            if kind != b"IHDR" or len(payload) != 13:
                raise HTTPException(422, "Invalid PNG poster")
            ihdr = payload
            continue
        if kind == b"IDAT":
            if closed:
                raise HTTPException(422, "Invalid PNG chunk order")
            saw_idat = True
            compressed.extend(payload)
            if len(compressed) > _MAX_IMAGE_BYTES:
                raise HTTPException(422, "PNG image data is too large")
        elif kind == b"IEND":
            if payload or not saw_idat or offset != len(data):
                raise HTTPException(422, "Invalid PNG poster")
            closed = True
            break
        else:
            # Reject all ancillary chunks (including animation/text) and all
            # unknown critical chunks, rather than preserving hidden payloads.
            raise HTTPException(422, "PNG contains unsupported chunks")
    if ihdr is None or not closed:
        raise HTTPException(422, "Invalid PNG poster")
    width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(">IIBBBBB", ihdr)
    if (bit_depth, color_type, compression, filtering, interlace) not in {(8, 2, 0, 0, 0), (8, 6, 0, 0, 0)}:
        raise HTTPException(422, "PNG must be a non-interlaced 8-bit RGB or RGBA image")
    _validate_dimensions(width, height)
    channels = 3 if color_type == 2 else 4
    expected = height * (1 + width * channels)
    try:
        decompressor = zlib.decompressobj()
        pixels = decompressor.decompress(bytes(compressed), expected + 1)
        if len(pixels) > expected or decompressor.unconsumed_tail:
            raise HTTPException(422, "PNG image data is invalid")
        pixels += decompressor.flush(expected + 1 - len(pixels))
    except zlib.error as exc:
        raise HTTPException(422, "PNG image data is invalid") from exc
    if not decompressor.eof or decompressor.unused_data or len(pixels) != expected:
        raise HTTPException(422, "PNG image data is invalid")
    row_size = 1 + width * channels
    if any(pixels[index] > 4 for index in range(0, len(pixels), row_size)):
        raise HTTPException(422, "PNG filter data is invalid")
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", zlib.compress(pixels, 9)) + _png_chunk(b"IEND", b""), width, height


def _image_dimensions(data: bytes) -> tuple[str, int, int]:
    """Compatibility helper; custom posters intentionally support sanitized PNG only."""
    _, width, height = _sanitize_png(data)
    return "png", width, height


def _validate_dimensions(width: int, height: int) -> None:
    if not 1 <= width <= _MAX_IMAGE_SIDE or not 1 <= height <= _MAX_IMAGE_SIDE or width * height > _MAX_IMAGE_PIXELS:
        raise HTTPException(422, "Poster dimensions are not allowed")


def _artwork_directory(config) -> Path:
    directory = Path(config.data_dir) / "artwork"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    details = os.lstat(directory)
    if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise HTTPException(409, "Artwork storage is not a safe directory")
    return directory


def _artwork_name(video_id: str) -> str:
    return hashlib.sha256(video_id.encode("utf-8")).hexdigest() + ".png"


def _check_artwork_budget(directory: Path, replacement: str, incoming_size: int) -> None:
    used = 0
    for entry in directory.iterdir():
        details = os.lstat(entry)
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise HTTPException(409, "Artwork storage contains an unsafe file")
        if entry.name != replacement:
            used += details.st_size
    if used + incoming_size > _MAX_ARTWORK_BYTES:
        raise HTTPException(413, "Artwork storage limit reached")


def _safe_artwork_file(directory: Path, name: str) -> tuple[Path, os.stat_result]:
    if not re.fullmatch(r"[0-9a-f]{64}\.png", name):
        raise HTTPException(404, "No custom poster")
    path = directory / name
    try:
        details = os.lstat(path)
    except OSError as exc:
        raise HTTPException(404, "No custom poster") from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or details.st_size > _MAX_IMAGE_BYTES:
        raise HTTPException(404, "No custom poster")
    return path, details


def _read_artwork(directory: Path, name: str) -> bytes:
    path, expected_details = _safe_artwork_file(directory, name)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise HTTPException(404, "No custom poster") from exc
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or details.st_size > _MAX_IMAGE_BYTES
            or (details.st_dev, details.st_ino) != (expected_details.st_dev, expected_details.st_ino)
        ):
            raise HTTPException(404, "No custom poster")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            content = handle.read(_MAX_IMAGE_BYTES + 1)
    finally:
        os.close(descriptor)
    sanitized, _, _ = _sanitize_png(content)
    # Files are revalidated at delivery and must already be canonical output.
    if content != sanitized:
        raise HTTPException(404, "No custom poster")
    return content


async def save_custom_poster(db, config, video_id: str, upload: UploadFile) -> dict:
    with db.connect() as conn:
        _video_exists(conn, video_id)
    original = await upload.read(_MAX_IMAGE_BYTES + 1)
    data, width, height = _sanitize_png(original)
    directory, filename = _artwork_directory(config), _artwork_name(video_id)
    _check_artwork_budget(directory, filename, len(data))
    descriptor, temporary = tempfile.mkstemp(prefix=".poster-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, directory / filename)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    url = f"/api/metadata/videos/{video_id}/poster"
    with db.transaction() as conn:
        row = conn.execute("SELECT body FROM video_overrides WHERE video_id=?", (video_id,)).fetchone()
        override = _json(row["body"], {}) if row else {}
        override.update({"poster_url": url, "poster_artwork": filename})
        conn.execute("INSERT INTO video_overrides(video_id,body,updated_at) VALUES(?,?,?) ON CONFLICT(video_id) DO UPDATE SET body=excluded.body,updated_at=excluded.updated_at", (video_id, json.dumps(override, separators=(",", ":")), int(time.time())))
    return {"poster_url": url, "width": width, "height": height}


def custom_poster_bytes(db, config, video_id: str) -> bytes:
    with db.connect() as conn:
        _video_exists(conn, video_id)
        row = conn.execute("SELECT body FROM video_overrides WHERE video_id=?", (video_id,)).fetchone()
    override = _json(row["body"], {}) if row else {}
    expected = _artwork_name(video_id)
    if override.get("poster_url") != f"/api/metadata/videos/{video_id}/poster" or override.get("poster_artwork") != expected:
        raise HTTPException(404, "No custom poster")
    return _read_artwork(_artwork_directory(config), expected)


def custom_poster_path(db, config, video_id: str) -> tuple[Path, str]:
    """Legacy helper retained for callers; authenticated routes must use bytes."""
    custom_poster_bytes(db, config, video_id)
    return _artwork_directory(config) / _artwork_name(video_id), "image/png"
