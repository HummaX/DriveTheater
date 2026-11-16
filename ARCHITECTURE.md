# DriveTheater integration contract

New public project: HummaX/DriveTheater. Old DriveConnector is read-only. No runtime secrets/data in source. Python/FastAPI + SQLite + FFmpeg worker; React/TypeScript + Vite + hls.js. Two Docker services (`web`, `worker`) share `/data`; web serves `frontend/dist` in development or `/app/ui` in the image. Package lives at `backend/drivetheater`; start web with `PYTHONPATH=backend uvicorn drivetheater.main:app`, worker with `PYTHONPATH=backend python -m drivetheater.worker`.

## Runtime configuration (environment)
`DT_DATA_DIR` (default `/data`, local dev must set temporary path), `DT_ADMIN_USER` (default `admin`), `DT_ADMIN_PASSWORD` (required only if creating initial admin; never resets an existing account), `DT_SECRET` (generated installer secret, >=32 characters), `DT_PUBLIC_URL` (optional OAuth browser URL), `DT_COOKIE_SECURE` (default false; true behind HTTPS), `DT_ALLOW_PRIVATE_SOURCES` (default false; test fixtures only), `DT_UI_DIR` (optional UI dist path). FFmpeg/ffprobe on PATH. SQLite WAL and runtime credential JSON under data directory, mode600 files. All API JSON errors `{detail:string}`; no credential content echoed.

## Shared data
Video:
```
{id,title,source_id,source_kind:'google_drive'|'http'|'yt-dlp',remote_id,
state:'cloud'|'queued'|'downloading'|'preparing'|'playable'|'cached'|'cancelled'|'failed',
mime_type,size_bytes:number|null,bytes_downloaded:number,duration:number|null,
width:number|null,height:number|null,thumbnail_url:string|null,poster_url:string|null,
preview_url:string|null,sprites_vtt_url:string|null,pinned:boolean,error:string|null,
audio_tracks:[{index:number,label:string,language:string,default:boolean}],
subtitle_tracks:[{id:string,label:string,language:string,url:string}],
playback:{mode:'direct'|'hls'|null,url:string|null,available_until:number|null,duration:number|null},
last_played:string|null,updated_at:string}
```
Unavailable URLs must be null (do not fake assets). IDs generated and validated server-side. Thumbnail URLs can be existing Google metadata images; they are not an excuse to fetch video content. The browser never starts jobs from effects on card mount/hover.

Job: `{id,video_id,title,state,phase,progress:number,bytes_downloaded,total_bytes:number|null,error:string|null,attempt:number,updated_at}`. Progress is 0..100 for its phase, not a claim that media processing completed.

Source: `{id,name,kind:'google_drive',auth_mode:'api_key'|'service_account'|'oauth',folder_id,status,video_count,last_sync,error,has_credentials:boolean,service_account_email:string|null,oauth_redirect_url:string|null}`. NEVER return API keys, private keys, access/refresh tokens.

