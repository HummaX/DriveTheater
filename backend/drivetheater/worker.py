from __future__ import annotations

import concurrent.futures
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urljoin

import httpx

from .config import load_config
from .db import Database, utcnow
from .sources import GoogleAdapter, validate_url
from .resources import media_threads, media_cpus, cpu_limit
from .downloader_tools import download_external
from .languages import preferred_audio, english_subtitle
from .cache_safety import automatic_cleanup
from .notifications import emit_event, process_outbox
from .diagnostics import record_probe, transition_stage, finish_job, record_sample
from .caption_index import index_existing_captions
from .storage_features import storage_maintenance
from .transfer_policy import TransferThrottle, allowed_now, limit_bytes as configured_limit_bytes

ACTIVE = ('queued', 'downloading', 'preparing')
STOP = threading.Event()
ENCODERS = threading.BoundedSemaphore(1)
WORKER_LOCK_FD = None


class Cancelled(Exception):
    pass


class Control:
    def __init__(self, db: Database, job_id: str, data: Path):
        self.db, self.job_id, self.data = db, job_id, data
        self.last_budget = 0.0
        self.last_phase = None

    def check(self):
        if STOP.is_set():
            raise Cancelled('Worker is stopping; retry resumes retained source bytes')
        with self.db.connect() as conn:
            row = conn.execute('SELECT cancel_requested FROM jobs WHERE id=?', (self.job_id,)).fetchone()
        if not row or row['cancel_requested']:
            raise Cancelled('Cancelled by viewer')

    def settings(self, *keys):
        placeholders = ','.join('?' for _ in keys)
        with self.db.connect() as conn:
            return {row['key']: row['value'] for row in conn.execute(
                f'SELECT key,value FROM settings WHERE key IN ({placeholders})', keys
            )}

    def limit_bytes(self) -> int:
        """Configured direct-HTTP limit in bytes/sec; zero means unlimited."""
        return configured_limit_bytes(self.settings('download_limit_mbps').get('download_limit_mbps'))

    def budget(self):
        self.check()
        if time.monotonic() - self.last_budget < 2:
            return
        self.last_budget = time.monotonic()
        values = self.settings('min_free_gb', 'cache_limit_gb')
        used = sum(item.stat().st_size for item in (self.data / 'cache').rglob('*') if item.is_file())
        if shutil.disk_usage(self.data).free < float(values.get('min_free_gb', 2)) * 1024**3:
            raise RuntimeError('Free-space headroom reached; remove cached media or increase disk capacity')
        if used >= float(values.get('cache_limit_gb', 20)) * 1024**3:
            raise RuntimeError('Cache budget reached; clean up cached media or increase the budget')

    def progress(self, phase, done=0, total=None, state=None, unit='bytes'):
        if phase != self.last_phase:
            transition_stage(self.db, self.job_id, phase)
            self.last_phase = phase
        known = total is not None and total > 0
        with self.db.transaction() as conn:
            conn.execute('UPDATE jobs SET phase=?,progress_known=?,progress_unit=?,progress=?,claimed_at=?,updated_at=? WHERE id=?',
                         (phase, int(known), unit if known else 'indeterminate', min(100, max(0, 100 * done / total)) if known else 0,
                          int(time.time()), utcnow(), self.job_id))
            if unit == 'bytes' and phase == 'download':
                conn.execute('UPDATE jobs SET bytes_downloaded=?,total_bytes=? WHERE id=?', (done, total, self.job_id))
            if state:
                conn.execute('UPDATE jobs SET state=? WHERE id=?', (state, self.job_id))
                conn.execute('UPDATE videos SET state=?,updated_at=? WHERE id=(SELECT video_id FROM jobs WHERE id=?)', (state, utcnow(), self.job_id))


def _terminate(process):
    if process.poll() is not None:
        return
    if os.name == 'posix':
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == 'posix':
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=10)


