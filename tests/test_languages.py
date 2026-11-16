"""Language selection and subtitle extraction contracts using synthetic inputs."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

from drivetheater.languages import language_code, preferred_audio


def audio(language, title='', default=False):
    return {
        'codec_type': 'audio',
        'tags': {'language': language, 'title': title},
        'disposition': {'default': default},
    }


class LanguageSelectionTests(unittest.TestCase):
    def test_normalizes_legacy_codes_to_en_and_hi(self):
        self.assertEqual(language_code('eng'), 'en')
        self.assertEqual(language_code('english-US'), 'en')
        self.assertEqual(language_code('hin'), 'hi')
        self.assertEqual(language_code('hi-IN'), 'hi')

    def test_keeps_english_hindi_and_original_source_indexes(self):
        tracks = preferred_audio({'streams': [
            audio('fra', 'French'), audio('eng', 'English'),
            audio('hin', 'Hindi'), audio('deu', 'German'),
        ]})
        self.assertEqual(
            [(track['language'], track['source_index'], track['default']) for track in tracks],
            [('en', 1, True), ('hi', 2, False)],
        )

    def test_excludes_french_and_german_unless_original_or_default_fallback(self):
        fallback = preferred_audio({'streams': [audio('fra', 'French', default=True), audio('deu', 'German')]})
        self.assertEqual([(track['language'], track['source_index']) for track in fallback], [('fra', 0)])
        self.assertIn('default source track', fallback[0]['label'])

        original = preferred_audio({'streams': [audio('deu', 'Original German'), audio('fra', 'French', default=True)]})
        self.assertEqual([(track['language'], track['source_index'], track['original']) for track in original], [('deu', 0, True)])


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg and ffprobe required')
class EnglishSubtitleExtractionTests(unittest.TestCase):
    def test_extracts_synthetic_english_srt_as_webvtt(self):
        from drivetheater import worker
        with tempfile.TemporaryDirectory(prefix='drivetheater-subtitles-') as tmp:
            root = Path(tmp)
            subtitle = root / 'english.srt'
            subtitle.write_text('1\n00:00:00,000 --> 00:00:01,000\nHello from synthetic captions.\n', encoding='utf-8')
            source = root / 'with-subtitle.mkv'
            subprocess.run([
                'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                '-f', 'lavfi', '-i', 'color=c=black:size=160x90:rate=12:duration=2',
                '-i', str(subtitle), '-map', '0:v', '-map', '1:s', '-c:v', 'libx264',
                '-pix_fmt', 'yuv420p', '-c:s', 'srt', '-metadata:s:s:0', 'language=eng', str(source),
            ], check=True, capture_output=True, timeout=30)
            tracks = worker._subtitles(source, root, worker.ffprobe(source), 'synthetic')
            self.assertEqual(len(tracks), 1)
            self.assertEqual(tracks[0]['language'], 'eng')
            target = root / 'subtitle-0.vtt'
            self.assertTrue(target.is_file())
            self.assertIn('WEBVTT', target.read_text(encoding='utf-8'))
            self.assertIn('Hello from synthetic captions.', target.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
