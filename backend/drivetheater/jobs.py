"""Shared transactional job admission for explicit single and confirmed bulk actions."""
from __future__ import annotations

import sqlite3
import uuid

from fastapi import HTTPException
from .db import utcnow


def enqueue_video(conn: sqlite3.Connection, video_id: str, immediate: bool = False):
    video = conn.execute('SELECT * FROM videos WHERE id=?', (video_id,)).fetchone()
    if not video:
        raise HTTPException(404, 'video not found')
    job = conn.execute("SELECT * FROM jobs WHERE video_id=? AND state IN ('queued','downloading','preparing')", (video_id,)).fetchone()
    if job:
        if immediate and job['state'] == 'queued':
            conn.execute('UPDATE jobs SET interactive=1 WHERE id=?', (job['id'],))
            job = conn.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone()
        return video, job
    if video['state'] == 'removing':
        raise HTTPException(409, 'cache removal is in progress')
    if video['removed_from_source'] and not video['local_path']:
        raise HTTPException(409, 'video was removed from its source')
    if video['state'] in ('playable', 'cached') and video['playback_url']:
        return video, None
    now, job_id = utcnow(), uuid.uuid4().hex
    conn.execute('INSERT INTO jobs(id,video_id,title,state,phase,interactive,updated_at) VALUES(?,?,?,?,?,?,?)',
                 (job_id, video_id, video['title'], 'queued', 'queued', int(immediate), now))
    conn.execute("UPDATE videos SET state='queued',updated_at=? WHERE id=?", (now, video_id))
    return (conn.execute('SELECT * FROM videos WHERE id=?', (video_id,)).fetchone(),
            conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