def run(command, timeout=120, control=None, tick=None, on_output=None):
    """Bounded process group with cancellable, independently read progress output."""
    with tempfile.TemporaryDirectory(prefix='dt-process-') as temporary:
        output_path, error_path = Path(temporary) / 'stdout', Path(temporary) / 'stderr'
        with output_path.open('wb') as output, error_path.open('wb') as errors:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
                                       start_new_session=os.name == 'posix',
                                       pass_fds=(WORKER_LOCK_FD,) if os.name == 'posix' and WORKER_LOCK_FD is not None else (),
                                       creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
            started = time.monotonic()
            cursor = 0
            pending = ''

            def consume():
                nonlocal cursor, pending
                if on_output is None:
                    return
                with output_path.open('rb') as reader:
                    reader.seek(cursor)
                    data = reader.read(1024 * 1024)
                    cursor += len(data)
                pending += data.decode('utf-8', errors='replace')
                lines = pending.split('\n')
                pending = lines.pop()[-8192:]
                for line in lines:
                    on_output(line.rstrip('\r'))

            try:
                while process.poll() is None:
                    if control:
                        control.budget()
                    if tick:
                        tick()
                    consume()
                    if time.monotonic() - started > timeout:
                        raise RuntimeError('Media process exceeded its execution deadline')
                    time.sleep(.15)
                consume()
                if process.returncode:
                    stderr = error_path.read_text(encoding='utf-8', errors='replace')
                    message = stderr[-1800:] if Path(command[0]).name in ('ffmpeg', 'ffmpeg.exe', 'ffprobe', 'ffprobe.exe') else 'Source downloader failed; the provider may restrict this URL'
                    raise RuntimeError(message)
                return output_path.read_text(encoding='utf-8', errors='replace')
            finally:
                _terminate(process)


def ffmpeg_progress(control, phase, duration):
    last = [0.0]

    def update(line):
        if not control or not duration or not line.startswith('out_time_us='):
            return
        try:
            seconds = float(line.split('=', 1)[1]) / 1000000
        except ValueError:
            return
        if time.monotonic() - last[0] >= .5 or seconds >= duration:
            control.progress(phase, seconds, duration, unit='media_seconds')
            last[0] = time.monotonic()
    return update


def ffprobe(path, control=None, timeout=30):
    return json.loads(run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)], timeout, control))


def _faststart_playable(path, probe):
    streams = probe.get('streams', [])
    videos = [s for s in streams if s.get('codec_type') == 'video']
    audios = [s for s in streams if s.get('codec_type') == 'audio']
    if (len(videos) != 1 or videos[0].get('codec_name') != 'h264'
            or videos[0].get('pix_fmt') not in ('yuv420p', 'yuvj420p') or len(audios) > 1
            or any(s.get('codec_name') not in ('aac', 'mp3') for s in audios)):
        return False
    if 'mov' not in probe.get('format', {}).get('format_name', ''):
        return False
    with path.open('rb') as handle:
        offset, available = 0, path.stat().st_size
        while offset + 8 <= min(available, 8 * 1024 * 1024):
            handle.seek(offset)
            size, atom = struct.unpack('>I4s', handle.read(8))
            header = 8
            if size == 1:
                extra = handle.read(8)
                if len(extra) != 8:
                    return False
                size = struct.unpack('>Q', extra)[0]
                header = 16
            if size < header:
                return False
            if atom == b'mdat':
                return False
            if atom == b'moov':
                return offset + size <= available
            offset += size
    return False


def _metadata(db, video_id, probe):
    video = next((s for s in probe.get('streams', []) if s.get('codec_type') == 'video'), {})
    duration = float(probe.get('format', {}).get('duration') or video.get('duration') or 0)
    with db.transaction() as conn:
        conn.execute('UPDATE videos SET duration=?,width=?,height=?,updated_at=? WHERE id=?',
                     (duration or None, video.get('width'), video.get('height'), utcnow(), video_id))
    return duration


def _early_playback(partial, total, control, video_id):
    if not total or partial.stat().st_size < 192 * 1024:
        return
    try:
        probe = ffprobe(partial, timeout=4)
        if not _faststart_playable(partial, probe):
            return
        duration = _metadata(control.db, video_id, probe)
        with control.db.transaction() as conn:
            conn.execute("UPDATE videos SET local_path=?,playback_mode='direct',playback_url=?,mime_type='video/mp4',size_bytes=?,playback_until=?,updated_at=? WHERE id=?",
                         (str(partial), f'/api/media/{video_id}/stream', total,
                          int(duration * partial.stat().st_size / total) if duration else None, utcnow(), video_id))
    except (RuntimeError, ValueError, OSError, json.JSONDecodeError):
        return  # Non-faststart inputs wait for a completed source, without a false promise.


