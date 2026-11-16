"""Authenticated per-user library services with conservative local-only behavior."""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from typing import Any

from fastapi import HTTPException

from .db import decode_video
from .jobs import enqueue_video

DEFAULT_PREFERENCES = {"watched_threshold": 90, "autoplay_next": False, "metadata_language": "en"}
FILTER_FIELDS = {
    "title", "year", "genre", "cast", "director", "type", "state", "source", "watched", "collection", "search",
    "recentlyadded", "lastwatched", "duration", "cached", "continuewatching", "failed", "unwatched", "movies", "series",
}
SORT_FIELDS = {"title", "year", "genre", "cast", "director", "type", "state", "source", "watched", "recentlyadded", "lastwatched", "duration"}
_MAX_QUERY = 160
_MAX_OVERRIDE_TEXT = 500
_MAX_OVERRIDE_ARRAY = 32
_FILENAME_EPISODE = re.compile(r"^\s*(?P<series>.+?)[. _-]+(?:S(?P<season>\d{1,2})E(?P<episode>\d{1,3})|(?P<season_x>\d{1,2})x(?P<episode_x>\d{1,3}))(?:[. _-]|$)", re.I)


def _json(value: Any, default: Any) -> Any:
    try:
        parsed = json.loads(value or "")
        return parsed if isinstance(parsed, type(default)) else default
    except (TypeError, ValueError):
        return default


def _override(conn: sqlite3.Connection, video_id: str) -> dict:
    row = conn.execute("SELECT body FROM video_overrides WHERE video_id=?", (video_id,)).fetchone()
    return _json(row["body"], {}) if row else {}


def _metadata(conn: sqlite3.Connection, video_id: str) -> dict:
    row = conn.execute("SELECT m.body FROM video_metadata vm JOIN metadata_records m ON m.id=vm.metadata_id WHERE vm.video_id=?", (video_id,)).fetchone()
    return _json(row["body"], {}) if row else {}


def _filename_episode(title: Any) -> dict:
    if not isinstance(title, str):
        return {}
    match = _FILENAME_EPISODE.match(title)
    if not match:
        return {}
    season = int(match.group("season") or match.group("season_x"))
    episode = int(match.group("episode") or match.group("episode_x"))
    series = re.sub(r"[._-]+", " ", match.group("series")).strip()
    return {"type": "series", "series": series, "season": season, "episode": episode} if series else {}


def _finite_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(float(value))


def _strict_bool(value: Any) -> bool | None:
    if type(value) is bool:
        return value
    if isinstance(value, str) and value.strip().casefold() in {"true", "false"}:
        return value.strip().casefold() == "true"
    return None


def validate_filters(filters: dict | None) -> dict:
    if filters is None:
        return {}
    if not isinstance(filters, dict) or set(filters) - FILTER_FIELDS:
        raise HTTPException(422, "Unsupported library filter")
    result = {}
    boolean_filters = {"watched", "recentlyadded", "lastwatched", "cached", "continuewatching", "failed", "unwatched", "movies", "series"}
    for key, value in filters.items():
        values = value if isinstance(value, list) else [value]
        if not values or len(values) > 32:
            raise HTTPException(422, "Invalid library filter")
        if key in boolean_filters:
            normalized = [_strict_bool(item) for item in values]
            if any(item is None for item in normalized):
                raise HTTPException(422, "Boolean library filters must be true or false")
            result[key] = normalized
        elif any(type(item) not in (str, int) or isinstance(item, str) and len(item) > _MAX_QUERY for item in values):
            raise HTTPException(422, "Invalid library filter")
        else:
            result[key] = values
    return result


def validate_sort(sort: str) -> str:
    if sort not in SORT_FIELDS:
        raise HTTPException(422, "Unsupported library sort")
    return sort


def preferences(db, username: str) -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT body FROM user_preferences WHERE username=?", (username,)).fetchone()
    return {**DEFAULT_PREFERENCES, **(_json(row["body"], {}) if row else {})}


