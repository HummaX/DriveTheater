"""Optional Stremio/Torrentio-compatible torrent discovery, not a media scraper."""
from __future__ import annotations

import json
import re
from typing import Literal
from urllib.parse import quote, urlencode, urljoin, urlparse, urlunparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .auth import session_user
from .sources import validate_url

router = APIRouter()
_MAX_JSON_BYTES = 2 * 1024 * 1024
_MAX_REDIRECTS = 5
_DEFAULT_METADATA_MANIFEST = 'https://v3-cinemeta.strem.io/manifest.json'
_MEDIA_ID_RE = re.compile(r'^tt\d{4,12}$')
_EPISODE_ID_RE = re.compile(r'^tt\d{4,12}:\d{1,4}:\d{1,4}$')


class Lookup(BaseModel):
    manifest_url: str | None = Field(default=None, min_length=10, max_length=4096)
    media_type: Literal['movie', 'series'] = 'movie'
    media_id: str = Field(pattern=r'^tt\d{4,12}(?::\d{1,4}:\d{1,4})?$')


class EpisodeLookup(BaseModel):
    media_id: str = Field(pattern=r'^tt\d{4,12}$')
    metadata_manifest_url: str | None = Field(default=None, min_length=10, max_length=4096)


def _manifest_endpoint(manifest_url: str, endpoint: str) -> str:
    parsed = urlparse(manifest_url.strip())
    if not parsed.path.endswith('/manifest.json'):
        raise ValueError('Use the add-on manifest URL ending in /manifest.json')
    path = parsed.path[:-len('/manifest.json')] + endpoint
    return urlunparse(parsed._replace(path=path, fragment=''))


def stream_endpoint(manifest_url: str, media_type: str, media_id: str) -> str:
    return _manifest_endpoint(manifest_url, '/stream/' + media_type + '/' + quote(media_id, safe=':') + '.json')


def metadata_endpoint(manifest_url: str, media_id: str) -> str:
    return _manifest_endpoint(manifest_url, '/meta/series/' + quote(media_id, safe='') + '.json')


def bounded_json_fetch(url: str, request: Request) -> dict:
    """Fetch a public add-on document with per-hop URL validation and a size cap."""
    with httpx.Client(timeout=25, follow_redirects=False, trust_env=False) as client:
        for redirects in range(_MAX_REDIRECTS):
            endpoint = validate_url(url, request.app.state.config.allow_private_sources)
            with client.stream('GET', endpoint, headers={'Accept': 'application/json'}) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get('location')
                    if not location or redirects == _MAX_REDIRECTS - 1:
                        raise ValueError('Add-on redirect could not be followed')
                    url = urljoin(endpoint, location)
                    continue
                response.raise_for_status()
                chunks = bytearray()
                for chunk in response.iter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > _MAX_JSON_BYTES:
                        raise ValueError('Add-on response is too large')
                body = json.loads(chunks)
                if not isinstance(body, dict):
                    raise ValueError('Add-on returned invalid JSON')
                return body
    raise ValueError('Add-on lookup could not finish')


def torrent_choices(body: dict) -> dict:
    choices = []
    unsupported = 0
    seen = set()
    for stream in body.get('streams', [])[:300]:
        if not isinstance(stream, dict):
            continue
        info_hash = str(stream.get('infoHash') or '')
        file_index = stream.get('fileIdx')
        if not re.fullmatch(r'[0-9a-fA-F]{40}', info_hash) or (file_index is not None and (type(file_index) is not int or file_index < 0 or file_index > 100000)):
            unsupported += 1
            continue
        key = (info_hash.lower(), file_index)
        if key in seen:
            continue
        seen.add(key)
        title = ' '.join(str(stream.get('title') or stream.get('description') or 'Torrent stream').split())[:300]
        name = ' '.join(str(stream.get('name') or 'Torrent').split())[:100]
        query = {'xt': 'urn:btih:' + info_hash.lower(), 'dn': title}
        if file_index is not None:
            query['so'] = str(file_index)
        choices.append({'name': name, 'title': title, 'magnet_url': 'magnet:?' + urlencode(query)})
    return {'items': choices, 'unsupported_count': unsupported}


def episode_metadata(body: dict, media_id: str) -> dict:
    meta = body.get('meta')
    if not isinstance(meta, dict) or meta.get('id') != media_id:
        raise ValueError('Add-on returned invalid series metadata')
    videos = meta.get('videos')
    if not isinstance(videos, list):
        raise ValueError('Add-on returned invalid episode metadata')

    episodes = []
    seen = set()
    for video in videos[:10000]:
        if not isinstance(video, dict):
            continue
        episode_id = video.get('id')
        season = video.get('season')
        episode = video.get('episode')
        if (
            not isinstance(episode_id, str)
            or not _EPISODE_ID_RE.fullmatch(episode_id)
            or not episode_id.startswith(media_id + ':')
            or type(season) is not int
            or type(episode) is not int
            or not 1 <= season <= 10000
            or not 1 <= episode <= 10000
            or episode_id != f'{media_id}:{season}:{episode}'
        ):
            continue
        if episode_id in seen:
            continue
        seen.add(episode_id)
        title = video.get('title')
        episodes.append({
            'id': episode_id,
            'title': title if isinstance(title, str) and title.strip() else f'S{season:02d}E{episode:02d}',
            'season': season,
            'episode': episode,
            'thumbnail': video.get('thumbnail') if isinstance(video.get('thumbnail'), str) else None,
            'released': video.get('released') if isinstance(video.get('released'), str) else None,
        })
    title = meta.get('name')
    poster = meta.get('poster')
    return {
        'id': media_id,
        'title': title if isinstance(title, str) and title.strip() else media_id,
        'poster': poster if isinstance(poster, str) else None,
        'episodes': episodes,
    }