def _download_http(url, target, job_id, db, base_headers=None, control=None, video_id=None, allow_private=False, max_bytes=None):
    partial = target.with_suffix(target.suffix + '.part')
    sidecar = partial.with_suffix(partial.suffix + '.json')
    control = control or Control(db, job_id, target.parent.parent.parent)
    for attempt in range(2):
        control.budget()
        old = {}
        if sidecar.is_file():
            with contextlib.suppress(ValueError, OSError):
                old = json.loads(sidecar.read_text())
        offset = partial.stat().st_size if partial.is_file() else 0
        if offset and not old.get('validator'):
            partial.unlink()
            offset = 0
        headers = {**(base_headers or {}), 'Accept-Encoding': 'identity'}
        if offset:
            headers.update(Range=f'bytes={offset}-', **{'If-Range': old['validator']})
        with httpx.Client(timeout=httpx.Timeout(60, read=30), follow_redirects=False) as client:
            current = validate_url(url, allow_private)
            response = None
            for redirects in range(6):
                request = client.build_request('GET', current, headers=headers)
                response = client.send(request, stream=True)
                if response.status_code not in (301, 302, 303, 307, 308):
                    break
                location = response.headers.get('location')
                response.close()
                if not location or redirects == 5:
                    raise RuntimeError('Source redirect is missing or exceeded the limit')
                next_url = validate_url(urljoin(current, location), allow_private)
                # Never forward a Google bearer token to an unrelated redirect origin.
                if httpx.URL(next_url).host != httpx.URL(current).host:
                    headers.pop('Authorization', None)
                current = next_url
            try:
                if response.status_code not in (200, 206):
                    raise RuntimeError(f'Source returned HTTP {response.status_code}; check access, quota and URL expiry')
                validator = response.headers.get('etag')
                if validator and validator.startswith('W/'):
                    validator = None
                validator = validator or response.headers.get('last-modified')
                content_range = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('content-range', ''))
                if offset and (response.status_code != 206 or not content_range or int(content_range[1]) != offset
                               or validator != old.get('validator') or (old.get('total') and int(content_range[3]) != old['total'])):
                    partial.unlink(missing_ok=True)
                    sidecar.unlink(missing_ok=True)
                    continue
                if response.status_code == 206:
                    if not content_range or int(content_range[1]) != offset:
                        raise RuntimeError('Source returned an invalid Content-Range')
                    total = int(content_range[3])
                    expected = int(content_range[2]) - int(content_range[1]) + 1
                else:
                    total = int(response.headers['content-length']) if response.headers.get('content-length', '').isdigit() else None
                    expected = total
                sidecar.write_text(json.dumps({'validator': validator, 'total': total}))
                os.chmod(sidecar, 0o600)
                received, last_probe = 0, 0
                throttle = TransferThrottle(control.limit_bytes())
                with partial.open('ab' if offset else 'wb') as handle:
                    for chunk in response.iter_raw(256 * 1024):
                        control.budget()
                        handle.write(chunk)
                        handle.flush()
                        received += len(chunk)
                        done = offset + received
                        if (total and done > total) or (max_bytes and done > max_bytes):
                            raise RuntimeError('Source exceeded the permitted content size')
                        control.progress('download', done, total)
                        with db.transaction() as conn:
                            conn.execute('UPDATE videos SET bytes_downloaded=?,size_bytes=?,updated_at=? WHERE id=(SELECT video_id FROM jobs WHERE id=?)', (done, total, utcnow(), job_id))
                        # Pace after accounting for exactly the received bytes.  The
                        # throttle checks cancellation every 50ms while sleeping.
                        throttle.consume(len(chunk), control.check)
                        if video_id and done - last_probe > 2 * 1024 * 1024:
                            _early_playback(partial, total, control, video_id)
                            last_probe = done
                if expected is not None and received != expected:
                    raise RuntimeError('Source ended before its declared content length; retry resumes verified bytes')
                if total is not None and partial.stat().st_size != total:
                    raise RuntimeError('Source transfer is incomplete; retry resumes verified bytes')
                for _ in range(30):
                    try:
                        partial.replace(target)
                        break
                    except PermissionError:
                        time.sleep(.1)
                else:
                    raise RuntimeError('Source cache is busy; retry publication after closing playback')
                sidecar.unlink(missing_ok=True)
                return
            finally:
                if response is not None:
                    response.close()
    raise RuntimeError('Source identity changed while resuming; retry from the beginning')


def _download_ytdlp(url, target, control):
    # Provider subprocess deliberately gets no cookies, shell, or arbitrary options.
    validate_url(url, load_config().allow_private_sources)
    command = [sys.executable, '-m', 'yt_dlp', '--no-playlist', '--continue', '--no-overwrites',
               '--newline', '--progress', '--progress-template', 'download:dt:%(progress.downloaded_bytes)s:%(progress.total_bytes)s',
               '--merge-output-format', 'mkv', '--no-mtime', '-o', str(target.parent / 'download.%(ext)s'), '--', url]

    def progress(line):
        match = re.fullmatch(r'dt:(\d+):(\d+|NA)', line.strip())
        if match:
            done, total = int(match[1]), int(match[2]) if match[2].isdigit() else None
            control.progress('download', done, total)
    run(command, timeout=6 * 3600, control=control, on_output=progress)
    files = [p for p in target.parent.glob('download.*') if p.is_file() and p.suffix not in ('.part', '.ytdl', '.temp')]
    if len(files) != 1:
        raise RuntimeError('Downloader did not produce one complete media file')
    files[0].replace(target)


