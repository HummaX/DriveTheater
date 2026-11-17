from __future__ import annotations

import re
import time
from email.utils import formatdate
from pathlib import Path
from urllib.parse import quote
from typing import Iterator

from fastapi import HTTPException, Request, Response
from fastapi.responses import StreamingResponse

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")
ASSET_TYPES = {'.m3u8': 'application/vnd.apple.mpegurl', '.ts': 'video/mp2t',
               '.m4s': 'video/iso.segment', '.mp4': 'video/mp4', '.vtt': 'text/vtt',
               '.jpg': 'image/jpeg', '.png': 'image/png'}


def parse_range(header: str | None, total: int) -> tuple[int, int] | None:
    if not header:
        return None
    match = RANGE_RE.fullmatch(header.strip())
    if not match or total <= 0:
        raise ValueError('invalid Range header')
    first, last = match.groups()
    if not first:
        if not last or int(last) <= 0:
            raise ValueError('invalid suffix range')
        return max(0, total - int(last)), total - 1
    start = int(first)
    end = int(last) if last else total - 1
    if start >= total or end < start:
        raise IndexError('range not satisfiable')
    return start, min(end, total - 1)


def iter_file(path: Path, start: int, end: int, chunk: int = 256 * 1024) -> Iterator[bytes]:
    with path.open('rb') as handle:
        handle.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            block = handle.read(min(chunk, remaining))
            if not block:
                raise IOError('cached file ended before the advertised range')
            remaining -= len(block)
            yield block


def safe_asset(base: Path, name: str) -> Path:
    candidate = (base / name).resolve()
    if (base.resolve() not in candidate.parents or not candidate.is_file()
            or candidate.suffix.lower() not in ASSET_TYPES or candidate.name.endswith('.tmp')):
        raise FileNotFoundError('asset not found')
    return candidate


def content_type(path: Path) -> str:
    return ASSET_TYPES.get(path.suffix.lower(), 'application/octet-stream')


def _path_after_publish(path: Path) -> Path:
    if path.is_file():
        return path
    final = path.with_suffix('') if path.suffix == '.part' else path
    return final


def stream_cached(db, row: dict, request: Request) -> Response:
    """Serve only actual cache bytes; do not confuse an incomplete file with EOF.

    A known total length is required before a growing MP4 is advertised. Missing
    seeks wait briefly then return503 Retry-After. Once headers are sent, a failed
    source terminates the response rather than fabricating bytes to meet length.
    """
    if not row.get('local_path'):
        raise HTTPException(503, 'Media is preparing', headers={'Retry-After': '1'})
    original = Path(row['local_path'])
    path = _path_after_publish(original)
    if not path.is_file():
        raise HTTPException(503, 'Media is preparing', headers={'Retry-After': '1'})
    present = path.stat().st_size
    complete = row.get('state') in ('cached', 'playable') and path.suffix != '.part'
    total = present if complete else int(row.get('size_bytes') or 0)
    if total <= 0:
        raise HTTPException(503, 'Media length is not available yet', headers={'Retry-After': '1'})
    base = {'Accept-Ranges': 'bytes', 'Content-Type': row.get('mime_type') or 'video/mp4',
            'Cache-Control': 'private, no-store', 'X-Cache-Bytes': str(present)}
    if request.method == 'HEAD':
        return Response(status_code=200, headers={**base, 'Content-Length': str(total)})
    try:
        span = parse_range(request.headers.get('range'), total)
    except (ValueError, IndexError):
        return Response(status_code=416, headers={**base, 'Content-Range': f'bytes */{total}'})
    start, end = span or (0, total - 1)
    deadline = time.monotonic() + 3
    while present <= start and time.monotonic() < deadline:
        time.sleep(.1)
        path = _path_after_publish(original)
        present = path.stat().st_size if path.is_file() else 0
    if present <= start:
        raise HTTPException(503, 'That position has not downloaded yet', headers={'Retry-After': '1'})
    if span:
        # Content-Range covers bytes already written, not a prediction of future
        # download progress. A later range observes a fresh cache boundary.
        end = min(end, present - 1, start + 2 * 1024 * 1024 - 1) if not complete else end
    headers = {**base, 'X-Cache-Bytes': str(present)}
    if span or complete:
        headers['Content-Length'] = str(end - start + 1)
    if span:
        headers['Content-Range'] = f'bytes {start}-{end}/{total}'

    def growing():
        handle = None
        remaining = end - start + 1
        position = start
        last_data = time.monotonic()
        try:
            while remaining > 0:
                if handle is None:
                    current = _path_after_publish(original)
                    handle = current.open('rb')
                    handle.seek(position)
                block = handle.read(min(256 * 1024, remaining))
                if block:
                    position += len(block)
                    remaining -= len(block)
                    last_data = time.monotonic()
                    yield block
                    continue
                with db.connect() as connection:
                    status = connection.execute('SELECT state FROM videos WHERE id=?', (row['id'],)).fetchone()
                if not status or status['state'] in ('failed', 'cancelled', 'cloud') or time.monotonic() - last_data > 60:
                    raise IOError('source stopped before the requested range became available')
                # Windows cannot rename an open source in some configurations.
                # Reopen at the same byte offset so atomic publication can finish.
                handle.close()
                handle = None
                time.sleep(.1)
        finally:
            if handle is not None:
                handle.close()
    return StreamingResponse(growing(), status_code=206 if span else 200, headers=headers)


