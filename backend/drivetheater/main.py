from __future__ import annotations

import hmac
import asyncio
import json
import math
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from fastapi import Cookie, Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from .auth import (ensure_admin, make_session, revoke_session,
                   session_hash, session_user, verify_password, hash_password)
from .audit import recent_activity, record_event, record_watch, watch_history
from .config import Config, load_config
from .db import Database, decode_job, decode_source, decode_video, utcnow
from .media import content_type, original_download, safe_asset, stream_cached
from .metrics import ServerMetrics
from .sources import (GoogleAdapter, folder_id, google_remote_metadata, is_video_metadata,
                      normalize_oauth_client_config, validate_url, PublicDriveAdapter)
from .providers import classify_source, source_identity
from .subtitle_utils import caption_path, normalize_caption, validated_language

class Login(BaseModel): username: str; password: str
class PasswordChange(BaseModel): current_password: str; new_password: str = Field(min_length=10)
class Activity(BaseModel):
    active: bool
    position: float | None = Field(default=None, ge=0)
    duration: float | None = Field(default=None, gt=0)
class UrlRequest(BaseModel): url: str; engine: str = "auto"; title: str | None = None; download: bool = False
class GoogleRequest(BaseModel):
    name: str; folder: str; auth_mode: str; api_key: str | None = None; service_account_json: dict | None = None; oauth_client_json: dict | None = None; redirect_base_url: str | None = None