def audio_tracks(probe):
    return preferred_audio(probe)


def _quoted(value):
    return str(value).replace('"', '').replace('\r', '').replace('\n', '')


def _hls(source, asset_dir, video_id, probe=None, control=None, on_ready=None):
    probe = probe or ffprobe(source, control)
    tracks = audio_tracks(probe)
    root = asset_dir / 'hls'
    if root.exists():
        shutil.rmtree(root)
    root.mkdir()
    video_stream = next(s for s in probe.get('streams', []) if s.get('codec_type') == 'video')
    codec = video_stream.get('codec_name', '')
    if codec not in ('h264', 'hevc', 'av1', 'vp9'):
        raise RuntimeError(f'Original video codec {codec or "unknown"} cannot be packaged for browser playback. Video conversion is disabled; the original file is kept for download.')
    width = max(1, int(video_stream.get('width') or 640))
    height = max(1, int(video_stream.get('height') or 360))
    rate = max(64000, int(video_stream.get('bit_rate') or probe.get('format', {}).get('bit_rate') or 5000000))
    command = ['ffmpeg', '-hide_banner', '-loglevel', 'warning', '-y', '-progress', 'pipe:1', '-nostats',
               '-threads', str(media_threads()), '-i', str(source)]
    hls = ['-f', 'hls', '-hls_time', '4', '-hls_playlist_type', 'event', '-hls_flags', 'temp_file']
    # Copy the original compressed video: no resize, bitrate change, or video encoder.
    command += ['-map', '0:v:0', '-an', '-c:v', 'copy']
    if codec == 'h264':
        command += [*hls, '-hls_segment_filename', str(root / 'video-%05d.ts'), str(root / 'video.m3u8')]
    else:
        if codec == 'hevc':
            command += ['-tag:v', 'hvc1']
        command += [*hls, '-hls_segment_type', 'fmp4', '-hls_fmp4_init_filename', 'video-init.mp4',
                    '-hls_segment_filename', str(root / 'video-%05d.m4s'), str(root / 'video.m3u8')]
    for track in tracks:
        n = track['index']
        command += ['-map', f'0:a:{track.get("source_index", n)}', '-vn', '-c:a', 'aac', '-ac', '2', '-b:a', '160k',
                    *hls, '-hls_segment_filename', str(root / f'audio-{n}-%05d.ts'), str(root / f'audio-{n}.m3u8')]
    master = ['#EXTM3U', '#EXT-X-VERSION:' + ('3' if codec == 'h264' else '7')]
    for track in tracks:
        n = track['index']
        master.append(f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="{_quoted(track["label"])} ({n+1})",LANGUAGE="{_quoted(track["language"])}",DEFAULT={"YES" if track["default"] else "NO"},AUTOSELECT=YES,URI="audio-{n}.m3u8"')
    master += [f'#EXT-X-STREAM-INF:BANDWIDTH={rate + 400000},RESOLUTION={width}x{height}' + (',AUDIO="audio"' if tracks else ''), 'video.m3u8', '']
    announced = False

    def publish():
        nonlocal announced
        if announced:
            return
        playlists = [root / 'video.m3u8', *[root / f'audio-{t["index"]}.m3u8' for t in tracks]]
        if all(p.is_file() and '#EXTINF:' in p.read_text() for p in playlists):
            temporary = root / 'master.m3u8.tmp'
            temporary.write_text('\n'.join(master))
            temporary.replace(root / 'master.m3u8')
            announced = True
            if on_ready:
                on_ready(tracks, f'/api/media/{video_id}/assets/hls/master.m3u8')
    run(command, timeout=12 * 3600, control=control, tick=publish,
        on_output=ffmpeg_progress(control, 'package', float(probe.get('format', {}).get('duration') or 0)))
    publish()
    if not announced:
        raise RuntimeError('FFmpeg did not produce playable video and audio segments')
    return tracks, f'/api/media/{video_id}/assets/hls/master.m3u8'


def _stamp(seconds):
    millis = max(0, round(seconds * 1000))
    hours, remainder = divmod(millis, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    seconds, millis = divmod(remainder, 1000)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}'


def _make_card_assets(source, asset_dir, probe, control=None):
    duration = float(probe.get('format', {}).get('duration') or 0)
    if not math.isfinite(duration) or duration <= 0:
        return
    count = min(60, max(1, math.ceil(duration / 10)))
    interval = duration / count
    columns, rows = min(10, count), math.ceil(count / min(10, count))
    temporary = asset_dir / 'previews-tmp'
    temporary.mkdir(exist_ok=True)
    try:
        scale = 'scale=160:90:force_original_aspect_ratio=decrease,pad=160:90:(ow-iw)/2:(oh-ih)/2'
        run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(source), '-vf', f'fps=1/{interval:.6f},{scale},tile={columns}x{rows}:nb_frames={count}:padding=0', '-frames:v', '1', str(temporary / 'sprites.jpg')], 300, control)
        if control:
            control.progress('previews', 1, 3, unit='items')
        run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-ss', str(min(duration * .1, max(0, duration - 6))), '-i', str(source), '-t', str(min(6, duration)), '-an', '-vf', 'scale=480:-2', '-c:v', 'libx264', '-threads', str(media_threads()), '-preset', 'veryfast', '-movflags', '+faststart', str(temporary / 'preview.mp4')], 300, control)
        if control:
            control.progress('previews', 2, 3, unit='items')
        run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(source), '-vf', 'scale=640:-2', '-frames:v', '1', str(temporary / 'poster.jpg')], 120, control)
        if control:
            control.progress('previews', 3, 3, unit='items')
        lines = ['WEBVTT', '']
        for i in range(count):
            lines += [f'{_stamp(i * interval)} --> {_stamp(min(duration, (i + 1) * interval))}',
                      f'sprites.jpg#xywh={(i % columns)*160},{(i // columns)*90},160,90', '']
        (temporary / 'sprites.vtt').write_text('\n'.join(lines))
        for file in temporary.iterdir():
            file.replace(asset_dir / file.name)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _subtitles(source, root, probe, video_id, control=None):
    tracks = []
    for ordinal, stream in enumerate(s for s in probe.get('streams', []) if s.get('codec_type') == 'subtitle'):
        if not english_subtitle(stream) or stream.get('codec_name') not in ('subrip', 'ass', 'ssa', 'mov_text', 'webvtt', 'text'):
            continue
        name = f'subtitle-{ordinal}.vtt'
        temporary = root / (name + '.tmp')
        run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(source), '-map', f'0:s:{ordinal}', '-c:s', 'webvtt', '-f', 'webvtt', str(temporary)], 180, control)
        content = temporary.read_text(encoding='utf-8', errors='replace')
        if '-->' not in content:
            temporary.unlink(missing_ok=True)
            continue
        temporary.replace(root / name)
        tags = stream.get('tags') or {}
        tracks.append({'id': str(ordinal), 'label': tags.get('title') or tags.get('language', f'Subtitle {ordinal+1}'),
                       'language': tags.get('language', 'und'), 'url': f'/api/media/{video_id}/assets/{name}'})
    return tracks


