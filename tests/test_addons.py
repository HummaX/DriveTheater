import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

from drivetheater import addons, providers


class AddonEpisodeTests(unittest.TestCase):
    def setUp(self):
        self.request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(db=object(), config=SimpleNamespace(allow_private_sources=False))),
            cookies={'dt_session': 'session'},
        )

    def test_episodes_use_default_metadata_manifest_and_filter_invalid_videos(self):
        body = {'meta': {
            'id': 'tt1234567', 'name': 'Example Show', 'poster': 'https://images.example/poster.jpg',
            'videos': [
                {'id': 'tt1234567:1:2', 'title': 'Second episode', 'season': 1, 'episode': 2,
                 'thumbnail': 'https://images.example/e2.jpg', 'released': '2026-01-01T00:00:00.000Z'},
                {'id': 'tt1234567:0:3', 'season': 0, 'episode': 3},
                {'id': 'tt9999999:1:3', 'season': 1, 'episode': 3},
                {'id': 'tt1234567:1:4', 'season': '1', 'episode': 4},
                {'id': 'tt1234567:1:5', 'season': 1, 'episode': 6},
            ],
        }}
        with patch.object(addons, 'require_principal', return_value=SimpleNamespace(username='fixture')), patch.object(addons, 'saved_addons', return_value={}), patch.object(addons, 'bounded_json_fetch', return_value=body) as fetch:
            result = addons.episodes(addons.EpisodeLookup(media_id='tt1234567'), self.request)
        self.assertEqual(fetch.call_args.args[0], 'https://v3-cinemeta.strem.io/meta/series/tt1234567.json')
        self.assertEqual(result, {
            'id': 'tt1234567', 'title': 'Example Show', 'poster': 'https://images.example/poster.jpg',
            'episodes': [{'id': 'tt1234567:1:2', 'title': 'Second episode', 'season': 1, 'episode': 2,
                          'thumbnail': 'https://images.example/e2.jpg', 'released': '2026-01-01T00:00:00.000Z'},
                         {'id': 'tt1234567:0:3', 'title': 'S00E03', 'season': 0, 'episode': 3, 'thumbnail': None, 'released': None}],
        })

    def test_episodes_rejects_metadata_id_mismatch_without_exposing_url(self):
        with patch.object(addons, 'require_principal', return_value=SimpleNamespace(username='fixture')), patch.object(addons, 'saved_addons', return_value={}), patch.object(
            addons, 'bounded_json_fetch', return_value={'meta': {'id': 'tt7654321', 'videos': []}}
        ):
            with self.assertRaises(addons.HTTPException) as caught:
                addons.episodes(addons.EpisodeLookup(media_id='tt1234567', metadata_manifest_url='https://provider.example/manifest.json'), self.request)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertNotIn('provider.example', caught.exception.detail)

    def test_bounded_json_fetch_validates_redirect_targets(self):
        class Response:
            def __init__(self, status_code, headers=None):
                self.status_code = status_code
                self.headers = headers or {}

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def stream(self, _method, url, **_kwargs):
                if url.endswith('/start'):
                    return Response(302, {'location': 'https://catalog.example/next'})
                raise AssertionError('redirect destination must be validated before request')

        with patch.object(addons.httpx, 'Client', return_value=Client()), patch.object(
            addons, 'validate_url', side_effect=['https://catalog.example/start', ValueError('blocked')]
        ) as validate:
            with self.assertRaises(ValueError):
                addons.bounded_json_fetch('https://catalog.example/start', self.request)
        self.assertEqual(validate.call_count, 2)

    def test_streams_and_metadata_endpoints_are_derived_from_manifest(self):
        manifest = 'https://provider.example/addon/manifest.json?token=secret'
        self.assertEqual(addons.stream_endpoint(manifest, 'series', 'tt1234567:1:2'),
                         'https://provider.example/addon/stream/series/tt1234567:1:2.json?token=secret')
        self.assertEqual(addons.metadata_endpoint(manifest, 'tt1234567'),
                         'https://provider.example/addon/meta/series/tt1234567.json?token=secret')


class SourceValidationTests(unittest.TestCase):
    def test_base32_magnet_identity_matches_hex_and_selected_file(self):
        hex_magnet = 'magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&so=7'
        base32_magnet = 'magnet:?xt=urn:btih:AERUKZ4JVPG66AJDIVTYTK6N54ASGRLH&so=7'
        self.assertEqual(providers.classify_source(base32_magnet), ('torrent', base32_magnet))
        self.assertEqual(providers.source_identity('torrent', base32_magnet), providers.source_identity('torrent', hex_magnet))
        self.assertEqual(providers.source_identity('torrent', base32_magnet), 'btih:0123456789abcdef0123456789abcdef01234567:file:7')

    def test_malformed_magnets_are_rejected_before_network_validation(self):
        for value in (
            'magnet:?xt=urn:btih:abc',
            'magnet://host?xt=urn:btih:0123456789abcdef0123456789abcdef01234567',
            'magnet:?xt=urn%3Abtih%3A0123456789abcdef0123456789abcdef01234567%ZZ',
        ):
            with self.assertRaises(ValueError):
                providers.classify_source(value)

    def test_torrent_metadata_rejects_unsafe_path_component(self):
        # {info: {name: safe, files: [{length: 1, path: [..]}]}}
        payload = b'd4:infod4:name4:safe5:filesld6:lengthi1e4:pathl2:..eeee'
        with self.assertRaises(ValueError):
            providers.torrent_files(payload)


if __name__ == '__main__':
    unittest.main(verbosity=2)