class SettingsPatch(BaseModel):
    cache_limit_gb: float | None = None
    min_free_gb: float | None = None
    download_concurrency: int | None = None
    max_transcodes: int | None = None
    activity_retention_days: int | None = Field(default=None, ge=1, le=3650)
    download_schedule_enabled: bool | None = None
    download_start_hour: int | None = Field(default=None, ge=0, le=23)
    download_end_hour: int | None = Field(default=None, ge=0, le=23)
    download_limit_mbps: float | None = Field(default=None, ge=0)
    auto_cache_cleanup: bool | None = None
    cache_cleanup_target_percent: int | None = Field(default=None, ge=1, le=100)


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or load_config()
    db = Database(cfg.database_path); db.initialize(); ensure_admin(db, cfg.admin_user, cfg.admin_password)
    cfg.credentials_dir.mkdir(parents=True, exist_ok=True); cfg.media_dir.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="DriveTheater")
    app.state.config, app.state.db, app.state.metrics = cfg, db, ServerMetrics()
    login_attempts: dict[tuple[str, str], tuple[int, float]] = {}
    login_attempts_lock = threading.Lock()
    login_window_seconds = 60
    login_max_attempts = 5
    login_attempts_limit = 1024

    @app.middleware("http")
    async def verify_mutation_origin(request: Request, call_next):
        """Reject cross-origin browser mutations while leaving CLI clients usable."""
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin:
                parsed = urlparse(origin)
                received_origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
                request_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
                configured_origin = (cfg.public_url or "").rstrip("/")
                if received_origin not in {request_origin, configured_origin}:
                    return JSONResponse({"detail": "invalid request origin"}, status_code=403)
        return await call_next(request)

    def _login_key(request: Request, username: str) -> tuple[str, str]:
        # A peer address is supplied by the ASGI server; never accept a spoofable forwarded header.
        return ((request.client.host if request.client else "unknown"), username.casefold())

    def _login_retry_after(key: tuple[str, str]) -> int | None:
        now = time.monotonic()
        with login_attempts_lock:
            expired = [candidate for candidate, (_, expires_at) in login_attempts.items() if expires_at <= now]
            for candidate in expired:
                login_attempts.pop(candidate, None)
            attempt = login_attempts.get(key)
            if attempt and attempt[0] >= login_max_attempts:
                return max(1, math.ceil(attempt[1] - now))
        return None

    def _record_login_failure(key: tuple[str, str]) -> None:
        now = time.monotonic()
        with login_attempts_lock:
            attempts, expires_at = login_attempts.get(key, (0, now + login_window_seconds))
            if expires_at <= now:
                attempts, expires_at = 0, now + login_window_seconds
            login_attempts[key] = (attempts + 1, expires_at)
            if len(login_attempts) > login_attempts_limit:
                oldest = min(login_attempts, key=lambda candidate: login_attempts[candidate][1])
                login_attempts.pop(oldest, None)

    def _clear_login_failures(key: tuple[str, str]) -> None:
        with login_attempts_lock:
            login_attempts.pop(key, None)

    def authenticated(dt_session: str | None = Cookie(None)) -> str:
        user = session_user(db, dt_session)
        if not user: raise HTTPException(401, "authentication required")
        return user
    def video_or_404(video_id: str):
        with db.connect() as conn: row=conn.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
        if not row: raise HTTPException(404, "video not found")
        return row
    def job_or_404(job_id: str):
        with db.connect() as conn: row=conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row: raise HTTPException(404, "job not found")
        return row

    @app.exception_handler(ValueError)
    async def value_error(_: Request, exc: ValueError): return JSONResponse({"detail": str(exc)}, status_code=400)
    @app.get("/api/health")
    def health(): return {"status":"ok", "app":"drivetheater"}
    @app.get("/api/ready")
    def ready():
        with db.connect() as conn: conn.execute("SELECT 1")
        with db.connect() as conn: row=conn.execute("SELECT value FROM settings WHERE key='worker_heartbeat'").fetchone()
        online=bool(row and int(row["value"]) > time.time()-30)
        return {"status":"ok" if online else "degraded", "database":True,"worker_online":online}

    @app.get('/api/events')
    async def events(request: Request, dt_session: str | None = Cookie(None), _: str = Depends(authenticated)):
        async def updates():
            checked = 0.0
            while not await request.is_disconnected():
                if time.monotonic() - checked > 15:
                    checked = time.monotonic()
                    if not session_user(db, dt_session):
                        yield 'data: {"type":"auth-expired"}\n\n'
                        return
                yield 'data: {"type":"invalidate"}\n\n'
                await asyncio.sleep(2)
        return StreamingResponse(updates(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no'})

    @app.post("/api/auth/login")
    def login(payload: Login, request: Request, response: Response):
        key = _login_key(request, payload.username)
        retry_after = _login_retry_after(key)
        if retry_after is not None:
            return JSONResponse(
                {"detail": "invalid username or password"},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        with db.connect() as conn: row=conn.execute("SELECT password_hash FROM users WHERE username=?", (payload.username,)).fetchone()
        if not row or not verify_password(payload.password, row["password_hash"]):
            _record_login_failure(key)
            record_event(db, "login_failure", username=payload.username)
            raise HTTPException(401, "invalid username or password")
        _clear_login_failures(key)
        token=make_session(db,payload.username); response.set_cookie("dt_session", token, httponly=True, samesite="lax", secure=cfg.cookie_secure, max_age=604800)
        record_event(db, "login_success", username=payload.username)
        return {"username":payload.username}
    @app.get("/api/auth/me")
    def me(user: str=Depends(authenticated)): return {"username":user}
    @app.post("/api/auth/logout")
    def logout(response: Response, dt_session: str | None = Cookie(None), user: str=Depends(authenticated)):
        revoke_session(db, dt_session)
        response.delete_cookie("dt_session")
        record_event(db, "logout", username=user)
        return {"ok":True}
    @app.post("/api/auth/password")
    def password(payload: PasswordChange, response: Response, user: str=Depends(authenticated)):
        with db.transaction() as conn:
            row=conn.execute("SELECT password_hash FROM users WHERE username=?",(user,)).fetchone()
            if not verify_password(payload.current_password,row["password_hash"]): raise HTTPException(400,"current password is incorrect")
            conn.execute("UPDATE users SET password_hash=? WHERE username=?",(hash_password(payload.new_password),user))
            conn.execute("DELETE FROM sessions WHERE username=?",(user,))
        response.delete_cookie("dt_session")
        return {"ok":True}

    @app.get("/api/server")
    def server_metrics(_: str=Depends(authenticated)):
        return app.state.metrics.snapshot(db, cfg.data_dir)
    @app.get("/api/activity")
    def activity_log(limit: int = Query(100, ge=1, le=200), _: str=Depends(authenticated)):
        return recent_activity(db, limit=limit)
    @app.post("/api/activity/clear")
    def clear_activity(user: str=Depends(authenticated)):
        with db.transaction() as conn:
            conn.execute("DELETE FROM activity_events WHERE username=?", (user,))
            conn.execute("DELETE FROM watch_history WHERE username=?", (user,))
        return {"ok":True}
    @app.get("/api/watch-history")
    def history(limit: int = Query(100, ge=1, le=200), user: str=Depends(authenticated)):
        return {"items": watch_history(db, user, limit)}

    @app.get("/api/alerts")
    def alerts(_: str = Depends(authenticated)):
        items: list[dict[str, str]] = []
        with db.connect() as conn:
            values = {row["key"]: row["value"] for row in conn.execute(
                "SELECT key,value FROM settings WHERE key IN ('min_free_gb','worker_heartbeat')"
            )}
            failed = int(conn.execute("SELECT COUNT(*) FROM jobs WHERE state='failed'").fetchone()[0])
            source_errors = int(conn.execute("SELECT COUNT(*) FROM sources WHERE deleted=0 AND status='error'").fetchone()[0])
        free_bytes = shutil.disk_usage(cfg.data_dir).free
        minimum_bytes = float(values.get("min_free_gb", "2")) * 1024 ** 3
        if free_bytes < minimum_bytes:
            items.append({"id": "low-disk", "level": "warning", "title": "Low disk space", "detail": "Free disk space is below the configured minimum."})
        if failed:
            items.append({"id": "failed-jobs", "level": "error", "title": "Failed downloads", "detail": f"{failed} job(s) need attention."})
        heartbeat = int(values.get("worker_heartbeat", "0") or 0)
        if heartbeat <= time.time() - 30:
            items.append({"id": "worker-offline", "level": "warning", "title": "Worker is offline", "detail": "No worker heartbeat was received in the last 30 seconds."})
        if source_errors:
            items.append({"id": "source-errors", "level": "error", "title": "Source sync errors", "detail": f"{source_errors} source(s) need attention."})
        policy = settings_dict()
        return {
            "items": items,
            "checked_at": utcnow(),
            "policy": {
                "schedule": policy["schedule"],
                "bandwidth": policy["bandwidth"],
                "cache": policy["cache"],
                "retention": policy["retention"],
            },
        }

    @app.get("/api/videos")
    def videos(search: str="", state: str="", user: str=Depends(authenticated)):
        q="SELECT v.*, h.resume_position AS user_watch_position FROM videos v LEFT JOIN watch_history h ON h.video_id=v.id AND h.username=? WHERE 1=1"; params=[user]
        if search: q+=" AND v.title LIKE ?"; params.append(f"%{search}%")
        if state: q+=" AND v.state=?"; params.append(state)
        q+=" ORDER BY COALESCE(v.last_played,v.updated_at) DESC"
        with db.connect() as conn: rows=conn.execute(q,params).fetchall()
        items=[]
        for row in rows:
            item=decode_video(row)
            item["watch_position"]=row["user_watch_position"]
            items.append(item)
        return {"items":items,"total":len(items)}
    @app.get("/api/videos/{video_id}")
    def video(video_id: str, _: str=Depends(authenticated)): return decode_video(video_or_404(video_id))
    # Preserve the retired pin endpoint's absence even though the item route otherwise
    # makes a POST to this path look like a method error.
    @app.post("/api/videos/{video_id}/pin", include_in_schema=False)
    def retired_pin(video_id: str):
        raise HTTPException(404, "not found")

    def queue(video_id: str, immediate: bool = False):
        with db.transaction() as conn:
            video=conn.execute("SELECT * FROM videos WHERE id=?",(video_id,)).fetchone()
            if not video: raise HTTPException(404,"video not found")
            job=conn.execute("SELECT * FROM jobs WHERE video_id=? AND state IN ('queued','downloading','preparing')",(video_id,)).fetchone()
            # An existing preparing job remains authoritative even if early bytes
            # have made the video look playable.
            if job:
                if immediate and job["state"] == "queued":
                    conn.execute('UPDATE jobs SET interactive=1 WHERE id=?', (job['id'],))
                    job=conn.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
                return video, job
            if video["state"] == "removing":
                raise HTTPException(409,"cache removal is in progress")
            if video["removed_from_source"] and not video["local_path"]:
                raise HTTPException(409,"video was removed from its source")
            if video["state"] in ("playable","cached") and video["playback_url"]:
                return video, None
            now=utcnow(); jid=db.new_id()
            try:
                conn.execute("INSERT INTO jobs(id,video_id,title,state,phase,interactive,updated_at) VALUES (?,?,?,?,?,?,?)",(jid,video_id,video["title"],"queued","queued",int(immediate),now))
            except sqlite3.IntegrityError:
                job=conn.execute("SELECT * FROM jobs WHERE video_id=? AND state IN ('queued','downloading','preparing')",(video_id,)).fetchone()
                if not job: raise
                if immediate and job["state"] == "queued":
                    conn.execute("UPDATE jobs SET interactive=1 WHERE id=?", (job["id"],))
                    job=conn.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
                return video, job
            conn.execute("UPDATE videos SET state='queued',updated_at=? WHERE id=?",(now,video_id))
            job=conn.execute("SELECT * FROM jobs WHERE id=?",(jid,)).fetchone(); video=conn.execute("SELECT * FROM videos WHERE id=?",(video_id,)).fetchone()
            return video,job
    @app.post("/api/videos/{video_id}/play")
    def play(video_id: str, user: str=Depends(authenticated)):
        video,job=queue(video_id, immediate=True)
        with db.transaction() as conn: conn.execute("UPDATE videos SET last_played=?,updated_at=? WHERE id=?",(utcnow(),utcnow(),video_id))
        record_event(db, "play", username=user, video_id=video_id)
        with db.connect() as conn: video=conn.execute("SELECT * FROM videos WHERE id=?",(video_id,)).fetchone()
        return {"video":decode_video(video),"job":decode_job(job),"playback":decode_video(video)["playback"]}
    @app.post("/api/videos/{video_id}/download")
    def download(video_id: str, user: str=Depends(authenticated)):
        video,job=queue(video_id)
        record_event(db, "download", username=user, video_id=video_id)
        return {"video":decode_video(video),"job":decode_job(job),"playback":decode_video(video)["playback"]}
    @app.post("/api/videos/{video_id}/activity")
    def activity(video_id: str, payload: Activity, user: str=Depends(authenticated)):
        video_or_404(video_id)
        record_watch(db, user, video_id, payload.position, payload.duration)
        if payload.active:
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE videos SET active_until=MAX(active_until, ?) WHERE id=?",
                    (int(time.time()) + 120, video_id),
                )
        return {"ok":True}

    @app.post("/api/videos/{video_id}/subtitles")
    async def upload_subtitles(video_id: str, file: UploadFile = File(...), language: str | None = Query(None), _: str=Depends(authenticated)):
        """Publish a validated sidecar caption without queueing or touching media bytes."""
        row = video_or_404(video_id)
        try:
            data = await file.read(5 * 1024 * 1024 + 1)
            original_name, normalized = normalize_caption(file.filename, data)
            lang = validated_language(language or "en")
            if lang != "en":
                raise ValueError("only English captions are supported")
            destination = caption_path(cfg.media_dir, video_id, lang, uuid.uuid4().hex[:12])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            await file.close()
        temporary = destination.with_suffix(".tmp")
        try:
            temporary.write_bytes(normalized)
            os.replace(temporary, destination)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise HTTPException(500, "caption upload could not be stored") from exc
        track = {"id": destination.stem, "language": lang, "label": Path(original_name).stem[:96] or lang,
                 "url": f"/api/media/{video_id}/assets/captions/{destination.name}", "kind": "subtitles"}
        with db.transaction() as conn:
            current = conn.execute("SELECT subtitle_tracks,state FROM videos WHERE id=?", (video_id,)).fetchone()
            if not current or current["state"] == "removing":
                destination.unlink(missing_ok=True)
                raise HTTPException(404 if not current else 409, "video not found" if not current else "cache removal is in progress")
            try:
                tracks = json.loads(current["subtitle_tracks"])
            except (TypeError, ValueError):
                tracks = []
            tracks.append(track)
            root = cfg.media_dir / video_id
            conn.execute("UPDATE videos SET asset_dir=COALESCE(asset_dir,?), subtitle_tracks=?, caption_status='ready',caption_error=NULL,updated_at=? WHERE id=?", (str(root), json.dumps(tracks), utcnow(), video_id))
        return decode_video(video_or_404(video_id))
    def _cache_paths(row) -> list[Path]:
        candidates = [Path(value) for value in (row["local_path"], row["asset_dir"]) if value]
        unique: list[Path] = []
        for candidate in sorted(set(candidates), key=lambda path: len(path.parts)):
            if not any(existing == candidate or existing in candidate.parents for existing in unique):
                unique.append(candidate)
        return unique

    def _remove_paths(paths: list[Path]) -> int:
        size = sum(
            file.stat().st_size for path in paths
            for file in (path.rglob("*") if path.is_dir() else (path,)) if file.is_file()
        )
        for path in paths:
            if path.is_dir(): shutil.rmtree(path, ignore_errors=True)
            else: path.unlink(missing_ok=True)
        return size

    def _reserve_cache_removal(video_id: str, *, automatic: bool = False):
        with db.transaction() as conn:
            row = conn.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
            if not row: raise HTTPException(404, "video not found")
            if row["active_until"] > time.time():
                raise HTTPException(409, "active video cannot be removed")
            active = conn.execute(
                "SELECT 1 FROM jobs WHERE video_id=? AND state IN ('queued','downloading','preparing')", (video_id,)
            ).fetchone()
            if active: raise HTTPException(409, "ongoing job cannot be removed")
            if row["state"] not in ("cached", "playable") and automatic:
                return None
            paths = _cache_paths(row)
            # Reserve before touching disk so queue/worker paths cannot race the deletion.
            conn.execute(
                "UPDATE videos SET state='removing', local_path=NULL, asset_dir=NULL, playback_mode=NULL, "
                "playback_url=NULL, playback_until=NULL, audio_tracks='[]', subtitle_tracks='[]', updated_at=? WHERE id=?",
                (utcnow(), video_id),
            )
            return row, paths

    def _finish_cache_removal(video_id: str, removed_from_source: bool) -> None:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE videos SET state=?, bytes_downloaded=0, error=CASE WHEN ? THEN 'Removed from Google Drive' ELSE NULL END, updated_at=? WHERE id=?",
                ("removed" if removed_from_source else "cloud", int(removed_from_source), utcnow(), video_id),
            )

    @app.delete("/api/videos/{video_id}/cache")
    def delete_cache(video_id: str, _: str=Depends(authenticated)):
        reserved = _reserve_cache_removal(video_id)
        assert reserved is not None
        row, paths = reserved
        _remove_paths(paths)
        _finish_cache_removal(video_id, bool(row["removed_from_source"]))
        return {"ok":True}

    @app.get("/api/jobs")
    def jobs(_: str=Depends(authenticated)):
        with db.connect() as conn: rows=conn.execute("SELECT * FROM jobs ORDER BY updated_at DESC").fetchall()
        return {"items":[decode_job(x) for x in rows]}
    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str, _: str=Depends(authenticated)):
        with db.transaction() as conn:
            job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job: raise HTTPException(404, "job not found")
            conn.execute("UPDATE jobs SET cancel_requested=1,state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END,updated_at=? WHERE id=?",(utcnow(),job_id))
            if job["state"] == "queued":
                conn.execute("UPDATE videos SET state=CASE WHEN local_path IS NULL THEN 'cancelled' ELSE state END,updated_at=? WHERE id=?", (utcnow(), job["video_id"]))
        return decode_job(job_or_404(job_id))
    @app.post("/api/jobs/{job_id}/retry")
    def retry(job_id: str, _: str=Depends(authenticated)):
        old=job_or_404(job_id)
        if old["state"] not in ("failed","cancelled"): raise HTTPException(409,"only terminal jobs can be retried")
        with db.transaction() as conn:
            existing=conn.execute("SELECT * FROM jobs WHERE video_id=? AND state IN ('queued','downloading','preparing')",(old["video_id"],)).fetchone()
            if existing: return decode_job(existing)
            jid=db.new_id()
            try:
                conn.execute("INSERT INTO jobs(id,video_id,title,state,phase,attempt,updated_at) VALUES (?,?,?,?,?,?,?)",(jid,old["video_id"],old["title"],"queued","queued",old["attempt"]+1,utcnow()))
            except sqlite3.IntegrityError:
                existing=conn.execute("SELECT * FROM jobs WHERE video_id=? AND state IN ('queued','downloading','preparing')",(old["video_id"],)).fetchone()
                if not existing: raise
                return decode_job(existing)
            conn.execute("UPDATE videos SET state='queued',updated_at=? WHERE id=?",(utcnow(),old["video_id"]))
        return decode_job(job_or_404(jid))

    def write_credentials(source_id: str, data: dict) -> str:
        path=cfg.credentials_dir/f"{source_id}.json"; path.write_text(json.dumps(data)); os.chmod(path,0o600); return str(path)
    @app.get("/api/sources")
    def sources(_: str=Depends(authenticated)):
        with db.connect() as conn: rows=conn.execute("SELECT * FROM sources WHERE deleted=0 ORDER BY name").fetchall()
        return {"items":[decode_source(x) for x in rows]}
    @app.post("/api/sources/google")
    def create_google(payload: GoogleRequest, _: str=Depends(authenticated)):
        selected_folder = folder_id(payload.folder)
        if payload.auth_mode not in ("api_key","service_account","oauth","public_link"):
            raise HTTPException(400,"unsupported auth mode")
        if payload.auth_mode == "public_link":
            creds = None
        else:
            creds = {"api_key":payload.api_key} if payload.auth_mode=="api_key" else (payload.service_account_json if payload.auth_mode=="service_account" else payload.oauth_client_json)
            if not creds: raise HTTPException(400,"credentials are required")
            try:
                if payload.auth_mode == "oauth": creds = normalize_oauth_client_config(creds)
                elif not isinstance(creds, dict): raise ValueError("invalid credentials")
            except ValueError as exc:
                raise HTTPException(400, "credential configuration is invalid") from exc
        sid=db.new_id(); cp=write_credentials(sid,creds) if creds is not None else None; email=creds.get("client_email") if isinstance(creds,dict) else None
        redirect=(payload.redirect_base_url or cfg.public_url or "").rstrip("/")+"/api/sources/google/callback" if payload.auth_mode=="oauth" else None
        try:
            with db.transaction() as conn:
                conn.execute("INSERT INTO sources(id,name,kind,auth_mode,folder_id,credential_path,service_account_email,oauth_redirect_url) VALUES (?,?,?,?,?,?,?,?)",
                             (sid,payload.name,"google_drive",payload.auth_mode,selected_folder,cp,email,redirect))
        except Exception:
            if cp:
                Path(cp).unlink(missing_ok=True)
            raise
        with db.connect() as conn: row=conn.execute("SELECT * FROM sources WHERE id=?",(sid,)).fetchone()
        return decode_source(row)
    def get_source(source_id: str):
        with db.connect() as conn: row=conn.execute("SELECT * FROM sources WHERE id=? AND deleted=0",(source_id,)).fetchone()
        if not row: raise HTTPException(404,"source not found")
        return row
    @app.post("/api/sources/{source_id}/test")
    def test_source(source_id: str, _: str=Depends(authenticated)):
        source=get_source(source_id)
        try:
            if source["auth_mode"] == "public_link":
                PublicDriveAdapter().test_metadata_access(source["folder_id"])
            else:
                credential_path=Path(source["credential_path"]); creds=json.loads(credential_path.read_text())
                GoogleAdapter(source["auth_mode"],creds,credential_path).test_metadata_access(source["folder_id"])
        except (OSError, ValueError, RuntimeError, httpx.HTTPError) as exc:
            raise HTTPException(400, "source metadata access failed") from exc
        return {"ok":True,"message":"metadata access succeeded"}
    @app.post("/api/sources/{source_id}/sync")
    def sync_source(source_id: str, _: str=Depends(authenticated)):
        source=get_source(source_id)
        with db.transaction() as conn:
            locked = conn.execute("SELECT status,sync_progress FROM sources WHERE id=? AND deleted=0", (source_id,)).fetchone()
            if not locked: raise HTTPException(404, "source not found")
            previous = json.loads(locked['sync_progress'] or '{}')
            if locked["status"] == "syncing" and previous.get('started_at', 0) > time.time() - 1800:
                raise HTTPException(409, "source sync is already in progress")
            started_at = int(time.time())
            conn.execute("UPDATE sources SET status='syncing',error=NULL,sync_progress=? WHERE id=?", (json.dumps({"phase": "listing", "processed": 0, "total": None, "started_at": started_at}), source_id))
        try:
            if source["auth_mode"] == "public_link":
                metadata = PublicDriveAdapter().list_metadata(source["folder_id"])
            else:
                credential_path=Path(source["credential_path"]); creds=json.loads(credential_path.read_text())
                metadata = GoogleAdapter(source["auth_mode"],creds,credential_path).list_metadata(source["folder_id"])
            videos = []
            processed = 0
            for item in metadata:
                processed += 1
                if is_video_metadata(item):
                    videos.append(google_remote_metadata(item))
                if processed % 20 == 0:
                    with db.transaction() as conn:
                        conn.execute('UPDATE sources SET sync_progress=? WHERE id=?',
                                     (json.dumps({'phase': 'listing', 'processed': processed, 'total': None, 'started_at': started_at}), source_id))
            added=updated=0
            with db.transaction() as conn:
                # Marking then clearing inside this one transaction avoids variable-limit NOT IN lists.
                if source['auth_mode'] != 'public_link':
                    conn.execute("UPDATE videos SET removed_from_source=1,error='Removed from Google Drive',state=CASE WHEN local_path IS NULL THEN 'removed' ELSE state END,updated_at=? WHERE source_id=?", (utcnow(), source_id))
                for meta in videos:
                    old=conn.execute("SELECT id FROM videos WHERE source_id=? AND source_kind='google_drive' AND remote_id=?",(source_id,meta["remote_id"])).fetchone()
                    if old:
                        conn.execute(
                            "UPDATE videos SET title=?,mime_type=COALESCE(?,mime_type),size_bytes=COALESCE(?,size_bytes),"
                            "duration=COALESCE(?,duration),width=CASE WHEN state IN ('cached','playable','removing') THEN width ELSE COALESCE(?,width) END,"
                            "height=CASE WHEN state IN ('cached','playable','removing') THEN height ELSE COALESCE(?,height) END,"
                            "thumbnail_url=COALESCE(?,thumbnail_url),removed_from_source=0,"
                            "state=CASE WHEN state='removed' THEN 'cloud' ELSE state END,error=NULL,updated_at=? WHERE id=?",
                            (*[meta[k] for k in ("title","mime_type","size_bytes","duration","width","height","thumbnail_url")],utcnow(),old["id"]),
                        ); updated+=1
                    else:
                        # The database treats a Drive file ID as globally unique. A shared file
                        # seen through another folder remains owned by its first source instead
                        # of violating that invariant or moving its cached state between sources.
                        shared=conn.execute("SELECT id FROM videos WHERE source_kind='google_drive' AND remote_id=?", (meta["remote_id"],)).fetchone()
                        if shared:
                            continue
                        conn.execute("INSERT INTO videos(id,title,source_id,source_kind,remote_id,mime_type,size_bytes,duration,width,height,thumbnail_url,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",(db.new_id(),meta["title"],source_id,"google_drive",meta["remote_id"],meta["mime_type"],meta["size_bytes"],meta["duration"],meta["width"],meta["height"],meta["thumbnail_url"],utcnow())); added+=1
                progress=json.dumps({"phase": "complete", "processed": processed, "total": processed})
                conn.execute("UPDATE sources SET video_count=(SELECT count(*) FROM videos WHERE source_id=?),last_sync=?,status='ready',error=NULL,sync_progress=? WHERE id=?",(source_id,utcnow(),progress,source_id))
        except (OSError, ValueError, RuntimeError, httpx.HTTPError) as exc:
            with db.transaction() as conn: conn.execute("UPDATE sources SET status='error',error='metadata sync failed',sync_progress=? WHERE id=?", (json.dumps({"phase": "failed", "processed": 0, "total": None}), source_id))
            raise HTTPException(400, "source metadata sync failed") from exc
        return {"source":decode_source(get_source(source_id)),"added":added,"updated":updated}
    @app.post("/api/sources/{source_id}/oauth/start")
    def oauth_start(source_id: str, dt_session: str | None = Cookie(None), _: str=Depends(authenticated)):
        source=get_source(source_id)
        if source["auth_mode"]!="oauth": raise HTTPException(400,"source does not use OAuth")
        if not dt_session: raise HTTPException(401, "authentication required")
        state=db.new_id()
        state_data={"source_id": source_id, "session_hash": session_hash(dt_session), "expires_at": int(time.time()) + 600}
        with db.transaction() as conn: conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)",(f"oauth:{state}",json.dumps(state_data)))
        client=json.loads(Path(source["credential_path"]).read_text())
        redirect=source["oauth_redirect_url"] or ""
        params={"client_id":client.get("client_id", ""),"redirect_uri":redirect,"response_type":"code","scope":"https://www.googleapis.com/auth/drive.readonly","access_type":"offline","prompt":"consent","state":state}
        return {"url":"https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)}
    @app.get("/api/sources/google/callback")
    def oauth_callback(state: str, code: str, dt_session: str | None = Cookie(None)):
        with db.transaction() as conn:
            row=conn.execute("SELECT value FROM settings WHERE key=?",(f"oauth:{state}",)).fetchone()
            conn.execute("DELETE FROM settings WHERE key=?",(f"oauth:{state}",))
        try:
            state_data=json.loads(row["value"]) if row else None
        except (TypeError, ValueError):
            state_data=None
        if (not state_data or not dt_session or state_data.get("expires_at", 0) < time.time()
                or not hmac.compare_digest(state_data.get("session_hash", ""), session_hash(dt_session))
                or not session_user(db, dt_session)):
            raise HTTPException(400,"invalid or expired OAuth state")
        source=get_source(state_data["source_id"])
        client=json.loads(Path(source["credential_path"]).read_text())
        try:
            response=httpx.post("https://oauth2.googleapis.com/token",data={"code":code,"client_id":client.get("client_id"),"client_secret":client.get("client_secret"),"redirect_uri":source["oauth_redirect_url"],"grant_type":"authorization_code"},timeout=20)
            response.raise_for_status()
            tokens=response.json()
            if not tokens.get("access_token"): raise ValueError("missing access token")
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(400,"OAuth token exchange failed") from exc
        # Keep client configuration and refresh material private, with no echo to API callers.
        client.update({key:tokens[key] for key in ("access_token","refresh_token","token_type") if key in tokens})
        client["expires_at"] = int(time.time()) + int(tokens.get("expires_in", 3600))
        write_credentials(source["id"],client)
        return RedirectResponse(url="/?view=sources&oauth=connected")
    @app.delete("/api/sources/{source_id}")
    def remove_source(source_id: str, _: str=Depends(authenticated)):
        get_source(source_id)
        with db.transaction() as conn: conn.execute("UPDATE sources SET deleted=1 WHERE id=?",(source_id,))
        return {"ok":True}

    @app.post("/api/urls")
    def add_url(payload: UrlRequest, _: str=Depends(authenticated)):
        try:
            detected_kind, source_url = classify_source(payload.url, cfg.allow_private_sources)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if payload.engine == "auto":
            kind = detected_kind
        elif payload.engine in ("http", "yt-dlp", "torrent"):
            if payload.engine in ("http", "torrent") and detected_kind != payload.engine:
                raise HTTPException(400, f"URL is not a {payload.engine} source")
            kind = payload.engine
        else:
            raise HTTPException(400, "unsupported engine")
        parsed=urlparse(source_url); identity=source_identity(kind, source_url)
        title=payload.title or Path(parsed.path).name or parsed.netloc or "Magnet download"
        with db.transaction() as conn:
            existing=conn.execute("SELECT * FROM videos WHERE source_kind=? AND remote_id=?", (kind, identity)).fetchone()
            if existing:
                video_id=existing["id"]; reused=True
            else:
                video_id=db.new_id(); reused=False
                conn.execute("INSERT INTO videos(id,title,source_kind,remote_id,source_url,state,updated_at) VALUES (?,?,?,?,?,?,?)",(video_id,title,kind,identity,source_url,"cloud",utcnow()))
        if payload.download:
            queue(video_id)
        with db.connect() as conn: row=conn.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
        result=decode_video(row)
        if reused:
            result["reused"] = True
        return result

    def settings_dict():
        with db.connect() as conn: vals={x["key"]:x["value"] for x in conn.execute("SELECT key,value FROM settings")}
        used=sum(p.stat().st_size for p in cfg.media_dir.rglob("*") if p.is_file()); free=shutil.disk_usage(cfg.data_dir).free
        schedule = {
            "enabled": vals.get("download_schedule_enabled", "false").lower() == "true",
            "start_hour": int(vals.get("download_start_hour", "0")),
            "end_hour": int(vals.get("download_end_hour", "6")),
        }
        bandwidth = {"download_limit_mbps": float(vals.get("download_limit_mbps", "0"))}
        cache = {
            "limit_gb": float(vals["cache_limit_gb"]),
            "min_free_gb": float(vals["min_free_gb"]),
            "auto_cleanup": vals.get("auto_cache_cleanup", "true").lower() == "true",
            "cleanup_target_percent": int(vals.get("cache_cleanup_target_percent", "90")),
            "used_bytes": used,
        }
        retention = {"activity_days": int(vals.get("activity_retention_days", "30"))}
        return {
            "cache_limit_gb":cache["limit_gb"], "min_free_gb":cache["min_free_gb"],
            "download_concurrency":int(vals["download_concurrency"]), "max_transcodes":int(vals["max_transcodes"]),
            "activity_retention_days":retention["activity_days"],
            "download_schedule_enabled":schedule["enabled"],
            "download_start_hour":schedule["start_hour"],
            "download_end_hour":schedule["end_hour"],
            "download_limit_mbps":bandwidth["download_limit_mbps"],
            "auto_cache_cleanup":cache["auto_cleanup"],
            "cache_cleanup_target_percent":cache["cleanup_target_percent"],
            "cache_used_bytes":cache["used_bytes"],"disk_free_bytes":free,"data_dir":str(cfg.data_dir),
            "ffmpeg_available":shutil.which("ffmpeg") is not None,"worker_online":int(vals.get("worker_heartbeat","0"))>time.time()-30,
            "schedule": schedule, "bandwidth": bandwidth, "cache": cache, "retention": retention,
        }
    @app.get("/api/settings")
    def settings(_: str=Depends(authenticated)): return settings_dict()
    @app.patch("/api/settings")
    def patch_settings(payload: SettingsPatch, _: str=Depends(authenticated)):
        values=payload.model_dump(exclude_none=True)
        for key, val in values.items():
            if not math.isfinite(float(val)):
                raise HTTPException(400, f"{key} must be finite")
            if key in ("download_concurrency", "max_transcodes") and (int(val) != val or not 1 <= int(val) <= 2):
                raise HTTPException(400, f"{key} must be between 1 and 2")
            if key == "cache_limit_gb" and float(val) <= 0:
                raise HTTPException(400, "cache_limit_gb must be positive")
            if key == "min_free_gb" and float(val) < .1:
                raise HTTPException(400, "min_free_gb must be at least 0.1")
        with db.transaction() as conn:
            for key,val in values.items(): conn.execute("UPDATE settings SET value=? WHERE key=?",(str(val),key))
        return settings_dict()
    @app.post("/api/cache/cleanup")
    def cleanup(_: str=Depends(authenticated)):
        limit=int(settings_dict()["cache_limit_gb"]*1024**3); used=settings_dict()["cache_used_bytes"]; freed=removed=0
        with db.connect() as conn: rows=conn.execute("SELECT * FROM videos WHERE pinned=0 AND active_until<? AND state IN ('cached','playable') ORDER BY COALESCE(last_played,updated_at)",(int(time.time()),)).fetchall()
        for candidate in rows:
            if used <= limit: break
            try:
                reserved = _reserve_cache_removal(candidate["id"], automatic=True)
            except HTTPException:
                continue
            if not reserved: continue
            row, paths = reserved
            size = _remove_paths(paths)
            _finish_cache_removal(row["id"], bool(row["removed_from_source"]))
            used-=size; freed+=size; removed+=1
        return {"removed":removed,"freed_bytes":freed}

    @app.api_route("/api/media/{video_id}/download", methods=["GET", "HEAD"])
    def device_download(video_id: str, request: Request, user: str=Depends(authenticated)):
        row = video_or_404(video_id)
        if row['local_path']:
            candidate = Path(row['local_path']).resolve()
            if cfg.media_dir.resolve() not in candidate.parents:
                raise HTTPException(404, 'original file not found')
        response = original_download(db, dict(row), request)
        if request.method == 'GET' and response.status_code in (200, 206):
            record_event(db, 'device_download', username=user, video_id=video_id)
        return response

    @app.api_route("/api/media/{video_id}/stream", methods=["GET","HEAD"])
    def stream(video_id: str, request: Request, _: str=Depends(authenticated)):
        row=video_or_404(video_id)
        return stream_cached(db, dict(row), request)
    @app.api_route("/api/media/{video_id}/assets/{asset_path:path}", methods=["GET","HEAD"])
    def assets(video_id: str, asset_path: str, request: Request, _: str=Depends(authenticated)):
        row=video_or_404(video_id)
        if not row["asset_dir"]: raise HTTPException(404,"asset not found")
        try: path=safe_asset(Path(row["asset_dir"]),asset_path)
        except FileNotFoundError: raise HTTPException(404,"asset not found")
        if request.method=="HEAD": return Response(headers={"Content-Type":content_type(path),"Content-Length":str(path.stat().st_size),"Cache-Control":"private, no-store"})
        return FileResponse(path,media_type=content_type(path),headers={"Cache-Control":"private, no-store"})

    from .addons import router as addons_router
    app.include_router(addons_router)

    ui_dir = Path(os.environ.get("DT_UI_DIR", "")).expanduser() if os.environ.get("DT_UI_DIR") else None
    @app.get("/{frontend_path:path}", include_in_schema=False)
    def spa(frontend_path: str):
        if frontend_path.startswith("api/"):
            raise HTTPException(404, "not found")
        if not ui_dir or not ui_dir.is_dir():
            raise HTTPException(404, "UI is not installed")
        index = ui_dir / "index.html"
        if not index.is_file():
            raise HTTPException(404, "UI is not installed")
        candidate = (ui_dir / frontend_path).resolve()
        if frontend_path and ui_dir.resolve() in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)
    return app

app=create_app()