def update_preferences(db, username: str, patch: dict) -> dict:
    if not isinstance(patch, dict) or set(patch) - set(DEFAULT_PREFERENCES):
        raise HTTPException(422, "Unsupported preference")
    if "watched_threshold" in patch and (type(patch["watched_threshold"]) is not int or not 50 <= patch["watched_threshold"] <= 100):
        raise HTTPException(422, "watched_threshold must be 50 through 100")
    if "autoplay_next" in patch and type(patch["autoplay_next"]) is not bool:
        raise HTTPException(422, "autoplay_next must be boolean")
    if "metadata_language" in patch and (not isinstance(patch["metadata_language"], str) or not re.fullmatch(r"[a-z]{2,8}", patch["metadata_language"])):
        raise HTTPException(422, "metadata_language is invalid")
    with db.transaction() as conn:
        row = conn.execute("SELECT body FROM user_preferences WHERE username=?", (username,)).fetchone()
        value = {**DEFAULT_PREFERENCES, **(_json(row["body"], {}) if row else {}), **patch}
        conn.execute("INSERT INTO user_preferences(username,body) VALUES(?,?) ON CONFLICT(username) DO UPDATE SET body=excluded.body", (username, json.dumps(value, separators=(",", ":"))))
    return value


def _valid_text(value: Any, *, required: bool = False) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= _MAX_OVERRIDE_TEXT if required else isinstance(value, str) and len(value) <= _MAX_OVERRIDE_TEXT


def update_override(db, video_id: str, patch: dict) -> dict:
    allowed = {"title", "year", "genre", "cast", "director", "type", "series", "season", "episode"}
    if not isinstance(patch, dict) or not patch or set(patch) - allowed:
        raise HTTPException(422, "Unsupported override")
    for key, value in patch.items():
        if value is None:
            continue
        if key == "title" and not _valid_text(value, required=True):
            raise HTTPException(422, "title must be nonblank")
        if key in {"director", "series"} and not _valid_text(value):
            raise HTTPException(422, "Invalid text override")
        if key == "type" and value not in {"movie", "series"}:
            raise HTTPException(422, "type must be movie or series")
        if key == "year" and (type(value) is not int or not 1888 <= value <= 3000):
            raise HTTPException(422, "Invalid year override")
        if key == "season" and (type(value) is not int or not 0 <= value <= 999):
            raise HTTPException(422, "Invalid season override")
        if key == "episode" and (type(value) is not int or not 1 <= value <= 9999):
            raise HTTPException(422, "Invalid episode override")
        if key in {"genre", "cast"} and (not isinstance(value, list) or len(value) > _MAX_OVERRIDE_ARRAY or any(not _valid_text(item, required=True) or len(item) > 120 for item in value)):
            raise HTTPException(422, "Invalid list override")
    with db.transaction() as conn:
        if not conn.execute("SELECT 1 FROM videos WHERE id=?", (video_id,)).fetchone():
            raise HTTPException(404, "video not found")
        current = _override(conn, video_id)
        # Null explicitly resets a local override, exposing normalized provider/filename data.
        for key, value in patch.items():
            if value is None:
                current.pop(key, None)
            else:
                current[key] = value.strip() if isinstance(value, str) else value
        conn.execute("INSERT INTO video_overrides(video_id,body,updated_at) VALUES(?,?,?) ON CONFLICT(video_id) DO UPDATE SET body=excluded.body,updated_at=excluded.updated_at", (video_id, json.dumps(current, separators=(",", ":")), int(time.time())))
    return current


def enrich_video(db, video_dict: dict, username: str) -> dict:
    with db.connect() as conn:
        override, metadata = _override(conn, video_dict["id"]), _metadata(conn, video_dict["id"])
        state = conn.execute("SELECT watched,resume_position,origin,completed_at FROM video_user_state WHERE username=? AND video_id=?", (username, video_dict["id"])).fetchone()
        history = conn.execute('SELECT resume_position FROM watch_history WHERE username=? AND video_id=?', (username, video_dict['id'])).fetchone()
    base = {**video_dict, 'watch_position': float(history[0]) if history else 0.0}
    return _compose_video(base, override, metadata, state)