def _caption_state(db, video_id, state, error=None):
    with db.transaction() as conn:
        conn.execute('UPDATE videos SET caption_status=?,caption_error=?,updated_at=? WHERE id=?',
                     (state, error, utcnow(), video_id))


def _merge_subtitles(db, video_id, tracks):
    with db.transaction() as conn:
        existing = json.loads(conn.execute('SELECT subtitle_tracks FROM videos WHERE id=?', (video_id,)).fetchone()[0])
        combined = {track.get('id', track['url']): track for track in existing}
        combined.update({track.get('id', track['url']): track for track in tracks})
        conn.execute('UPDATE videos SET subtitle_tracks=? WHERE id=?', (json.dumps(list(combined.values())), video_id))
        return list(combined.values())


def _auto_captions(source, root, probe, video_id, control):
    with control.db.connect() as conn:
        row = conn.execute('SELECT subtitle_tracks FROM videos WHERE id=?', (video_id,)).fetchone()
    if json.loads(row[0]):
        _caption_state(control.db, video_id, 'ready')
        return
    streams = [stream for stream in probe.get('streams', []) if stream.get('codec_type') == 'audio']
    if not streams or os.environ.get('DT_AUTO_CAPTIONS', 'false').lower() != 'true':
        _caption_state(control.db, video_id, 'unavailable', 'No audio stream' if not streams else 'Automatic captions are disabled')
        return
    from .caption_runner import MODELS
    model = os.environ.get('DT_CAPTION_MODEL', 'small')
    if model not in MODELS:
        _caption_state(control.db, video_id, 'failed', 'Unsupported local caption model; choose tiny, base, small, medium or large-v3')
        return
    audio_index = next((index for index, stream in enumerate(streams) if stream.get('disposition', {}).get('default')), 0)
    audio = root / 'caption-audio.wav'
    target = root / 'automatic-captions.vtt'
    _caption_state(control.db, video_id, 'generating')
    control.progress('caption_audio')
    try:
        run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-threads', str(media_threads()),
             '-i', str(source), '-map', f'0:a:{audio_index}', '-vn', '-ac', '1', '-ar', '16000', str(audio)],
            timeout=3600, control=control)

        def progress(line):
            if not line.startswith('dtcaption:'):
                return
            try:
                value = json.loads(line[len('dtcaption:'):])
                phase = value['phase']
                if phase not in ('caption_model', 'captions'):
                    return
                control.progress(phase, value.get('seconds', 0), value.get('duration'), unit='media_seconds')
            except (ValueError, TypeError, KeyError):
                pass
        run([sys.executable, '-m', 'drivetheater.caption_runner', str(audio), str(target),
             '--model', model, '--models', str(control.data / 'models'), '--threads', str(media_threads())],
            timeout=24 * 3600, control=control, on_output=progress)
        metadata = json.loads(target.with_suffix('.json').read_text())
        _merge_subtitles(control.db, video_id, [{
            'id': 'automatic', 'label': 'English — automatic captions/translation (check accuracy)',
            'language': metadata.get('language', 'und'),
            'url': f'/api/media/{video_id}/assets/{target.name}',
        }])
        _caption_state(control.db, video_id, 'ready')
    except Cancelled:
        _caption_state(control.db, video_id, 'pending')
        raise
    except Exception:
        _caption_state(control.db, video_id, 'failed', 'Local transcription did not finish. Check speech, model access and worker dependencies; you can upload English SRT/VTT instead.')
    finally:
        audio.unlink(missing_ok=True)


