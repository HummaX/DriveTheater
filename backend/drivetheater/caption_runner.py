"""Isolated local speech transcription. No media is sent to a cloud API."""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path


MODELS = {'tiny', 'base', 'small', 'medium', 'large-v3'}


def stamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, rest = divmod(milliseconds, 3600000)
    minutes, rest = divmod(rest, 60000)
    seconds, milliseconds = divmod(rest, 1000)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('audio', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--model', choices=sorted(MODELS), default='small')
    parser.add_argument('--models', type=Path, required=True)
    parser.add_argument('--threads', type=int, default=1)
    args = parser.parse_args()
    from faster_whisper import WhisperModel

    def progress(**values):
        print('dtcaption:' + json.dumps(values), flush=True)

    progress(phase='caption_model')
    model = WhisperModel(args.model, device='cpu', compute_type='int8',
                         cpu_threads=max(1, args.threads), num_workers=1,
                         download_root=str(args.models))
    progress(phase='captions')
    segments, info = model.transcribe(str(args.audio), beam_size=5, vad_filter=True, task='translate')
    count = 0
    temporary = args.output.with_suffix('.vtt.tmp')
    with temporary.open('w', encoding='utf-8') as output:
        output.write('WEBVTT\n\n')
        for segment in segments:
            text = ' '.join(segment.text.split()).replace('-->', '→')
            if text and segment.end > segment.start:
                count += 1
                output.write(f'{stamp(segment.start)} --> {stamp(segment.end)}\n{html.escape(text)}\n\n')
            progress(phase='captions', seconds=float(segment.end), duration=float(info.duration))
    if count == 0:
        temporary.unlink(missing_ok=True)
        raise RuntimeError('No speech was detected')
    temporary.replace(args.output)
    args.output.with_suffix('.json').write_text(json.dumps({'language': 'en', 'source_language': info.language, 'cues': count}), encoding='utf-8')
    progress(phase='captions', seconds=float(info.duration), duration=float(info.duration))


if __name__ == '__main__':
    main()
