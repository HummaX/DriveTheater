"""Index only bounded, already-local English caption files; no provider requests."""
from pathlib import Path
import json

from .library import index_caption, remove_caption_index
from .media import safe_asset


def index_existing_captions(db, media_root: Path, video_id: str, track_id: str | None = None) -> int:
    with db.connect() as conn:
        row = conn.execute('SELECT asset_dir,subtitle_tracks FROM videos WHERE id=?', (video_id,)).fetchone()
    if not row or not row['asset_dir']:
        return 0
    root = Path(row['asset_dir'])
    try:
        root.resolve().relative_to(Path(media_root).resolve())
        tracks = json.loads(row['subtitle_tracks'] or '[]')
    except (ValueError, OSError):
        return 0
    if root.is_symlink() or not root.is_dir():
        return 0
    count = 0
    for track in tracks[:64]:
        identity = str(track.get('id', ''))
        language = str(track.get('language', '')).lower()
        if track_id is not None and identity != track_id:
            continue
        if not (language in ('en', 'eng', 'english') or language.startswith('en-')):
            continue
        prefix = f'/api/media/{video_id}/assets/'
        url = track.get('url', '')
        if not isinstance(url, str) or not url.startswith(prefix):
            continue
        try:
            path = safe_asset(root, url[len(prefix):])
            if path.is_symlink() or path.suffix.lower() != '.vtt' or path.stat().st_size > 524288:
                continue
            text = path.read_text(encoding='utf-8')
            remove_caption_index(db, video_id, identity)
            count += index_caption(db, video_id, identity, text)
        except (ValueError, OSError, UnicodeError, FileNotFoundError):
            continue
    return count