def _source_sha256(source, control):
    total = source.stat().st_size
    digest, done = hashlib.sha256(), 0
    control.progress('hashing', 0, total)
    with source.open('rb') as handle:
        while chunk := handle.read(1024 * 1024):
            control.check()
            digest.update(chunk)
            done += len(chunk)
            control.progress('hashing', done, total)
    return digest.hexdigest()


def _deduplicate_source(db, video_id, source, digest):
    """Record a byte-identical source and atomically share its immutable bytes."""
    with db.transaction() as conn:
        match = conn.execute(
            "SELECT id,local_path,duplicate_of FROM videos WHERE id!=? AND content_sha256=? "
            "AND local_path IS NOT NULL ORDER BY duplicate_of IS NOT NULL, updated_at DESC LIMIT 1",
            (video_id, digest),
        ).fetchone()
        canonical = (match['duplicate_of'] or match['id']) if match else None
        conn.execute('UPDATE videos SET content_sha256=?,duplicate_of=?,updated_at=? WHERE id=?',
                     (digest, canonical, utcnow(), video_id))
    if not match:
        return
    candidate = Path(match['local_path'])
    temporary = source.with_name(source.name + '.dedupe')
    try:
        if candidate.resolve() == source.resolve() or not candidate.is_file() or candidate.stat().st_size != source.stat().st_size:
            return
        os.link(candidate, temporary)
        # replace is atomic; if it fails, the independently downloaded source remains.
        temporary.replace(source)
    except OSError:
        temporary.unlink(missing_ok=True)


def _transient_download_error(exc):
    if isinstance(exc, PermissionError):
        return False
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, OSError)):
        return True
    if not isinstance(exc, RuntimeError):
        return False
    message = str(exc).lower()
    return ('http 429' in message or re.search(r'http 5\\d\\d', message) is not None
            or 'timed out' in message or 'incomplete' in message or 'ended before' in message)


def _download_with_retry(operation, control, db, job_id, maximum=3):
    for attempt in range(1, maximum + 1):
        try:
            with db.transaction() as conn:
                conn.execute('UPDATE jobs SET attempt=?,updated_at=? WHERE id=?', (attempt, utcnow(), job_id))
            return operation()
        except Cancelled:
            raise
        except Exception as exc:
            if attempt >= maximum or not _transient_download_error(exc):
                raise
            # Small, bounded exponential backoff.  Check in slices so cancellation
            # never waits for a retry timer to expire.
            deadline = time.monotonic() + 2 ** (attempt - 1)
            while time.monotonic() < deadline:
                control.check()
                time.sleep(min(.1, deadline - time.monotonic()))


