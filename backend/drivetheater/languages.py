"""Explicit language preferences; never infer a film's original language."""
from __future__ import annotations

import re


def language_code(value: str | None) -> str:
    value = str(value or 'und').strip().lower().replace('_', '-').split('-')[0]
    return {'eng': 'en', 'english': 'en', 'hin': 'hi', 'hindi': 'hi'}.get(value, value)


def stream_language(stream: dict) -> str:
    tags = stream.get('tags') or {}
    language = language_code(tags.get('language'))
    if language == 'und':
        title = str(tags.get('title', ''))
        if re.search(r'\benglish\b', title, flags=re.I):
            return 'en'
        if re.search(r'\bhindi\b', title, flags=re.I):
            return 'hi'
    return language


def preferred_audio(probe: dict) -> list[dict]:
    streams = [stream for stream in probe.get('streams', []) if stream.get('codec_type') == 'audio']
    original = {index for index, stream in enumerate(streams)
                if re.search(r'\boriginal\b', str((stream.get('tags') or {}).get('title', '')), re.I)}
    fallback = None
    if streams and not original:
        fallback = next((index for index, stream in enumerate(streams) if stream.get('disposition', {}).get('default')), None)
        if fallback is None and not any(stream_language(stream) in ('en', 'hi') for stream in streams):
            fallback = 0
    chosen = []
    for index, stream in enumerate(streams):
        code = stream_language(stream)
        if code not in ('en', 'hi') and index not in original and index != fallback:
            continue
        title = str((stream.get('tags') or {}).get('title') or '').strip()
        label = title or {'en': 'English', 'hi': 'Hindi'}.get(code, code if code != 'und' else 'Unknown language')
        if index == fallback and code not in ('en', 'hi'):
            label += ' — default source track; original language unverified'
        chosen.append({'source_index': index, 'language': code, 'label': label,
                       'original': index in original, 'default': False})
    chosen.sort(key=lambda item: (0 if item['language'] == 'en' else 2 if item['language'] == 'hi' else 1, item['source_index']))
    for index, item in enumerate(chosen):
        item['index'] = index
        item['default'] = index == 0
    return chosen


def english_subtitle(stream: dict) -> bool:
    return stream_language(stream) == 'en'
