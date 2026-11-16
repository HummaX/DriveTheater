from __future__ import annotations

import json
import math
import time
from typing import Any

from .db import Database, utcnow

_MAX_ACTIVITY_LIMIT = 200
_VALID_EVENTS = {"login_success", "login_failure", "logout", "play", "download", "device_download", "watch"}


def retention_days(db: Database) -> int:
    with db.connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='activity_retention_days'").fetchone()
    try:
        return max(1, min(3650, int(row["value"] if row else 30)))
    except (TypeError, ValueError):
        return 30


def prune_activity(conn, days: int) -> None:
    """Remove expired per-user audit and resume records in one transaction."""
    cutoff = int(time.time()) - days * 86400
    conn.execute("DELETE FROM activity_events WHERE occurred_at < ?", (cutoff,))
    conn.execute("DELETE FROM watch_history WHERE last_watched_at < ?", (cutoff,))


def clear_user_activity(db: Database, username: str) -> dict[str, int]:
    """Permanently clear one user's local activity without touching shared media."""
    if not isinstance(username, str) or not username:
        raise ValueError("username is required")
    with db.transaction() as conn:
        history = conn.execute("DELETE FROM watch_history WHERE username=?", (username,)).rowcount
        events = conn.execute("DELETE FROM activity_events WHERE username=?", (username,)).rowcount
    return {"watch_history": max(0, history), "activity_events": max(0, events)}


def record_event(
    db: Database,
    event_type: str,
    *,
    username: str | None = None,
    video_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Store local audit metadata only; callers must never supply secrets."""
    if event_type not in _VALID_EVENTS:
        raise ValueError("unsupported activity event")
    details_json = json.dumps(details or {}, separators=(",", ":"))
    occurred_at = int(time.time())
    days = retention_days(db)
    with db.transaction() as conn:
        # Heartbeats can be frequent; retain an activity trail without unbounded churn.
        if event_type == "watch" and username and video_id:
            recent = conn.execute(
                "SELECT 1 FROM activity_events WHERE event_type='watch' AND username=? AND video_id=? "
                "AND occurred_at>? LIMIT 1",
                (username, video_id, occurred_at - 15),
            ).fetchone()
            if recent:
                return
        conn.execute(
            "INSERT INTO activity_events(occurred_at,created_at,username,event_type,video_id,details) VALUES (?,?,?,?,?,?)",
            (occurred_at, utcnow(), username, event_type, video_id, details_json),
        )
        prune_activity(conn, days)


def record_watch(
    db: Database,
    username: str,
    video_id: str,
    position: float | None,
    duration: float | None,
) -> None:
    """Upsert the user's resume point and append a rate-limited watch audit event."""
    if position is not None and (position < 0 or not math.isfinite(position)):
        raise ValueError("position must be a non-negative finite number")
    if duration is not None and (duration <= 0 or not math.isfinite(duration)):
        raise ValueError("duration must be a positive finite number")
    if position is not None and duration is not None and position > duration + 1:
        raise ValueError("position cannot exceed duration")
    occurred_at = int(time.time())
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO watch_history(username,video_id,resume_position,duration,last_played,last_watched_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(username,video_id) DO UPDATE SET "
            "resume_position=CASE WHEN ? IS NULL THEN watch_history.resume_position ELSE excluded.resume_position END,"
            "duration=COALESCE(excluded.duration,watch_history.duration),"
            "last_played=excluded.last_played,last_watched_at=excluded.last_watched_at",
            (username, video_id, 0.0 if position is None else position, duration, utcnow(), occurred_at, position),
        )
    record_event(db, "watch", username=username, video_id=video_id, details={
        "position": position,
        "duration": duration,
    })


def recent_activity(db: Database, *, username: str | None = None, limit: int = 100) -> dict[str, Any]:
    limit = max(1, min(_MAX_ACTIVITY_LIMIT, limit))
    with db.connect() as conn:
        if username:
            rows = conn.execute(
                "SELECT occurred_at,created_at,username,event_type,video_id,details FROM activity_events "
                "WHERE username=? ORDER BY occurred_at DESC,id DESC LIMIT ?",
                (username, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT occurred_at,created_at,username,event_type,video_id,details FROM activity_events "
                "ORDER BY occurred_at DESC,id DESC LIMIT ?", (limit,)
            ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        try:
            item["details"] = json.loads(item["details"])
        except (TypeError, ValueError):
            item["details"] = {}
        items.append(item)
    return {"items": items, "retention_days": retention_days(db)}


def watch_history(db: Database, username: str, limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(_MAX_ACTIVITY_LIMIT, limit))
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT h.video_id,h.resume_position,h.duration,h.last_played,h.last_watched_at,v.title,v.thumbnail_url "
            "FROM watch_history h JOIN videos v ON v.id=h.video_id WHERE h.username=? "
            "ORDER BY h.last_watched_at DESC LIMIT ?", (username, limit),
        ).fetchall()
    return [dict(row) for row in rows]