def process_job(db, data_dir, job_id):
    control = Control(db, job_id, data_dir)
    with db.connect() as conn:
        job = conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        video = conn.execute('SELECT * FROM videos WHERE id=?', (job['video_id'],)).fetchone()
    root = data_dir / 'cache' / video['id']
    root.mkdir(parents=True, exist_ok=True)
    source = root / 'source.bin'
    try:
        automatic_cleanup(db, data_dir, exclude_video_id=video['id'])
        control.budget()
        control.progress('download', state='downloading')
        if not source.is_file():
            def download():
                if video['source_kind'] == 'yt-dlp':
                    _download_ytdlp(video['source_url'], source, control)
                elif video['source_kind'] in ('mega', 'gofile', 'torrent', 'google_public'):
                    download_external(video['source_kind'], video['source_url'], source, control, run,
                                      lambda url, path, ctl: _download_http(url, path, job_id, db, control=ctl,
                                          allow_private=load_config().allow_private_sources, max_bytes=16 * 1024 * 1024))
                else:
                    headers = {}
                    url = video['source_url']
                    if video['source_kind'] == 'google_drive':
                        with db.connect() as conn:
                            source_row = conn.execute('SELECT * FROM sources WHERE id=?', (video['source_id'],)).fetchone()
                        if not source_row:
                            raise RuntimeError('Drive connection is unavailable; reconnect the source')
                        if source_row['auth_mode'] == 'public_link':
                            download_external('google_public', f'https://drive.google.com/file/d/{video["remote_id"]}/view', source,
                                              control, run, None)
                            url = None
                        else:
                            path = Path(source_row['credential_path'])
                            adapter = GoogleAdapter(source_row['auth_mode'], json.loads(path.read_text()), credential_path=path)
                            url, headers = adapter.download_parameters(video['remote_id'])
                    if url:
                        _download_http(url, source, job_id, db, headers, control, video['id'], load_config().allow_private_sources)
            _download_with_retry(download, control, db, job_id)
        digest = _source_sha256(source, control)
        _deduplicate_source(db, video['id'], source, digest)
        control.progress('probe', state='preparing')
        probe = ffprobe(source, control)
        record_probe(db, video['id'], probe)
        if not any(s.get('codec_type') == 'video' for s in probe.get('streams', [])):
            raise RuntimeError('The downloaded source does not contain a video stream')
        _metadata(db, video['id'], probe)
        with db.transaction() as conn:
            conn.execute('UPDATE videos SET local_path=?,bytes_downloaded=?,size_bytes=?,asset_dir=?,updated_at=? WHERE id=?',
                         (str(source), source.stat().st_size, source.stat().st_size, str(root), utcnow(), video['id']))

        def playable(tracks, url, mode='hls'):
            with db.transaction() as conn:
                conn.execute("UPDATE videos SET state='playable',playback_mode=?,playback_url=?,audio_tracks=?,error=NULL,updated_at=? WHERE id=?", (mode, url, json.dumps(tracks), utcnow(), video['id']))
        video_height = max((int(s.get('height') or 0) for s in probe.get('streams', []) if s.get('codec_type') == 'video'), default=0)
        if _faststart_playable(source, probe) and video_height <= 480:
            playable(audio_tracks(probe), f'/api/media/{video["id"]}/stream', 'direct')
        else:
            control.progress('package')
            with ENCODERS:
                _hls(source, root, video['id'], probe, control, playable)
        control.check()
        _caption_state(db, video['id'], 'extracting')
        control.progress('subtitles')
        try:
            with ENCODERS:
                subtitles = _subtitles(source, root, probe, video['id'], control)
            _merge_subtitles(db, video['id'], subtitles)
        except Cancelled:
            raise
        except RuntimeError:
            pass
        control.progress('previews', 0, 3, unit='items')
        try:
            with ENCODERS:
                _make_card_assets(source, root, probe, control)
        except Cancelled:
            raise
        except RuntimeError:
            pass  # Optional derivative failure does not revoke valid playback.
        with ENCODERS:
            _auto_captions(source, root, probe, video['id'], control)
        index_existing_captions(db, data_dir / 'cache', video['id'])
        with db.transaction() as conn:
            conn.execute("UPDATE jobs SET state='completed',phase='complete',progress=100,progress_known=1,error=NULL,updated_at=? WHERE id=?", (utcnow(), job_id))
            conn.execute("UPDATE videos SET state='cached',error=NULL,updated_at=? WHERE id=?", (utcnow(), video['id']))
            emit_event(conn, f'job:{job_id}:completed', 'job_completed', 'Media is ready', 'A requested media job completed.', entity_id=video['id'])
        finish_job(db, job_id, 'completed')
    except Cancelled:
        with db.transaction() as conn:
            conn.execute("UPDATE jobs SET state='cancelled',error=NULL,updated_at=? WHERE id=?", (utcnow(), job_id))
            conn.execute("UPDATE videos SET state='cancelled',playback_mode=NULL,playback_url=NULL,updated_at=? WHERE id=?", (utcnow(), video['id']))
            emit_event(conn, f'job:{job_id}:cancelled', 'job_cancelled', 'Media job cancelled', 'A requested media job was cancelled.', entity_id=video['id'])
        finish_job(db, job_id, 'cancelled', {'error_code': 'cancelled'})
    except Exception as exc:
        # Raw provider/FFmpeg errors can contain signed URLs, credentials and paths.
        error_code = 'unknown'
        message = 'Media processing failed; check source health, disk capacity and codec diagnostics, then retry'
        if isinstance(exc, RuntimeError):
            if 'headroom' in str(exc) or 'budget reached' in str(exc):
                error_code = 'disk_full'
                message = 'Storage budget reached; review inactive cached media or increase capacity'
            elif 'does not contain a video stream' in str(exc):
                error_code = 'invalid_media'
                message = 'The downloaded source does not contain a video stream'
            elif 'execution deadline' in str(exc):
                error_code = 'timeout'
                message = 'Media processing exceeded its execution deadline; inspect the failed stage before retrying'
        with db.transaction() as conn:
            conn.execute("UPDATE jobs SET state='failed',error=?,updated_at=? WHERE id=?", (message, utcnow(), job_id))
            conn.execute("UPDATE videos SET state='failed',error=?,playback_mode=NULL,playback_url=NULL,updated_at=? WHERE id=?", (message, utcnow(), video['id']))
            emit_event(conn, f'job:{job_id}:failed', 'job_failed', 'Media job needs attention', message, entity_id=video['id'])
        finish_job(db, job_id, 'failed', {'error_code': error_code})