def _compose_video(video_dict: dict, override: dict, metadata: dict, state) -> dict:
    result = dict(video_dict)
    inferred = _filename_episode(video_dict.get("title"))
    fields = {**inferred, **metadata, **override}
    for key in ("title", "year", "genre", "cast", "director", "type", "series", "season", "episode", "description", "poster_url", "rating", "trailer_links", "related_ids", "provider", "media_id", "language"):
        if key in fields:
            result[key] = fields[key]
    result.setdefault("type", "series" if inferred else "movie")
    result["metadata"] = metadata
    result["overrides"] = override
    result["metadata_provenance"] = {key: ("override" if key in override else "metadata" if key in metadata else "filename" if key in inferred else "video") for key in result if key in {"title", "year", "genre", "cast", "director", "type", "series", "season", "episode", "description", "poster_url", "rating", "trailer_links", "related_ids", "provider", "media_id", "language"}}
    result["watched"] = bool(state["watched"]) if state else False
    result["resume_position"] = float(state["resume_position"]) if state else float(result.get('watch_position') or 0)
    result['watch_position'] = result['resume_position']
    result["watch_origin"] = state["origin"] if state else None
    result["completed_at"] = state["completed_at"] if state else None
    return result


def _state_row(conn: sqlite3.Connection, username: str, video_id: str):
    return conn.execute("SELECT * FROM video_user_state WHERE username=? AND video_id=?", (username, video_id)).fetchone()


def set_watched(db, username: str, video_id: str, watched: bool) -> dict:
    if type(watched) is not bool:
        raise HTTPException(422, "watched must be boolean")
    with db.transaction() as conn:
        if not conn.execute("SELECT 1 FROM videos WHERE id=?", (video_id,)).fetchone():
            raise HTTPException(404, "video not found")
        conn.execute("INSERT INTO video_user_state(username,video_id,watched,origin,completed_at,resume_position) VALUES(?,?,?,?,?,?) ON CONFLICT(username,video_id) DO UPDATE SET watched=excluded.watched,origin='manual',completed_at=excluded.completed_at,resume_position=excluded.resume_position", (username, video_id, int(watched), "manual", int(time.time()) if watched else None, 0.0))
    return {"watched": watched, "origin": "manual"}


def observe_playback(db, username: str, video_id: str, position: float, duration: float, now: float | None = None) -> dict:
    if not all(_finite_number(value) for value in (position, duration)) or float(position) < 0 or float(duration) <= 0 or float(position) > float(duration) * 1.05:
        raise HTTPException(422, "Invalid playback position")
    now = time.time() if now is None else now
    if not _finite_number(now):
        raise HTTPException(422, "Invalid observation time")
    position, duration, now = float(position), float(duration), float(now)
    with db.transaction() as conn:
        stored_video = conn.execute("SELECT duration FROM videos WHERE id=?", (video_id,)).fetchone()
        if not stored_video:
            raise HTTPException(404, "video not found")
        authoritative_duration = stored_video['duration']
        known_duration = _finite_number(authoritative_duration) and authoritative_duration > 0
        if known_duration:
            duration = float(authoritative_duration)
            position = min(position, duration)
        old = _state_row(conn, username, video_id)
        origin = old["origin"] if old else "automatic"
        observed = float(old["observed_seconds"]) if old else 0.0
        previous_position = float(old["last_position"]) if old else position
        previous_at = float(old["last_observed_at"]) if old else now
        elapsed, movement = now - previous_at, position - previous_position
        furthest = float(old['furthest_position']) if old else position
        # Count only newly covered forward progress. Replaying a short section,
        # paused heartbeats and large seeks cannot inflate completion evidence.
        if elapsed > 0 and movement > 0 and movement <= elapsed * 2.25 + 2.0:
            observed += max(0.0, position - max(previous_position, furthest))
        furthest = max(furthest, position)
        threshold = preferences(db, username)["watched_threshold"] / 100.0
        watched = bool(old["watched"]) if old else False
        automatic = known_duration and origin != "manual" and position / duration >= threshold and observed >= duration * threshold
        if automatic:
            watched = True
        resume = 0.0 if watched else min(position, duration)
        conn.execute("INSERT INTO video_user_state(username,video_id,watched,origin,completed_at,resume_position,observed_seconds,last_position,last_observed_at,furthest_position) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(username,video_id) DO UPDATE SET watched=excluded.watched,origin=excluded.origin,completed_at=excluded.completed_at,resume_position=excluded.resume_position,observed_seconds=excluded.observed_seconds,last_position=excluded.last_position,last_observed_at=excluded.last_observed_at,furthest_position=excluded.furthest_position", (username, video_id, int(watched), origin, int(now) if automatic else (old["completed_at"] if old else None), resume, observed, position, now, furthest))
    return {"watched": watched, "resume_position": resume, "observed_seconds": observed, "origin": origin}


