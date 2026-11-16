"""Explicit synthetic browser fixture: never uses Google or existing server data."""
import contextlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))


def main():
    with tempfile.TemporaryDirectory(prefix='drivetheater-browser-fixture-') as tmp:
        data = Path(tmp).resolve()
        os.environ.update(DT_DATA_DIR=str(data), DT_ADMIN_PASSWORD='fixture-password-only',
                          DT_SECRET='synthetic-browser-secret-not-a-real-deployment-12345',
                          DT_ALLOW_PRIVATE_SOURCES='true', DT_AUTO_CAPTIONS='false', DT_UI_DIR=str(ROOT / 'frontend' / 'dist'))
        from drivetheater.main import app
        from drivetheater.worker import process_job
        from drivetheater.db import utcnow
        from fastapi.responses import FileResponse
        db = app.state.db
        media = data / 'sample.mkv'
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=24:duration=16', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=16', '-f', 'lavfi', '-i', 'sine=frequency=880:duration=16', '-map', '0:v', '-map', '1:a', '-map', '2:a', '-c:v', 'libx264', '-threads', '2', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-metadata:s:a:0', 'language=eng', '-metadata:s:a:0', 'title=English', '-metadata:s:a:1', 'language=hin', '-metadata:s:a:1', 'title=Hindi', str(media)], check=True, timeout=60)
        with db.transaction() as conn:
            conn.execute('INSERT INTO videos(id,title,source_kind,remote_id,source_url,updated_at) VALUES (?,?,?,?,?,?)', ('synthetic-cached', 'Motion study — English and Hindi', 'http', 'synthetic-cached', 'http://127.0.0.1:4175/fixture/source.mkv', utcnow()))
            conn.execute("INSERT INTO jobs(id,video_id,title,state,phase,updated_at) VALUES ('synthetic-job','synthetic-cached','Motion study','queued','queued',?)", (utcnow(),))
            conn.execute('INSERT INTO videos(id,title,source_kind,remote_id,source_url,updated_at) VALUES (?,?,?,?,?,?)', ('synthetic-cloud', 'Cloud-only title — Play to download', 'http', 'synthetic-cloud', 'http://127.0.0.1:4175/fixture/source.mkv', utcnow()))
            for fixture_id, title in [('synthetic-episode-1', 'Test Series S01E01 First light'),
                                      ('synthetic-episode-2', 'Test Series S01E02 Second light'),
                                      ('synthetic-episode-3', 'Test Series S02E01 A new reel')]:
                conn.execute('INSERT INTO videos(id,title,source_kind,remote_id,source_url,updated_at) VALUES (?,?,?,?,?,?)',
                             (fixture_id, title, 'http', fixture_id, 'http://127.0.0.1:4175/fixture/source.mkv', utcnow()))
        folder = data / 'cache' / 'synthetic-cached'
        folder.mkdir(parents=True)
        import shutil
        shutil.copyfile(media, folder / 'source.bin')
        process_job(db, data, 'synthetic-job')
        from drivetheater import addons
        def synthetic_addon_document(url, request):
            if '/catalog/movie/' in url:
                return {'metas': [
                    {'id': 'tt9900001', 'type': 'movie', 'name': 'Midnight signal — synthetic', 'releaseInfo': '2026', 'imdbRating': '', 'poster': 'http://127.0.0.1:4175/fixture/posters/signal.svg', 'description': 'A synthetic catalog record for testing the Discover interface. No real movie or torrent is accessed.'},
                    {'id': 'tt9900002', 'type': 'movie', 'name': 'Orbital drift — synthetic', 'releaseInfo': '2026', 'poster': 'http://127.0.0.1:4175/fixture/posters/orbit.svg'},
                    {'id': 'tt9900003', 'type': 'movie', 'name': 'Glass horizon — synthetic', 'releaseInfo': '2026', 'poster': 'http://127.0.0.1:4175/fixture/posters/horizon.svg'},
                ]}
            if '/stream/movie/' in url:
                return {'streams': []}  # Never launch a real torrent from a preview.
            raise ValueError('Live add-on access is disabled in the synthetic preview')
        addons.bounded_json_fetch = synthetic_addon_document
        with db.transaction() as conn:
            conn.execute("INSERT INTO settings(key,value) VALUES ('torrentio_manifest_url','https://synthetic.invalid/manifest.json') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            conn.execute("UPDATE videos SET thumbnail_url='http://127.0.0.1:4175/fixture/posters/orbit.svg' WHERE id LIKE 'synthetic-episode-%'")
            conn.execute("UPDATE videos SET thumbnail_url='http://127.0.0.1:4175/fixture/posters/signal.svg' WHERE id='synthetic-cloud'")
        counts = {'source_requests': 0}
        @app.get('/fixture/source.mkv')
        def source():
            counts['source_requests'] += 1
            return FileResponse(media, media_type='video/x-matroska')
        @app.get('/fixture/posters/{name}.svg')
        def synthetic_poster(name: str):
            from fastapi import Response
            palettes = {'signal': ('#132c46', '#9366c9', '#ecb6b1', 'Midnight signal'),
                        'orbit': ('#0f253a', '#397c8b', '#91d7df', 'Orbital drift'),
                        'horizon': ('#15294a', '#6e66aa', '#c8b4ed', 'Glass horizon')}
            base, glow, light, label = palettes.get(name, palettes['signal'])
            svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="600" height="900" viewBox="0 0 600 900"><defs><linearGradient id="bg" x2=".8" y2="1"><stop stop-color="{base}"/><stop offset="1" stop-color="{glow}"/></linearGradient><radialGradient id="light"><stop stop-color="{light}" stop-opacity=".8"/><stop offset="1" stop-color="{glow}" stop-opacity="0"/></radialGradient></defs><path fill="url(#bg)" d="M0 0h600v900H0z"/><circle cx="390" cy="330" r="280" fill="url(#light)"/><circle cx="310" cy="330" r="128" fill="{base}" stroke="{light}" stroke-width="2"/><ellipse cx="310" cy="340" rx="245" ry="58" fill="none" stroke="{light}" stroke-opacity=".5" transform="rotate(-27 310 340)"/><path d="M0 670 160 560 285 650 430 520 600 645V900H0z" fill="{base}" opacity=".8"/><text x="42" y="760" fill="#edf3ff" font-family="Trebuchet MS,sans-serif" font-size="38">{label}</text><text x="44" y="805" fill="#cfdaef" font-family="Trebuchet MS,sans-serif" font-size="18">Synthetic preview artwork</text></svg>'''
            return Response(svg, media_type='image/svg+xml', headers={'Cache-Control': 'no-store'})
        @app.get('/fixture/counts')
        def requests():
            return counts
        # Keep the production SPA fallback last, after these test-only routes.
        app.router.routes.sort(key=lambda route: getattr(route, 'path', '') == '/{frontend_path:path}')
        environment = dict(os.environ, PYTHONPATH=str(ROOT / 'backend'))
        worker = subprocess.Popen([sys.executable, '-m', 'drivetheater.worker'], env=environment, cwd=ROOT)
        import uvicorn
        try:
            print('Synthetic browser fixture ready; no real provider or user data is used.', flush=True)
            uvicorn.run(app, host='127.0.0.1', port=4175, access_log=False)
        finally:
            worker.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                worker.wait(timeout=10)
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=5)


if __name__ == '__main__':
    main()
