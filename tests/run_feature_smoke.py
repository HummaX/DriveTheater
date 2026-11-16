"""Drive the actual HTTP server using disposable data and no provider/media tools."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = 'feature-fixture-password-only'


def serve(port: int):
    with tempfile.TemporaryDirectory(prefix='dt-feature-http-') as directory:
        os.environ.update(DT_DATA_DIR=directory, DT_ADMIN_USER='admin', DT_ADMIN_PASSWORD=PASSWORD,
                          DT_SECRET='synthetic-feature-http-secret-never-deploy-123456789',
                          DT_AUTO_CAPTIONS='false', DT_ALLOW_PRIVATE_SOURCES='false')
        os.environ.pop('DT_UI_DIR', None)
        sys.path.insert(0, str(ROOT / 'backend'))
        from drivetheater.main import app
        from drivetheater.db import utcnow
        from drivetheater import addons
        import uvicorn
        db, cfg = app.state.db, app.state.config
        original = cfg.media_dir / 'fixture' / 'source.bin'
        original.parent.mkdir(parents=True)
        original.write_bytes(b'0123456789abcdef' * 100)
        with db.transaction() as conn:
            conn.execute("INSERT INTO videos(id,title,source_kind,remote_id,state,local_path,asset_dir,playback_mode,playback_url,size_bytes,duration,updated_at) VALUES(?,?,'http',?,'cached',?,?,'direct',?,?,100,?)", ('fixture', 'Synthetic Series S00E01', 'fixture', str(original), str(original.parent), '/api/media/fixture/stream', original.stat().st_size, utcnow()))
        def no_provider(*_args, **_kwargs):
            raise AssertionError('Provider access is disabled in this HTTP smoke fixture')
        addons.bounded_json_fetch = no_provider
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', access_log=False, proxy_headers=False))
        @app.post('/api/__feature_fixture_stop')
        def stop():
            server.should_exit = True
            return {'stopping': True}
        app.router.routes.sort(key=lambda route: getattr(route, 'path', '') == '/{frontend_path:path}')
        server.run()


def smoke():
    import httpx
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0)); port = listener.getsockname()[1]
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--serve', str(port)], cwd=ROOT)
    checks = []
    with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=10) as client:
        try:
            for attempt in range(100):
                try:
                    if client.get('/api/health').status_code == 200: break
                except httpx.TransportError:
                    pass
                if process.poll() is not None: raise RuntimeError('Synthetic server exited before readiness')
                time.sleep(.1)
            else: raise RuntimeError('Synthetic server did not become ready')
            assert client.get('/api/security').status_code == 401
            assert client.post('/api/auth/login', json={'username': 'admin', 'password': PASSWORD}).status_code == 200
            library = client.get('/api/library', params={'query': 'S00E01'}).json()['items']
            assert len(library) == 1 and library[0]['season'] == 0
            collection = client.post('/api/library/collections', json={'name': 'HTTP smoke collection'}).json()
            assert client.post(f"/api/library/collections/{collection['id']}/items", json={'video_id': 'fixture'}).status_code == 200
            assert client.get('/api/jobs').json()['items'] == []
            checks.append('authenticated metadata, S00, collections; no media jobs')
            assert client.get('/api/notifications').status_code == 200
            assert client.get('/api/diagnostics/support-export').status_code == 200
            checks.append('durable inbox and allowlisted support route')
            preview = client.post('/api/storage/cleanup/preview', json={'video_ids': ['fixture']})
            assert preview.status_code == 200, preview.text
            plan = preview.json()['plan_id']
            original = client.get('/api/media/fixture/download', headers={'Range': 'bytes=0-15'})
            assert original.status_code == 206 and original.content == b'0123456789abcdef'
            confirmation = f'/api/storage/cleanup/{plan}/confirm'
            assert client.post(confirmation, json={'confirm': True}).status_code == 409
            assert client.post(confirmation, json={'confirm': True}).status_code == 409
            checks.append('ranged original protects bytes; stale cleanup token is one-use')
            secret = client.post('/api/security/mfa/begin', json={'password': PASSWORD}).json()['secret']
            sys.path.insert(0, str(ROOT / 'backend'))
            from drivetheater.security import totp
            enrollment = client.post('/api/security/mfa/confirm', json={'password': PASSWORD, 'code': totp(secret, int(time.time() // 30))})
            assert enrollment.status_code == 200, enrollment.text
            codes = enrollment.json()['recovery_codes']
            assert client.post('/api/auth/logout').status_code == 200
            login = client.post('/api/auth/login', json={'username': 'admin', 'password': PASSWORD})
            assert login.status_code == 202 and 'dt_session' not in login.cookies
            assert client.get('/api/library').status_code == 401
            assert client.post('/api/auth/mfa', json={'challenge': login.json()['challenge'], 'recovery_code': codes[0]}).status_code == 200
            sessions = client.get('/api/security/sessions').json()['items']
            assert all('token_hash' not in item and 'network_hash' not in item for item in sessions)
            current = next(item for item in sessions if item['current'])
            assert client.post('/api/security/sessions/revoke', json={'password': PASSWORD, 'recovery_code': codes[1], 'session_id': current['id']}).status_code == 200
            assert client.get('/api/notifications').status_code == 401
            checks.append('MFA password-only challenge, one-use recovery and immediate session revocation')
            print(json.dumps({'actual_http_smoke': 'passed', 'checks': checks, 'ui_build_verified': False, 'live_integrations_used': False}, indent=2))
        finally:
            with contextlib.suppress(httpx.HTTPError): client.post('/api/__feature_fixture_stop')
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate(); process.wait(timeout=5)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--serve': serve(int(sys.argv[2]))
    else: smoke()