@router.post('/api/torrentio/streams')
def streams(payload: Lookup, request: Request):
    if not session_user(request.app.state.db, request.cookies.get('dt_session')):
        raise HTTPException(401, 'authentication required')
    try:
        manifest = payload.manifest_url or saved_addons(request).get('torrentio_manifest_url')
        if not manifest:
            raise ValueError('No stream add-on configured')
        body = bounded_json_fetch(stream_endpoint(manifest, payload.media_type, payload.media_id), request)
        if not isinstance(body.get('streams', []), list):
            raise ValueError('Add-on returned an invalid stream list')
        return torrent_choices(body)
    except (httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError):
        # Configured add-on paths can contain account/API keys. Never echo them.
        raise HTTPException(400, 'Add-on lookup failed. Check the manifest URL, media ID and provider availability.') from None


@router.post('/api/torrentio/episodes')
def episodes(payload: EpisodeLookup, request: Request):
    """Return series metadata only after an authenticated user explicitly requests it."""
    if not session_user(request.app.state.db, request.cookies.get('dt_session')):
        raise HTTPException(401, 'authentication required')
    try:
        manifest_url = payload.metadata_manifest_url or _DEFAULT_METADATA_MANIFEST
        return episode_metadata(bounded_json_fetch(metadata_endpoint(manifest_url, payload.media_id), request), payload.media_id)
    except (httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError):
        raise HTTPException(400, 'Episode lookup failed. Check the metadata manifest URL, media ID and provider availability.') from None


class DiscoveryConfig(BaseModel):
    torrentio_manifest_url: str | None = Field(default=None, max_length=4096)
    metadata_manifest_url: str | None = Field(default=None, max_length=4096)


class CatalogLookup(BaseModel):
    query: str = Field(default='', max_length=120)
    skip: int = Field(default=0, ge=0, le=10000)


def saved_addons(request: Request) -> dict:
    with request.app.state.db.connect() as conn:
        return dict(conn.execute("SELECT key,value FROM settings WHERE key IN ('torrentio_manifest_url','metadata_manifest_url')"))


def require_user(request: Request):
    if not session_user(request.app.state.db, request.cookies.get('dt_session')):
        raise HTTPException(401, 'authentication required')


@router.get('/api/discover/config')
def discovery_config(request: Request):
    require_user(request)
    values = saved_addons(request)
    return {'torrentio_configured': bool(values.get('torrentio_manifest_url')),
            'metadata_configured': bool(values.get('metadata_manifest_url'))}


@router.post('/api/discover/config')
def save_discovery_config(payload: DiscoveryConfig, request: Request):
    require_user(request)
    updates = {}
    try:
        for key, value in payload.model_dump(exclude_none=True).items():
            value = value.strip()
            if value:
                _manifest_endpoint(value, '/manifest.json')
                validate_url(value, request.app.state.config.allow_private_sources)
            updates[key] = value
    except ValueError:
        raise HTTPException(400, 'Use a valid, permitted add-on URL ending in /manifest.json.') from None
    with request.app.state.db.transaction() as conn:
        for key, value in updates.items():
            conn.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))
    return discovery_config(request)


def catalog_movies(body: dict) -> list:
    metas = body.get('metas')
    if not isinstance(metas, list):
        raise ValueError('Invalid catalog')
    items, seen = [], set()
    for meta in metas[:300]:
        if not isinstance(meta, dict):
            continue
        movie_id = meta.get('id')
        if not isinstance(movie_id, str) or not _MEDIA_ID_RE.fullmatch(movie_id) or movie_id in seen:
            continue
        if meta.get('type', 'movie') != 'movie':
            continue
        seen.add(movie_id)
        poster = meta.get('poster')
        poster = poster if isinstance(poster, str) and urlparse(poster).scheme in ('https', 'http') else None
        rating = str(meta.get('imdbRating') or '')
        try:
            rating = rating if 0 <= float(rating) <= 10 else ''
        except ValueError:
            rating = ''
        items.append({'id': movie_id, 'title': str(meta.get('name') or movie_id)[:240], 'poster': poster,
                      'year': str(meta.get('releaseInfo') or '')[:40], 'rating': rating,
                      'description': str(meta.get('description') or '')[:2000],
                      'genres': [str(value)[:40] for value in meta.get('genres', [])[:5]] if isinstance(meta.get('genres'), list) else []})
    return items


@router.post('/api/discover/catalog')
def discover_catalog(payload: CatalogLookup, request: Request):
    require_user(request)
    manifest = saved_addons(request).get('metadata_manifest_url') or _DEFAULT_METADATA_MANIFEST
    extra = []
    if payload.query.strip():
        extra.append('search=' + quote(payload.query.strip(), safe=''))
    if payload.skip:
        extra.append('skip=' + str(payload.skip))
    suffix = ('/' + '&'.join(extra)) if extra else ''
    try:
        endpoint = _manifest_endpoint(manifest, '/catalog/movie/top' + suffix + '.json')
        return {'items': catalog_movies(bounded_json_fetch(endpoint, request)), 'skip': payload.skip}
    except (httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError):
        raise HTTPException(400, 'Movie catalog could not be loaded. Check the metadata add-on and try again.') from None
