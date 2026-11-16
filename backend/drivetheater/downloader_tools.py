"""Optional download tools run only after an explicit Play/Download job."""
from __future__ import annotations

import json
from pathlib import Path
import secrets
import shutil
import socket
import sys
from urllib.parse import parse_qs, urlparse

import httpx

from .providers import VIDEO_SUFFIXES, torrent_files


def _torrent(url, target, control, run, http_download):
    if not shutil.which('aria2c'):
        raise RuntimeError('Torrent support needs aria2c; rebuild the application image')
    work = target.parent / 'torrent'
    work.mkdir(exist_ok=True)
    if url.lower().startswith('magnet:'):
        control.progress('torrent_metadata')
        run(['aria2c', '--no-conf=true', '--bt-metadata-only=true', '--bt-save-metadata=true',
             '--seed-time=0', '--file-allocation=none', '--dir=' + str(work), '--', url],
            timeout=600, control=control)
        metadata = list(work.glob('*.torrent'))
        if len(metadata) != 1:
            raise RuntimeError('Torrent metadata was not received; check peers and trackers')
        torrent = metadata[0]
    else:
        torrent = work / 'source.torrent'
        http_download(url, torrent, control)
    try:
        candidates = [entry for entry in torrent_files(torrent.read_bytes()) if entry[1].lower().endswith(VIDEO_SUFFIXES)]
    except ValueError as exc:
        raise RuntimeError('Invalid or unsafe torrent metadata') from exc
    if not candidates:
        raise RuntimeError('Torrent contains no supported video files')
    requested = parse_qs(urlparse(url).query).get('so', []) if url.lower().startswith('magnet:') else []
    if requested:
        if not requested[0].isdigit():
            raise RuntimeError('Selected torrent file index is invalid')
        wanted = int(requested[0]) + 1  # Stremio/BEP53 is zero-based; aria2 is one-based.
        selected_entries = [entry for entry in candidates if entry[0] == wanted]
        if not selected_entries:
            raise RuntimeError('The selected torrent entry is not a supported video')
        index, relative, expected = selected_entries[0]
    else:
        index, relative, expected = max(candidates, key=lambda entry: entry[2])
    selected = (work / relative).resolve()
    if work.resolve() not in selected.parents:
        raise RuntimeError('Torrent path is outside its cache')
    # Bind the RPC listener to loopback with a random per-job token. Peer traffic
    # is normal BitTorrent; torrents are not subject to HTTP-only SSRF guarantees.
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    secret = secrets.token_hex(24)
    control.progress('download')
    download_error = []
    with httpx.Client(timeout=1, trust_env=False) as rpc:
        def call(method, arguments):
            response = rpc.post(f'http://127.0.0.1:{port}/jsonrpc', json={
                'jsonrpc': '2.0', 'id': 'progress', 'method': method,
                'params': ['token:' + secret, *arguments],
            })
            return response.json().get('result')

        def tick():
            try:
                active = call('aria2.tellActive', [['completedLength', 'totalLength']]) or []
                for item in active:
                    total = int(item.get('totalLength') or 0)
                    if total:
                        control.progress('download', int(item.get('completedLength') or 0), total)
                if not active:
                    stopped = call('aria2.tellStopped', [0, 10, ['status']]) or []
                    if stopped:
                        if any(item.get('status') != 'complete' for item in stopped):
                            download_error.append(True)
                        call('aria2.shutdown', [])
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                pass
        run(['aria2c', '--no-conf=true', '--enable-rpc=true', '--rpc-listen-all=false',
             '--rpc-listen-port=' + str(port), '--rpc-secret=' + secret,
             '--seed-time=0', '--seed-ratio=0.0', '--bt-stop-timeout=120',
             '--file-allocation=none', '--continue=true', '--auto-file-renaming=false',
             '--allow-overwrite=true', '--select-file=' + str(index),
             '--dir=' + str(work), '--', str(torrent)], timeout=24 * 3600, control=control, tick=tick)
    if download_error or not selected.is_file() or selected.is_symlink() or selected.stat().st_size != expected:
        raise RuntimeError('Torrent did not finish the selected video')
    selected.replace(target)
    shutil.rmtree(work)


def download_external(kind, url, target, control, run, http_download):
    if kind == 'torrent':
        return _torrent(url, target, control, run, http_download)
    if kind == 'mega':
        if not shutil.which('megadl'):
            raise RuntimeError('MEGA file support needs megatools; rebuild the application image')
        work = target.parent / 'mega'
        work.mkdir(exist_ok=True)
        control.progress('download')
        run(['megadl', '--no-progress', '--path', str(work), '--', url], timeout=12 * 3600, control=control)
        files = [p for p in work.rglob('*') if p.is_file() and not p.is_symlink() and p.suffix != '.mega']
        if len(files) != 1:
            raise RuntimeError('Use one accessible MEGA file link, not an account or folder link')
        files[0].replace(target)
        shutil.rmtree(work)
        return
    if kind == 'gofile':
        work = target.parent / 'gofile'
        work.mkdir(exist_ok=True)
        # The maintained gallery-dl extractor handles Gofile's public/guest API.
        # Select one supported video instead of bulk-downloading a shared folder.
        expression = 'extension.lower() in ' + repr(tuple(suffix[1:] for suffix in VIDEO_SUFFIXES))
        control.progress('download')
        run([sys.executable, '-m', 'gallery_dl', '--ignore-config', '--no-input',
             '--directory', str(work), '--filename', 'video.{extension}',
             '--filter', expression, '--range', '1', '--', url], timeout=12 * 3600, control=control)
        files = [p for p in work.rglob('*') if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES and not p.is_symlink()]
        if len(files) != 1:
            raise RuntimeError('No public Gofile video was downloaded; protected links require a direct authorized file URL')
        files[0].replace(target)
        shutil.rmtree(work)
        return
    if kind == 'google_public':
        control.progress('download')
        # No cookies or account sessions are imported from the workstation.
        partial = target.with_suffix('.gdown.part')
        run([sys.executable, '-m', 'gdown', '--no-cookies', '--fuzzy', '-O', str(partial), '--', url],
            timeout=12 * 3600, control=control)
        if not partial.is_file():
            raise RuntimeError('Public Drive file could not be downloaded; check sharing and quota')
        partial.replace(target)
        return
    raise RuntimeError('Unsupported download provider')