def claim(db):
    with db.transaction() as conn:
        settings = {row['key']: row['value'] for row in conn.execute(
            "SELECT key,value FROM settings WHERE key IN ('download_schedule_enabled','download_start_hour','download_end_hour')"
        )}
        rows = conn.execute(
            "SELECT id,interactive FROM jobs WHERE state='queued' AND cancel_requested=0 "
            "ORDER BY interactive DESC,updated_at LIMIT 32"
        ).fetchall()
        for row in rows:
            if not allowed_now(settings, bool(row['interactive'])):
                continue
            changed = conn.execute(
                "UPDATE jobs SET state='downloading',claimed_at=?,updated_at=? WHERE id=? AND state='queued' AND cancel_requested=0",
                (int(time.time()), utcnow(), row['id']),
            ).rowcount
            if changed:
                return row['id']
        return None


@contextlib.contextmanager
def exclusive_worker(data_dir):
    global WORKER_LOCK_FD
    handle = (data_dir / 'worker.lock').open('a+b')
    try:
        if os.name == 'posix':
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Children retain the same lock description: a crashed parent cannot
            # be replaced while one of its media subprocesses is still writing.
            WORKER_LOCK_FD = handle.fileno()
        else:
            import msvcrt
            handle.write(b'0'); handle.flush(); handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        yield
    finally:
        WORKER_LOCK_FD = None
        handle.close()


def _feature_tick(db, cfg):
    """Runs outside the heartbeat thread; only explicitly enabled policies/channels act."""
    storage_maintenance(db, cfg.data_dir)
    return process_outbox(db, cfg)


def main():
    global ENCODERS
    cfg = load_config()
    db = Database(cfg.database_path)
    db.initialize()
    for name in ('SIGTERM', 'SIGINT'):
        signal.signal(getattr(signal, name), lambda *_: STOP.set())
    with exclusive_worker(cfg.data_dir), concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool, concurrent.futures.ThreadPoolExecutor(max_workers=1) as features:
        with db.transaction() as conn:
            # Exclusive worker identity permits startup recovery, not time-based stealing
            # of a still-running job by a second worker process.
            conn.execute("UPDATE jobs SET state='queued',claimed_at=NULL WHERE state IN ('downloading','preparing')")
        futures = {}
        feature_future, last_features = None, 0.0
        while not STOP.is_set():
            if time.monotonic() - last_features >= 30 and (feature_future is None or feature_future.done()):
                if feature_future is not None:
                    with contextlib.suppress(Exception):
                        feature_future.result()
                feature_future = features.submit(_feature_tick, db, cfg)
                last_features = time.monotonic()
            with db.transaction() as conn:
                conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('worker_heartbeat',?)", (str(int(time.time())),))
                conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('worker_cpu',?)", (json.dumps({'effective_cpus': media_cpus(), 'cgroup_cpus': cpu_limit()}),))
                conn.execute("UPDATE jobs SET claimed_at=? WHERE state IN ('downloading','preparing')", (int(time.time()),))
                settings = dict(conn.execute("SELECT key,value FROM settings WHERE key IN ('download_concurrency','max_transcodes')"))
            futures = {job: future for job, future in futures.items() if not future.done()}
            if not futures:
                ENCODERS = threading.BoundedSemaphore(min(2, max(1, int(settings.get('max_transcodes', '1')))))
                os.environ['DT_ENCODER_SLOTS'] = str(min(2, max(1, int(settings.get('max_transcodes', '1')))))
            while len(futures) < min(2, max(1, int(settings.get('download_concurrency', '1')))):
                job = claim(db)
                if not job:
                    break
                futures[job] = pool.submit(process_job, db, cfg.data_dir, job)
            STOP.wait(.5)


if __name__ == '__main__':
    main()