def original_download(db, row: dict, request: Request) -> Response:
    """Range-capable attachment from a complete source, never a provider proxy."""
    if not row.get('local_path'):
        raise HTTPException(409, 'Save this title to the server first. Device downloads never start a source download automatically.')
    path = _path_after_publish(Path(row['local_path']))
    if path.suffix == '.part' or not path.is_file():
        raise HTTPException(409, 'The original file is still downloading. Try the device download when it is complete.')
    stat = path.stat()
    total = stat.st_size
    if total <= 0:
        raise HTTPException(409, 'The original file is not ready')
    filename = re.sub(r'[\x00-\x1f\x7f/\\]', '_', row.get('title') or 'video').strip('. ')[:180] or 'video'
    if Path(filename).suffix.lower() not in ('.mp4', '.mkv', '.webm', '.mov', '.m4v', '.avi', '.ts', '.m2ts', '.wmv', '.flv', '.mpg', '.mpeg'):
        with path.open('rb') as handle:
            signature = handle.read(16)
        extension = '.mp4' if signature[4:8] == b'ftyp' else '.mkv' if signature[:4] == b'\x1aE\xdf\xa3' else {
            'video/mp4': '.mp4', 'video/webm': '.webm', 'video/x-matroska': '.mkv', 'video/x-msvideo': '.avi', 'video/mp2t': '.ts',
        }.get(str(row.get('mime_type') or '').split(';')[0], '.bin')
        filename += extension
    etag = '"' + (row.get('content_sha256') or f'{stat.st_mtime_ns:x}-{total:x}') + '"'
    modified = formatdate(stat.st_mtime, usegmt=True)
    safe_ascii = re.sub(r'[^a-zA-Z0-9._-]', '_', filename) or 'video.mp4'
    media_mime = str(row.get('mime_type') or '').split(';')[0].strip()
    if not media_mime or media_mime == 'application/octet-stream':
        media_mime = '.mkv' in filename.lower() and 'video/x-matroska' or 'video/mp4'
    headers = {'Accept-Ranges': 'bytes', 'Content-Type': media_mime,
               'Content-Disposition': f'attachment; filename="{safe_ascii}"; filename*=UTF-8\'\'{quote(filename, safe="")}',
               'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff',
               'ETag': etag, 'Last-Modified': modified}
    if request.method == 'HEAD':
        return Response(headers={**headers, 'Content-Length': str(total)})
    range_header = request.headers.get('range')
    if request.headers.get('if-range') and request.headers['if-range'] not in (etag, modified):
        range_header = None
    try:
        span = parse_range(range_header, total)
    except (ValueError, IndexError):
        return Response(status_code=416, headers={**headers, 'Content-Range': f'bytes */{total}'})
    start, end = span or (0, total - 1)
    headers['Content-Length'] = str(end - start + 1)
    if span:
        headers['Content-Range'] = f'bytes {start}-{end}/{total}'

    def protect():
        with db.transaction() as conn:
            changed = conn.execute("UPDATE videos SET active_until=MAX(active_until, ?) WHERE id=? AND state!='removing'",
                                   (int(time.time()) + 120, row['id'])).rowcount
        if not changed:
            raise HTTPException(409, 'Cache removal is in progress')
    protect()

    def chunks():
        last_touch = time.monotonic()
        for block in iter_file(path, start, end, chunk=1024 * 1024):
            if time.monotonic() - last_touch >= 30:
                protect()
                last_touch = time.monotonic()
            yield block
    return StreamingResponse(chunks(), status_code=206 if span else 200, headers=headers)
