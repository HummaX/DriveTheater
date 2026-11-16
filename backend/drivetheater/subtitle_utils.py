from __future__ import annotations

import re
from pathlib import Path

_MAX_CAPTION_BYTES = 5 * 1024 * 1024
_ALLOWED_LANGUAGE = "en"
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TIMESTAMP = re.compile(
    r"^(?P<start>(?:\d{2,}:)?\d{2}:\d{2})[,.](?P<start_ms>\d{3})"
    r"\s*-->\s*"
    r"(?P<end>(?:\d{2,}:)?\d{2}:\d{2})[,.](?P<end_ms>\d{3})"
    r"(?P<settings>(?:[ \t]+[^\r\n]*)?)$"
)


def validated_language(value: str | None) -> str:
    """Normalize the sole supported caption language."""
    language = (value or _ALLOWED_LANGUAGE).strip().lower()
    if language != _ALLOWED_LANGUAGE:
        raise ValueError("only English captions are supported")
    return language


def _timestamp_seconds(value: str, milliseconds: str) -> float:
    parts = [int(part) for part in value.split(":")]
    if len(parts) == 2:
        hours, minutes, seconds = 0, *parts
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:  # The regular expression makes this defensive rather than expected.
        raise ValueError("caption timestamp is invalid")
    if minutes > 59 or seconds > 59:
        raise ValueError("caption timestamp is invalid")
    return hours * 3600 + minutes * 60 + seconds + int(milliseconds) / 1000


def _normalized_timing(line: str, *, allow_srt_separator: bool) -> str:
    match = _TIMESTAMP.fullmatch(line.strip())
    if not match:
        raise ValueError("caption cue timing is invalid")
    if not allow_srt_separator and "," in line:
        raise ValueError("VTT timestamps must use a decimal point")
    start = _timestamp_seconds(match["start"], match["start_ms"])
    end = _timestamp_seconds(match["end"], match["end_ms"])
    if end <= start:
        raise ValueError("caption cue end must follow its start")
    return (
        f"{match['start']}.{match['start_ms']} --> "
        f"{match['end']}.{match['end_ms']}{match['settings']}"
    )


def _cue_blocks(body: str, *, allow_srt_separator: bool) -> list[str]:
    cues: list[str] = []
    for block in re.split(r"\n{2,}", body.strip()):
        lines = block.split("\n")
        if not lines:
            continue
        timing_index = 0
        if not _TIMESTAMP.fullmatch(lines[0].strip()):
            if len(lines) < 2 or not lines[0].strip() or not _TIMESTAMP.fullmatch(lines[1].strip()):
                raise ValueError("caption cue must contain a timing line")
            timing_index = 1
        timing = _normalized_timing(lines[timing_index], allow_srt_separator=allow_srt_separator)
        text_lines = lines[timing_index + 1:]
        if not text_lines or not any(line.strip() for line in text_lines):
            raise ValueError("caption cue must contain text")
        if any("\x00" in line for line in text_lines):
            raise ValueError("caption text contains an invalid character")
        cue = lines[:timing_index] + [timing] + text_lines
        cues.append("\n".join(cue))
    if not cues:
        raise ValueError("caption file contains no cues")
    return cues


def _safe_vtt_filename(filename: str | None) -> str:
    original = Path((filename or "captions.vtt").replace("\\", "/")).name
    suffix = Path(original).suffix.lower()
    if suffix not in {".srt", ".vtt"}:
        raise ValueError("caption file must be .srt or .vtt")
    stem = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(original).stem).strip(" .")
    return f"{stem or 'captions'}.vtt"


def normalize_caption(filename: str | None, data: bytes) -> tuple[str, bytes]:
    """Validate SRT/WebVTT input and emit normalized UTF-8 WebVTT bytes."""
    if len(data) > _MAX_CAPTION_BYTES:
        raise ValueError("caption file exceeds 5 MiB")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("caption file must be UTF-8") from exc
    if "\x00" in text:
        raise ValueError("caption file contains an invalid character")
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    output_name = _safe_vtt_filename(filename)
    if Path(filename or "captions.vtt").suffix.lower() == ".srt":
        cues = _cue_blocks(text, allow_srt_separator=True)
    else:
        header, separator, body = text.partition("\n")
        if not separator or not re.fullmatch(r"WEBVTT(?:[ \t].*)?", header):
            raise ValueError("VTT file must begin with WEBVTT")
        cues = _cue_blocks(body, allow_srt_separator=False)
    encoded = ("WEBVTT\n\n" + "\n\n".join(cues).rstrip() + "\n").encode("utf-8")
    if len(encoded) > _MAX_CAPTION_BYTES:
        raise ValueError("caption file exceeds 5 MiB")
    return output_name, encoded


def caption_path(cache_dir: Path, video_id: str, language: str, sequence: str) -> Path:
    if not all(_SAFE_COMPONENT.fullmatch(value) for value in (video_id, language, sequence)):
        raise ValueError("invalid caption path")
    root = (cache_dir / video_id / "captions").resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{language}-{sequence}.vtt"
    if root not in path.resolve().parents:
        raise ValueError("invalid caption path")
    return path