def _value(item: dict, field: str) -> Any:
    if field == "source": return item.get("source_id") or item.get("source_name") or ""
    if field == "recentlyadded": return item.get("created_at") or item.get("updated_at") or ""
    if field == "lastwatched": return item.get("completed_at") or 0
    return item.get(field, "")


def _matches(item: dict, filters: dict, collections: set[str]) -> bool:
    for field, values in filters.items():
        if field == "collection":
            if not any(str(v) in collections for v in values): return False
            continue
        actual = _value(item, field)
        if field == "watched":
            if not any(value is bool(actual) for value in values): return False
        elif field == "unwatched":
            if not any(value is (not bool(item.get("watched"))) for value in values): return False
        elif field == "cached":
            if not any(value is (item.get("state") in {"cached", "playable"} or bool(item.get("local_path"))) for value in values): return False
        elif field == "failed":
            if not any(value is (item.get("state") == "failed" or bool(item.get("error"))) for value in values): return False
        elif field == "continuewatching":
            eligible = not item.get("watched") and float(item.get("resume_position") or 0) > 0
            if not any(value is eligible for value in values): return False
        elif field == "series":
            if not any(value is (item.get("type") == "series") for value in values): return False
        elif field == "movies":
            if not any(value is (item.get("type") == "movie") for value in values): return False
        elif field == "lastwatched":
            if not any(value is bool(item.get("completed_at")) for value in values): return False
        elif field == "recentlyadded":
            if not any(value is bool(item.get("created_at") or item.get("updated_at")) for value in values): return False
        elif field == "duration":
            if not any(str(value) == str(actual) for value in values): return False
        elif isinstance(actual, list):
            if not any(str(v).casefold() in [str(x).casefold() for x in actual] for v in values): return False
        elif field == "title":
            if not any(str(v).casefold() in str(actual).casefold() for v in values): return False
        elif not any(str(v).casefold() == str(actual).casefold() for v in values): return False
    return True


def list_library(db, username: str, filters: dict | None = None, sort: str = "title", query: str = "", limit: int | None = 500) -> list[dict]:
    filters, sort = validate_filters(filters), validate_sort(sort)
    saved_query = filters.pop('search', [])
    if saved_query and (len(saved_query) != 1 or not isinstance(saved_query[0], str)):
        raise HTTPException(422, 'Saved search must contain one text query')
    if saved_query and isinstance(query, str) and not query.strip():
        query = saved_query[0]
    if not isinstance(query, str) or len(query) > _MAX_QUERY or (limit is not None and (type(limit) is not int or not 1 <= limit <= 500)):
        raise HTTPException(422, "Invalid library query")
    with db.connect() as conn:
        # Do not impose a hidden database cap: the public limit is the only truncation.
        rows = conn.execute("SELECT v.*,s.name AS source_name FROM videos v LEFT JOIN sources s ON s.id=v.source_id WHERE v.removed_from_source=0 ORDER BY v.id").fetchall()
        collection_rows = conn.execute("SELECT ci.video_id,ci.collection_id FROM collection_items ci JOIN collections c ON c.id=ci.collection_id WHERE c.username=?", (username,)).fetchall()
        overrides = {row['video_id']: _json(row['body'], {}) for row in conn.execute('SELECT video_id,body FROM video_overrides')}
        metadata = {row['video_id']: _json(row['body'], {}) for row in conn.execute('SELECT vm.video_id,m.body FROM video_metadata vm JOIN metadata_records m ON m.id=vm.metadata_id')}
        states = {row['video_id']: row for row in conn.execute('SELECT * FROM video_user_state WHERE username=?', (username,))}
        history = {row['video_id']: row['resume_position'] for row in conn.execute('SELECT video_id,resume_position FROM watch_history WHERE username=?', (username,))}
        cues = []
        if query.strip():
            escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            cues = conn.execute("SELECT video_id,start,end,text FROM caption_cues WHERE text LIKE ? ESCAPE '\\' ORDER BY video_id,start LIMIT 100", (f"%{escaped}%",)).fetchall()
    cue_map: dict[str, list[dict]] = {}
    for cue in cues: cue_map.setdefault(cue["video_id"], []).append(dict(cue))
    collection_map: dict[str, set[str]] = {}
    for row in collection_rows: collection_map.setdefault(row["video_id"], set()).add(row["collection_id"])
    output = []
    for row in rows:
        base = decode_video(row)
        base['watch_position'] = history.get(row['id'], 0.0)
        item = _compose_video(base, overrides.get(row['id'], {}), metadata.get(row['id'], {}), states.get(row['id']))
        item["source_name"] = row["source_name"]
        searchable = ' '.join(str(item.get(key) or '') for key in ('title', 'year', 'genre', 'cast', 'director', 'series', 'season', 'episode', 'description'))
        if item.get('season') is not None and item.get('episode') is not None:
            searchable += f" S{item['season']:02d}E{item['episode']:02d}"
        if query.strip() and query.strip().casefold() not in searchable.casefold() and item["id"] not in cue_map: continue
        if _matches(item, filters, collection_map.get(item["id"], set())):
            if item["id"] in cue_map: item["caption_matches"] = cue_map[item["id"]]
            output.append(item)
    def order(value: dict):
        raw = _value(value, sort)
        if isinstance(raw, (int, float)): raw = f"{float(raw):020.6f}"
        return (raw is None, str(raw).casefold(), value["id"])
    return sorted(output, key=order, reverse=sort in {"recentlyadded", "lastwatched", "duration"}) if limit is None else sorted(output, key=order, reverse=sort in {"recentlyadded", "lastwatched", "duration"})[:limit]


