"""Source selection without contacting providers or downloading media."""
from __future__ import annotations

import base64
import hashlib
import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from .sources import validate_url

VIDEO_SUFFIXES = ('.mp4', '.mkv', '.webm', '.mov', '.m4v', '.avi', '.ts', '.m2ts', '.wmv', '.flv', '.mpg', '.mpeg')


def _valid_percent_encoding(value: str) -> bool:
    return not re.search(r'%(?![0-9A-Fa-f]{2})', value)


def _canonical_btih(value: str) -> str | None:
    """Return the canonical lowercase hexadecimal v1 info hash, if valid."""
    if re.fullmatch(r'[0-9a-fA-F]{40}', value):
        return value.lower()
    if not re.fullmatch(r'[A-Z2-7a-z]{32}', value):
        return None
    try:
        decoded = base64.b32decode(value.upper(), casefold=True)
    except (ValueError, base64.binascii.Error):
        return None
    return decoded.hex() if len(decoded) == 20 else None


def _magnet_hash(value: str) -> str | None:
    parsed = urlparse(value)
    if parsed.scheme.lower() != 'magnet' or parsed.netloc or parsed.path or parsed.params or parsed.fragment:
        return None
    if not parsed.query or not _valid_percent_encoding(parsed.query):
        return None
    for item in parse_qs(parsed.query, keep_blank_values=True).get('xt', []):
        prefix, separator, info_hash = item.partition(':')
        if prefix.lower() == 'urn' and separator:
            namespace, separator, encoded_hash = info_hash.partition(':')
            if namespace.lower() == 'btih' and separator:
                canonical = _canonical_btih(encoded_hash)
                if canonical:
                    return canonical
    return None


def classify_source(value: str, allow_private: bool = False) -> tuple[str, str]:
    value = value.strip()
    if len(value) > 16384:
        raise ValueError('Source link is too long')
    if any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError('Source link is malformed')
    if value.lower().startswith('magnet:'):
        if not _magnet_hash(value):
            raise ValueError('Magnet link needs a valid BitTorrent v1 info hash')
        return 'torrent', value
    if not _valid_percent_encoding(value):
        raise ValueError('Source link is malformed')
    value = validate_url(value, allow_private)
    parsed = urlparse(value)
    host = (parsed.hostname or '').lower()
    path = parsed.path.lower()
    if path.endswith('.torrent'):
        return 'torrent', value
    if host in ('mega.nz', 'mega.co.nz', 'www.mega.nz'):
        if '/folder/' in path or parsed.fragment.startswith('F!'):
            raise ValueError('Add a MEGA file link, not a folder link')
        return 'mega', value
    if host == 'gofile.io' or host.endswith('.gofile.io'):
        return ('http' if path.endswith(VIDEO_SUFFIXES) else 'gofile'), value
    if host in ('drive.google.com', 'docs.google.com'):
        if '/folders/' in path:
            raise ValueError('Connect Drive folders on the Sources page')
        return 'google_public', value
    if path.endswith(VIDEO_SUFFIXES):
        return 'http', value
    # Extractor-backed sites and generic host pages are handled inside the
    # worker, not fetched while registering/browsing the library.
    return 'yt-dlp', value


def source_identity(kind: str, url: str) -> str:
    """Stable identity without discarding authorization or signed URL parameters."""
    parsed = urlparse(url)
    if kind == 'torrent' and parsed.scheme.lower() == 'magnet':
        info_hash = _magnet_hash(url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        selected_file = query.get('so', ['largest'])[0]
        if not isinstance(selected_file, str) or not re.fullmatch(r'\d{1,6}', selected_file):
            selected_file = 'largest'
        return 'btih:' + (info_hash or '') + ':file:' + selected_file
    if kind == 'google_public':
        match = re.search(r'/d/([\w-]+)', parsed.path)
        remote_id = match[1] if match else parse_qs(parsed.query).get('id', [''])[0]
        if remote_id:
            return 'drive:' + remote_id
    # Fragments may carry MEGA decryption keys; retain them. URL query ordering
    # can be significant for signatures, so do not blindly strip/sort it.
    canonical = urlunparse(parsed._replace(scheme=parsed.scheme.lower(), netloc=parsed.netloc.lower()))
    return hashlib.sha256((kind + ':' + canonical).encode()).hexdigest()


def torrent_files(payload: bytes) -> list[tuple[int, str, int]]:
    """Bounded bencode reader for safe largest-video selection, not an executor."""
    if len(payload) > 16 * 1024 * 1024:
        raise ValueError('Torrent metadata exceeds 16 MiB')
    offset = 0
    nodes = 0

    def parse(depth=0):
        nonlocal offset, nodes
        nodes += 1
        if depth > 40 or nodes > 200000 or offset >= len(payload):
            raise ValueError('Torrent metadata is invalid or too deeply nested')
        char = payload[offset:offset + 1]
        if char == b'i':
            end = payload.index(b'e', offset + 1)
            value = int(payload[offset + 1:end])
            offset = end + 1
            return value
        if char in (b'l', b'd'):
            offset += 1
            values = []
            while offset < len(payload) and payload[offset:offset + 1] != b'e':
                values.append(parse(depth + 1))
            if offset >= len(payload):
                raise ValueError('Truncated torrent metadata')
            offset += 1
            if char == b'l':
                return values
            if len(values) % 2:
                raise ValueError('Invalid torrent dictionary')
            return dict(zip(values[::2], values[1::2]))
        end = payload.index(b':', offset)
        length = int(payload[offset:end])
        if length < 0 or end + 1 + length > len(payload):
            raise ValueError('Invalid torrent string length')
        offset = end + 1 + length
        return payload[end + 1:offset]

    def component(raw):
        if not isinstance(raw, bytes) or not raw or len(raw) > 255:
            raise ValueError('Invalid torrent path')
        text = raw.decode('utf-8', errors='replace')
        if not text or text in ('.', '..') or any(char in text for char in ('/', '\\', ':', '\x00')):
            raise ValueError('Unsafe torrent path')
        return text

    try:
        body = parse()
        if offset != len(payload) or not isinstance(body, dict):
            raise ValueError('Invalid torrent metadata')
        info = body[b'info']
        if not isinstance(info, dict):
            raise ValueError('Invalid torrent metadata')
        name = component(info.get(b'name.utf-8', info.get(b'name')))
        result = []
        if b'files' in info:
            files = info[b'files']
            if not isinstance(files, list) or len(files) > 100000:
                raise ValueError('Invalid torrent file list')
            for index, entry in enumerate(files, 1):
                if not isinstance(entry, dict):
                    raise ValueError('Invalid torrent file entry')
                parts = entry.get(b'path.utf-8', entry.get(b'path'))
                if not isinstance(parts, list) or not parts or len(parts) > 64:
                    raise ValueError('Torrent entry is missing its path')
                path = '/'.join([name, *[component(part) for part in parts]])
                if len(path) > 4096:
                    raise ValueError('Torrent path is too long')
                size = entry[b'length']
                if not isinstance(size, int) or size < 0 or size > 2 ** 63 - 1:
                    raise ValueError('Invalid torrent file size')
                result.append((index, path, size))
        else:
            size = info[b'length']
            if not isinstance(size, int) or size < 0 or size > 2 ** 63 - 1:
                raise ValueError('Invalid torrent file size')
            result.append((1, name, size))
        return result
    except (KeyError, IndexError, TypeError, OverflowError) as exc:
        raise ValueError('Invalid torrent metadata') from exc