## Endpoints
All authenticated by same-origin HttpOnly session cookie except health/login and OAuth callback. Standard methods:
- POST `/api/auth/login` `{username,password}` -> `{username}` + cookie; GET `/api/auth/me` -> `{username}`; POST `/api/auth/logout`.
- POST `/api/auth/password` `{current_password,new_password}` -> `{ok:true}`.
- GET `/api/health` liveness `{status:'ok',app:'drivetheater'}`; GET `/api/ready` database+worker readiness.
- GET `/api/videos?search=&state=` -> `{items:Video[],total:number}`; GET `/api/videos/{id}` -> Video.
- POST `/api/videos/{id}/play` -> `{video:Video,job:Job|null,playback:Video.playback}`; atomically enqueue/reuse one job. This is the only automatic play trigger.
- POST `/api/videos/{id}/download` -> same result for explicit Download.
- POST `/api/videos/{id}/pin` `{pinned:boolean}` -> Video.
- DELETE `/api/videos/{id}/cache` -> `{ok:true}` (catalog remains; active/pinned cannot be removed).
- POST `/api/videos/{id}/activity` `{active:boolean}` -> `{ok:true}`; protect actively watched data. No downloads.
- GET `/api/jobs` -> `{items:Job[]}`; POST `/api/jobs/{id}/cancel`, POST `/api/jobs/{id}/retry` -> Job.
- GET `/api/sources` -> `{items:Source[]}`.
- POST `/api/sources/google` `{name,folder,auth_mode,api_key?,service_account_json?,oauth_client_json?,redirect_base_url?}` -> Source; creates private credential file. Accept folder ID or link. Does not download videos.
- POST `/api/sources/{id}/test` -> `{ok:true,message:string}` (metadata access only).
- POST `/api/sources/{id}/sync` -> `{source:Source,added:number,updated:number}`; metadata-only sync, pagination, changed/removed states, no full media or preview fetch.
- POST `/api/sources/{id}/oauth/start` -> `{url:string}`; GET `/api/sources/google/callback` validates single-use state and stores refreshed credentials, redirects to `/?view=sources`.
- DELETE `/api/sources/{id}` -> `{ok:true}`; retain cached videos, no data wipe.
- POST `/api/urls` `{url,engine:'http'|'yt-dlp',title?:string,download?:boolean}` -> Video. Creates cloud record; download only if explicitly requested.
- GET `/api/settings` -> `{cache_limit_gb,min_free_gb,download_concurrency,max_transcodes,cache_used_bytes,disk_free_bytes,data_dir,ffmpeg_available,worker_online}`.
- PATCH `/api/settings` editable numeric keys cache_limit_gb/min_free_gb/download_concurrency/max_transcodes -> settings.
- POST `/api/cache/cleanup` -> `{removed:number,freed_bytes:number}`; only complete unpinned inactive LRU assets.
- GET/HEAD `/api/media/{id}/stream` -> local direct/growing cache Range response; unavailable bytes wait then explicit503 Retry-After, not false Content-Length/206.
- GET/HEAD `/api/media/{id}/assets/{path:path}` -> only contained completed HLS/art/preview/subtitle assets; publish typed content. HLS master may reference relative playlists. Cookie auth applies.

Frontend polls visible library/jobs every2seconds; player polls selected video. Avoid global refetch loops and cancel requests on unmount. Player uses HLS.js audioTracks on non-native platforms, native fallback; retain time/pause/rate when changing source, no destructive source reset for every progress update. HLS generation can become playable before full encoding completes; seekbar unavailable sections show preparing state.

## Media processing requirements
Metadata sync/browse/hover never trigger download. Only Play/Download enqueue durable unique work. Validated resumable HTTP/Google download to safe staging path; yt-dlp command as bounded subprocess. Cache-size/free-space checks, bounded queue, exclusive worker claim, cancellation and restart recovery. Fast-start compatible MP4 may be served progressively after initialization is detected; otherwise finish download, FFprobe, then generate HLS with separately selectable audio-language renditions. Genuine card clip/sprites generated from local requested media after Play. Subtitles to WebVTT. Do not claim unsupported codec/private Drive setup or universal partial-container playback.

## UI direction
Projection desk: light mineral #E9EEEB gallery, dark pine #173F42 navigation, ink #183036, slate secondary text (verify contrast), amber #B66A24 interactions, dark theater player. Local humanist font stack, artwork-led cards, quiet sidebar, no fabricated KPI tiles. Pages Library/Sources/Downloads/Settings and selected Player. Responsive, accessible, reduced-motion. Empty library explains how to connect a folder/add URL. Every visible control works. Cached clips only on intentional pointer hover; timeline sprites actual VTT. Vitest/unit helpers useful; built UI must type-check.

## File ownership
Backend implementation owns `backend/**` and `tests/backend/**`; frontend implementation owns `frontend/**`. Lead owns root Docker/Compose/install/docs/test integration. Do not commit, switch branches, modify reference repo or edit another owner's paths. Tell the lead promptly if an API contract needs adjustment.