def _parse_vtt(vtt_text: str) -> list[tuple[float, float, str]]:
    if not isinstance(vtt_text, str) or len(vtt_text.encode("utf-8")) > 512 * 1024:
        raise HTTPException(422, "Caption file is too large")
    cues, lines, index = [], vtt_text.replace("\r", "").split("\n"), 0
    timestamp = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2}(?:\.\d+)?)\s+-->\s+(?:(\d+):)?(\d{1,2}):(\d{2}(?:\.\d+)?)")
    while index < len(lines) and len(cues) < 2000:
        match = timestamp.search(lines[index])
        if not match: index += 1; continue
        start = int(match.group(1) or 0) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
        end = int(match.group(4) or 0) * 3600 + int(match.group(5)) * 60 + float(match.group(6))
        index += 1; text = []
        while index < len(lines) and lines[index].strip():
            text.append(re.sub(r"<[^>]+>", "", lines[index]).strip()); index += 1
        joined = " ".join(part for part in text if part)[:2000]
        if joined and end >= start and math.isfinite(start) and math.isfinite(end): cues.append((start, end, joined))
        index += 1
    return cues


def index_caption(db, video_id: str, track_id: str, vtt_text: str) -> int:
    if not isinstance(track_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", track_id):
        raise HTTPException(422, "Invalid caption track")
    cues = _parse_vtt(vtt_text)
    with db.transaction() as conn:
        if not conn.execute("SELECT 1 FROM videos WHERE id=?", (video_id,)).fetchone(): raise HTTPException(404, "video not found")
        conn.execute("DELETE FROM caption_cues WHERE video_id=? AND track_id=?", (video_id, track_id))
        conn.executemany("INSERT INTO caption_cues(video_id,track_id,cue_index,start,end,text,updated_at) VALUES(?,?,?,?,?,?,?)", [(video_id, track_id, i, start, end, text, int(time.time())) for i, (start, end, text) in enumerate(cues)])
    return len(cues)


def remove_caption_index(db, video_id: str, track_id: str | None = None) -> int:
    if track_id is not None and (not isinstance(track_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", track_id)):
        raise HTTPException(422, "Invalid caption track")
    with db.transaction() as conn:
        if not conn.execute("SELECT 1 FROM videos WHERE id=?", (video_id,)).fetchone(): raise HTTPException(404, "video not found")
        cursor = conn.execute("DELETE FROM caption_cues WHERE video_id=?" + (" AND track_id=?" if track_id else ""), (video_id, track_id) if track_id else (video_id,))
    return cursor.rowcount


def create_collection(db, username: str, name: str) -> dict:
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120: raise HTTPException(422, "Invalid collection name")
    result = {"id": uuid.uuid4().hex, "name": name.strip(), "created_at": int(time.time())}
    with db.transaction() as conn: conn.execute("INSERT INTO collections(id,username,name,created_at) VALUES(?,?,?,?)", (result["id"], username, result["name"], result["created_at"]))
    return result


def list_collections(db, username: str) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT c.id,c.name,c.created_at,COUNT(ci.video_id) AS item_count FROM collections c LEFT JOIN collection_items ci ON ci.collection_id=c.id WHERE c.username=? GROUP BY c.id ORDER BY c.name COLLATE NOCASE,c.id", (username,)).fetchall()
    return [dict(row) for row in rows]


def update_collection(db, username: str, collection_id: str, name: str) -> dict:
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120: raise HTTPException(422, "Invalid collection name")
    with db.transaction() as conn:
        if not conn.execute("UPDATE collections SET name=? WHERE id=? AND username=?", (name.strip(), collection_id, username)).rowcount: raise HTTPException(404, "collection not found")
    return {"id": collection_id, "name": name.strip()}


def delete_collection(db, username: str, collection_id: str) -> None:
    with db.transaction() as conn:
        if not conn.execute("DELETE FROM collections WHERE id=? AND username=?", (collection_id, username)).rowcount: raise HTTPException(404, "collection not found")



def list_collection_items(db, username: str, collection_id: str) -> list[dict]:
    with db.connect() as conn:
        owner = conn.execute("SELECT 1 FROM collections WHERE id=? AND username=?", (collection_id, username)).fetchone()
        if not owner: raise HTTPException(404, "collection not found")
        rows = conn.execute("SELECT video_id,position FROM collection_items WHERE collection_id=? ORDER BY position,video_id", (collection_id,)).fetchall()
    return [dict(row) for row in rows]


def add_collection_item(db, username: str, collection_id: str, video_id: str, position: int = 0) -> None:
    if type(position) is not int or position < 0: raise HTTPException(422, "Invalid item position")

    with db.transaction() as conn:
        if not conn.execute("SELECT 1 FROM collections WHERE id=? AND username=?", (collection_id, username)).fetchone(): raise HTTPException(404, "collection not found")
        if not conn.execute("SELECT 1 FROM videos WHERE id=?", (video_id,)).fetchone(): raise HTTPException(404, "video not found")
        conn.execute("INSERT INTO collection_items(collection_id,video_id,position) VALUES(?,?,?) ON CONFLICT(collection_id,video_id) DO UPDATE SET position=excluded.position", (collection_id, video_id, position))


def remove_collection_item(db, username: str, collection_id: str, video_id: str) -> None:
    with db.transaction() as conn:
        if not conn.execute("SELECT 1 FROM collections WHERE id=? AND username=?", (collection_id, username)).fetchone(): raise HTTPException(404, "collection not found")
        if not conn.execute("DELETE FROM collection_items WHERE collection_id=? AND video_id=?", (collection_id, video_id)).rowcount: raise HTTPException(404, "collection item not found")


def reorder_collection_items(db, username: str, collection_id: str, video_ids: list[str]) -> None:
    if not isinstance(video_ids, list) or len(video_ids) != len(set(video_ids)) or any(not isinstance(video_id, str) for video_id in video_ids): raise HTTPException(422, "Invalid collection order")
    with db.transaction() as conn:
        rows = conn.execute("SELECT ci.video_id FROM collection_items ci JOIN collections c ON c.id=ci.collection_id WHERE c.id=? AND c.username=?", (collection_id, username)).fetchall()
        actual = {row["video_id"] for row in rows}
        if not rows: raise HTTPException(404, "collection not found")
        if set(video_ids) != actual: raise HTTPException(422, "Collection order must contain every item")
        conn.executemany("UPDATE collection_items SET position=? WHERE collection_id=? AND video_id=?", [(index, collection_id, video_id) for index, video_id in enumerate(video_ids)])


def create_saved_view(db, username: str, name: str, filters: dict, sort: str) -> dict:
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120: raise HTTPException(422, "Invalid saved view name")
    filters, sort = validate_filters(filters), validate_sort(sort)
    result = {"id": uuid.uuid4().hex, "name": name.strip(), "filters": filters, "sort": sort, "created_at": int(time.time())}
    with db.transaction() as conn: conn.execute("INSERT INTO saved_views(id,username,name,filters,sort,created_at) VALUES(?,?,?,?,?,?)", (result["id"], username, result["name"], json.dumps(filters), sort, result["created_at"]))
    return result


def list_saved_views(db, username: str) -> list[dict]:
    with db.connect() as conn: rows = conn.execute("SELECT * FROM saved_views WHERE username=? ORDER BY name COLLATE NOCASE,id", (username,)).fetchall()
    return [{**dict(row), "filters": _json(row["filters"], {})} for row in rows]



def update_saved_view(db, username: str, view_id: str, name: str, filters: dict, sort: str) -> dict:
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120: raise HTTPException(422, "Invalid saved view name")
    filters, sort = validate_filters(filters), validate_sort(sort)
    with db.transaction() as conn:
        if not conn.execute("UPDATE saved_views SET name=?,filters=?,sort=? WHERE id=? AND username=?", (name.strip(), json.dumps(filters, separators=(",", ":")), sort, view_id, username)).rowcount:
            raise HTTPException(404, "saved view not found")
    return {"id": view_id, "name": name.strip(), "filters": filters, "sort": sort}


def delete_saved_view(db, username: str, view_id: str) -> None:
    with db.transaction() as conn:
        if not conn.execute("DELETE FROM saved_views WHERE id=? AND username=?", (view_id, username)).rowcount: raise HTTPException(404, "saved view not found")


def record_search(db, username: str, query: str) -> None:
    if not isinstance(query, str) or not (query := query.strip()) or len(query) > _MAX_QUERY: return
    with db.transaction() as conn:
        conn.execute("INSERT INTO recent_searches(id,username,query,created_at) VALUES(?,?,?,?)", (uuid.uuid4().hex, username, query, int(time.time())))
        conn.execute("DELETE FROM recent_searches WHERE id IN (SELECT id FROM recent_searches WHERE username=? ORDER BY created_at DESC,id DESC LIMIT -1 OFFSET 20)", (username,))


def list_recent_searches(db, username: str) -> list[dict]:
    with db.connect() as conn: rows = conn.execute("SELECT id,query,created_at FROM recent_searches WHERE username=? ORDER BY created_at DESC,id DESC LIMIT 20", (username,)).fetchall()
    return [dict(row) for row in rows]


def delete_recent_search(db, username: str, search_id: str) -> None:
    with db.transaction() as conn:
        if not conn.execute("DELETE FROM recent_searches WHERE id=? AND username=?", (search_id, username)).rowcount: raise HTTPException(404, "recent search not found")


def clear_recent_searches(db, username: str) -> None:
    with db.transaction() as conn: conn.execute("DELETE FROM recent_searches WHERE username=?", (username,))


def next_episode(db, username: str, video_id: str) -> dict | None:
    current = next((item for item in list_library(db, username, limit=None) if item["id"] == video_id), None)
    if not current or not current.get("series") or current.get("season") is None or current.get("episode") is None: return None
    candidates = [item for item in list_library(db, username, limit=None) if item.get("series") == current["series"] and item.get('season') is not None and item.get('episode') is not None and (item["season"], item["episode"]) > (current["season"], current["episode"])]
    following = min(candidates, key=lambda item: (item["season"], item["episode"], item["id"])) if candidates else None
    # Never skip an unavailable next catalog entry to start a later episode and
    # never admit a job here. A UI can only advance to existing playable bytes.
    return following if following and following['state'] in {'playable', 'cached'} and following.get('playback', {}).get('url') else None


def series_progress(db, username: str, series: str) -> dict:
    if not isinstance(series, str) or not series.strip(): raise HTTPException(422, "Invalid series")
    items = [item for item in list_library(db, username, filters={"series": True}, limit=None) if item.get("series", "").casefold() == series.strip().casefold()]
    seasons: dict[int, dict] = {}
    for item in items:
        season = item.get("season")
        if type(season) is not int: continue
        summary = seasons.setdefault(season, {"season": season, "total": 0, "watched": 0, "unwatched": 0, "episodes": []})
        summary["total"] += 1; summary["watched"] += int(item["watched"]); summary["unwatched"] += int(not item["watched"]); summary["episodes"].append(item)
    return {"series": series.strip(), "total": len(items), "watched": sum(int(item["watched"]) for item in items), "unwatched": sum(int(not item["watched"]) for item in items), "seasons": [seasons[key] for key in sorted(seasons)]}


def _download_source_fingerprint(row) -> str:
    # Retain only a digest in the server-side plan; never expose signed URLs.
    fields = ('source_id', 'source_kind', 'remote_id', 'source_url', 'size_bytes')
    return hashlib.sha256(json.dumps([row[key] for key in fields], separators=(',', ':')).encode('utf-8')).hexdigest()


def create_download_plan(db, username: str, video_ids: list[str], ttl_seconds: int = 600) -> dict:
    if not isinstance(video_ids, list) or not 1 <= len(video_ids) <= 500 or any(not isinstance(item, str) or not item for item in video_ids): raise HTTPException(422, "Invalid season selection")
    ids = list(dict.fromkeys(video_ids))
    with db.connect() as conn:
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(f"SELECT v.*,EXISTS(SELECT 1 FROM jobs j WHERE j.video_id=v.id AND j.state IN ('queued','downloading','preparing')) AS has_live_job FROM videos v WHERE v.id IN ({placeholders})", ids).fetchall()
    if len(rows) != len(ids): raise HTTPException(404, "A selected video was not found")
    by_id = {row['id']: row for row in rows}
    eligible, excluded, items, fingerprints, known, unknown = [], [], [], {}, 0, 0
    for video_id in ids:
        row = by_id[video_id]
        reason = None
        if row['state'] == 'removing': reason = 'cache_removal'
        elif row['removed_from_source'] and not row['local_path']: reason = 'source_unavailable'
        elif row['has_live_job']: reason = 'active_job'
        elif row['state'] in {'playable', 'cached'} and row['playback_url']: reason = 'already_playable'
        size = row['size_bytes'] if type(row['size_bytes']) is int and row['size_bytes'] >= 0 else None
        items.append({'id': video_id, 'title': row['title'], 'source_kind': row['source_kind'], 'size_bytes': size, 'excluded_reason': reason})
        if reason:
            excluded.append(video_id)
            continue
        eligible.append(video_id)
        fingerprints[video_id] = _download_source_fingerprint(row)
        if size is not None: known += size
        else: unknown += 1
    public = {'video_ids': eligible, 'excluded': excluded, 'items': items, 'known_bytes': known, 'unknown_count': unknown}
    result = {'id': uuid.uuid4().hex, **public, 'expires_at': int(time.time()) + min(max(ttl_seconds, 30), 3600)}
    body = {**public, 'source_fingerprints': fingerprints}
    with db.transaction() as conn: conn.execute('INSERT INTO download_plans(id,username,body,expires_at) VALUES(?,?,?,?)', (result['id'], username, json.dumps(body), result['expires_at']))
    return result


def confirm_download_plan(db, username: str, plan_id: str) -> dict:
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM download_plans WHERE id=? AND username=?", (plan_id, username)).fetchone()
        if not row or row["consumed"]: raise HTTPException(404, "download plan not found")
        if row["expires_at"] < int(time.time()): raise HTTPException(410, "download plan expired")
        body = _json(row["body"], {})
        ids = body.get("video_ids", [])
        if not ids: raise HTTPException(409, "No selected videos remain eligible")
        placeholders = ",".join("?" for _ in ids)
        current = conn.execute(f"SELECT * FROM videos WHERE id IN ({placeholders})", ids).fetchall()
        fingerprints = body.get('source_fingerprints', {})
        if (len(current) != len(ids) or not isinstance(fingerprints, dict) or
                any(video['state'] == 'removing' or (video['removed_from_source'] and not video['local_path']) or
                    fingerprints.get(video['id']) != _download_source_fingerprint(video) for video in current)):
            raise HTTPException(409, "Selection changed; create a new plan")
        # Admission happens in this single transaction: an error rolls back every queued job.
        queued, excluded = [], list(body.get('excluded', []))
        current_by_id = {video['id']: video for video in current}
        for video_id in ids:
            live = conn.execute("SELECT 1 FROM jobs WHERE video_id=? AND state IN ('queued','downloading','preparing')", (video_id,)).fetchone()
            video = current_by_id[video_id]
            if live or (video['state'] in {'playable', 'cached'} and video['playback_url']):
                excluded.append(video_id)
                continue
            _, job = enqueue_video(conn, video_id, False)
            if job: queued.append(job["id"])
        if not conn.execute("UPDATE download_plans SET consumed=1 WHERE id=? AND consumed=0", (plan_id,)).rowcount:
            raise HTTPException(409, "download plan already confirmed")
    return {"queued_job_ids": queued, "excluded": excluded}
