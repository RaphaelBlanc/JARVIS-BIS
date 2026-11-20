"""Regressions for security review #46, items 1-4. No production configuration."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

from fastapi.testclient import TestClient
import pytest

from cloud.app import create_app
from cloud.body_limit import BodyRejected, DavBodyLimit, bounded_dav_body
from cloud.storage import CloudError, Settings, Store

ROOT = Path(__file__).resolve().parents[1]
HELPER_SPEC = importlib.util.spec_from_file_location('cloud_security_helpers', ROOT / 'tests/test_cloud.py')
HELPERS = importlib.util.module_from_spec(HELPER_SPEC)
HELPER_SPEC.loader.exec_module(HELPERS)
intent, prepared, service = HELPERS.intent, HELPERS.prepared, HELPERS.service
sys.path.insert(0, str(ROOT / 'scripts'))
from env_files import EnvFile, isolate_runner, sync_components

SPEC = importlib.util.spec_from_file_location('cloud_control_security', ROOT / 'scripts/cloud-control.py')
CONTROL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTROL)


@pytest.mark.parametrize('method', ['GET', 'HEAD', 'OPTIONS', 'PROPFIND', 'DELETE', 'MOVE', 'COPY', 'MKCOL', 'LOCK', 'UNLOCK', 'PROPPATCH', 'PUT', 'PATCH'])
def test_dav_limit_before_basic_auth_for_every_method(service, method):
    _, client, _ = service
    client.headers.pop('Authorization')
    response = client.request(method, '/cloud/dav/', content=b'x' * 8193)
    assert response.status_code == 413
    streamed = client.request(method, '/cloud/dav/', content=iter([b'x' * 4096, b'x' * 4096, b'x']))
    assert streamed.status_code == 413


@pytest.mark.parametrize('length', [None, b'1', b'8193'])
def test_raw_asgi_body_never_reaches_wsgi_when_too_large(length):
    called, reads, statuses = [], [], []
    frames = iter([b'x' * 4096, b'x' * 4096, b'x', b'unread'])
    async def inner(*args):
        called.append(True)
    async def receive():
        reads.append(True)
        return {'type': 'http.request', 'body': next(frames), 'more_body': True}
    async def send(message):
        if message['type'] == 'http.response.start':
            statuses.append(message['status'])
    scope = dict(type='http', method='PROPFIND', path='/cloud/dav/', headers=[] if length is None else [(b'content-length', length)])
    asyncio.run(DavBodyLimit(inner)(scope, receive, send))
    assert statuses == [413] and not called
    assert len(reads) == (0 if length == b'8193' else 3)


def test_dav_body_deadline():
    async def receive():
        await asyncio.sleep(1)
    with pytest.raises(BodyRejected) as caught:
        asyncio.run(bounded_dav_body({'headers': []}, receive, timeout=.01))
    assert caught.value.status == 408


@pytest.mark.parametrize('terminal', ['commit', 'cancel', 'expire', 'revoke'])
def test_parts_reclaimed_and_receipts_retained(service, terminal):
    store, _, device = service
    ident = device['device_id']
    transfer = prepared(store, ident)
    assert sum(p.stat().st_size for p in store.parts.iterdir()) == 5
    if terminal == 'commit':
        result = store.commit(ident, transfer)
        assert store.commit(ident, transfer) == result
    elif terminal == 'cancel':
        store.cancel(ident, transfer)
    elif terminal == 'expire':
        with store.transaction() as db:
            db.execute('UPDATE uploads SET expires=0 WHERE id=?', (transfer,))
        store.collect_expired()
    else:
        store.revoke(ident)
    assert list(store.parts.iterdir()) == []
    with store.connection() as db:
        row = db.execute('SELECT * FROM uploads WHERE id=?', (transfer,)).fetchone()
        assert row is not None and row['cleanup_pending'] == 0
        assert db.execute('SELECT COUNT(*) FROM chunks WHERE upload=?', (transfer,)).fetchone()[0] == 0


def test_repeated_cancel_cannot_accumulate_physical_parts(service):
    store, _, device = service
    for number in range(25):
        transfer = prepared(store, device['device_id'])
        store.cancel(device['device_id'], transfer)
        assert list(store.parts.iterdir()) == []


def test_cleanup_failure_keeps_its_reservation(service, monkeypatch):
    store, _, device = service
    ident = device['device_id']
    data = b'x' * 700
    transfer = prepared(store, ident, data)
    unlink = Path.unlink
    def denied(path, *args, **kwargs):
        if path.parent == store.parts:
            raise PermissionError('simulated cleanup failure')
        return unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', denied)
        store.cancel(ident, transfer)
        assert store.status(ident)['reserved_bytes'] == 700
        with pytest.raises(CloudError, match='quota_exceeded'):
            store.create_upload(ident, intent(store, data), uuid.uuid4().hex)
    store.collect_expired()
    assert list(store.parts.iterdir()) == []
    assert store.status(ident)['reserved_bytes'] == 0


def test_cleanup_does_not_remove_parts_used_by_commit(service, monkeypatch):
    store, _, device = service
    ident = device['device_id']
    transfer = prepared(store, ident)
    entered, release = threading.Event(), threading.Event()
    original = store._commit
    def paused(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)
    monkeypatch.setattr(store, '_commit', paused)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(store.commit, ident, transfer)
        try:
            assert entered.wait(3)
            store.cancel(ident, transfer)
            store.collect_expired()
            assert list(store.parts.iterdir())  # assembler owns its per-upload lease
        finally:
            release.set()
        with pytest.raises(CloudError, match='upload_not_pending'):
            future.result(timeout=3)
    assert list(store.parts.iterdir()) == []
    assert store.entries()['entries'] == []


def test_lifespan_reaps_expired_parts_without_client_activity(tmp_path, monkeypatch):
    import cloud.app as module
    monkeypatch.setenv('DEPLOY_MODE', 'server')
    monkeypatch.setenv('CLOUD_ENABLED', '1')
    monkeypatch.setattr(module, 'PART_CLEANUP_INTERVAL', .02)
    app = create_app(Settings(tmp_path, chunk_size=4), 'x' * 32)
    store = app.state.store
    device = store.associate('PC')['device_id']
    with TestClient(app):
        transfer = prepared(store, device)
        with store.transaction() as db:
            db.execute('UPDATE uploads SET expires=0 WHERE id=?', (transfer,))
        deadline = time.monotonic() + 2
        while list(store.parts.iterdir()) and time.monotonic() < deadline:
            time.sleep(.01)
        assert list(store.parts.iterdir()) == []


def test_v1_migration_preserves_identity_and_reclaims_old_parts(service):
    store, _, device = service
    transfer = prepared(store, device['device_id'])
    with store.transaction() as db:
        db.execute("UPDATE uploads SET state='committed',result='{}' WHERE id=?", (transfer,))
        db.execute('ALTER TABLE uploads DROP COLUMN cleanup_pending')
        db.execute('PRAGMA user_version=1')
    restored = Store(store.settings)
    restored.collect_expired()
    assert restored.server_id == store.server_id
    assert restored.authenticate(device['token']) == device['device_id']
    assert list(restored.parts.iterdir()) == []
    with restored.connection() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 3
        assert db.execute('SELECT state FROM uploads WHERE id=?', (transfer,)).fetchone()[0] == 'committed'


@pytest.fixture
def configuration(tmp_path, monkeypatch):
    for name in ('DEPLOY_MODE', 'CLOUD_INTERNAL_KEY', 'CLOUD_INTERNAL_URL'):
        monkeypatch.delenv(name, raising=False)
    for path in ('gateway', 'home-runner', 'deploy'):
        (tmp_path / path).mkdir()
    return tmp_path


def test_configure_preserves_comments_accents_multiline_and_custom_url(configuration):
    root = configuration
    original = '# café\r\nDEPLOY_MODE=server # garder\r\nMESSAGE="été # données" # fin\r\nMULTI="a\r\nb"\r\n'
    (root / '.env').write_bytes(original.encode())
    gateway = 'export DEPLOY_MODE=server\r\nCLOUD_ENABLED=0  # commentaire\r\nCLOUD_INTERNAL_URL="http://127.0.0.1:9912" # tunnel\r\nOTHER=été # fin\r\n'
    (root / 'gateway/.env').write_bytes(gateway.encode())
    assert not CONTROL.configure(root)
    assert (root / '.env').read_bytes() == original.encode()
    updated = (root / 'gateway/.env').read_bytes().decode()
    assert 'CLOUD_ENABLED="1"  # commentaire\r\n' in updated
    assert 'CLOUD_INTERNAL_URL="http://127.0.0.1:9912" # tunnel\r\n' in updated
    assert 'OTHER=été # fin\r\n' in updated
    assert '\\u00' not in updated
    assert EnvFile(root / 'gateway/.env').values['OTHER'] == 'été'
    assert 'CLOUD_INTERNAL_KEY' not in EnvFile(root / '.env').values
    assert not (root / 'home-runner/.env').exists()
    before = {p: p.read_bytes() for p in root.rglob('.env')}
    before[root / 'deploy/cloud.env'] = (root / 'deploy/cloud.env').read_bytes()
    CONTROL.configure(root)
    assert all(p.read_bytes() == data for p, data in before.items())
    assert (root / 'gateway/.env').stat().st_mode & 0o077 == 0


def test_legacy_key_removed_from_runner_and_root_and_rotated(configuration):
    root = configuration
    old = 'old-test-key-' + 'x' * 40
    for name in ('.env', 'gateway/.env', 'home-runner/.env'):
        (root / name).write_text('DEPLOY_MODE=server\n# autre\nOTHER="été # valeur" # note\nexport CLOUD_INTERNAL_KEY="' + old + '" # ancien\n')
    (root / 'deploy/cloud.env').write_text('CLOUD_INTERNAL_KEY="' + old + '"\n')
    assert CONTROL.configure(root)
    key = EnvFile(root / 'gateway/.env').values['CLOUD_INTERNAL_KEY']
    assert key != old and key == EnvFile(root / 'deploy/cloud.env').values['CLOUD_INTERNAL_KEY']
    for name in ('.env', 'home-runner/.env'):
        text = (root / name).read_text()
        assert old not in text and 'CLOUD_INTERNAL_KEY' not in text
        assert '# autre\nOTHER="été # valeur" # note\n' in text


def test_env_synchronization_preserves_gateway_cloud_and_filters_runner(configuration):
    root = configuration
    (root / '.env').write_text('DEPLOY_MODE=server\nOTHER="été # texte"\nCLOUD_INTERNAL_KEY="' + 'x' * 40 + '"\n')
    (root / 'gateway/.env').write_text('# gateway\nCLOUD_INTERNAL_KEY="' + 'g' * 40 + '" # interne\nCLOUD_INTERNAL_URL=http://127.0.0.1:9912\nCLOUD_ENABLED=1\n')
    (root / 'home-runner/.env').write_text('# runner\nCLOUD_INTERNAL_KEY=legacy\nCLOUD_ENABLED=1\n')
    sync_components(root)
    assert not any(k.startswith('CLOUD_') for k in EnvFile(root / 'home-runner/.env').values)
    assert 'CLOUD_INTERNAL_KEY' not in EnvFile(root / '.env').values
    gateway = EnvFile(root / 'gateway/.env').values
    assert gateway['CLOUD_INTERNAL_KEY'] == 'g' * 40 and gateway['CLOUD_INTERNAL_URL'].endswith(':9912')
    assert EnvFile(root / 'home-runner/.env').values['OTHER'] == 'été # texte'
    assert '# gateway\n' in (root / 'gateway/.env').read_text()


def test_invalid_env_aborts_before_any_write(configuration):
    root = configuration
    (root / '.env').write_text('DEPLOY_MODE=server\n')
    (root / 'gateway/.env').write_text('BAD="unterminated\n')
    with pytest.raises(ValueError, match='Invalid dotenv syntax'):
        CONTROL.configure(root)
    assert (root / '.env').read_text() == 'DEPLOY_MODE=server\n'
    assert not (root / 'deploy/cloud.env').exists()


def test_deploy_start_cannot_pass_cloud_key_to_runner(configuration):
    root = configuration
    shutil.copyfile(ROOT / 'deploy.sh', root / 'deploy.sh')
    (root / 'scripts').mkdir()
    shutil.copyfile(ROOT / 'scripts/env_files.py', root / 'scripts/env_files.py')
    for component in ('gateway', 'home-runner'):
        binary = root / component / '.venv/bin'
        binary.mkdir(parents=True)
        (binary / 'python').symlink_to(sys.executable)
        launcher = binary / 'uvicorn'
        launcher.write_text('#!' + sys.executable + '\nimport os\nfrom pathlib import Path\n'
                            'from dotenv import load_dotenv\n'
                            'load_dotenv(Path(__file__).resolve().parents[3]/".env")\n'
                            'Path(__file__).resolve().parents[2].joinpath("saw-cloud-key").write_text(str("CLOUD_INTERNAL_KEY" in os.environ))\n')
        launcher.chmod(0o755)
    (root / '.env').write_text('DEPLOY_MODE=server\nCLOUD_INTERNAL_KEY=legacy-root\n')
    (root / 'home-runner/.env').write_text('CLOUD_INTERNAL_KEY=legacy-runner\n')
    result = subprocess.run(['bash', str(root / 'deploy.sh'), 'start'], capture_output=True, text=True,
                            env=dict(os.environ, CLOUD_INTERNAL_KEY='inherited-test-key'), timeout=10)
    assert result.returncode == 0, result.stderr
    assert (root / 'home-runner/saw-cloud-key').read_text() == 'False'
    assert (root / 'gateway/saw-cloud-key').read_text() == 'True'


@pytest.mark.skipif(importlib.util.find_spec('discord') is None or importlib.util.find_spec('telegram') is None,
                    reason='Gateway bot dependencies required for the full import regression')
@pytest.mark.parametrize('kind', ['valid', 'syntax', 'encoding', 'symlink', 'manifest'])
def test_gateway_reads_root_shared_and_gateway_cloud_configuration(configuration, kind):
    root = configuration
    for directory in ('core', 'gateway', 'cloud', 'scripts'):
        shutil.copytree(ROOT / directory, root / directory, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('.env', '__pycache__', '.venv', '*.sqlite3', '*.db'))
    (root / '.env').write_text('DEPLOY_MODE=server\nCLOUD_ENABLED=0\nSECRET_KEY=test-only\n')
    if kind == 'syntax':
        with (root / '.env').open('ab') as f:
            f.write(b'UNRELATED="unterminated\n')
    elif kind == 'encoding':
        with (root / '.env').open('ab') as f:
            f.write(b'UNRELATED=\xff\n')
    elif kind == 'symlink':
        (root / '.env').rename(root / 'linked.env')
        (root / '.env').symlink_to('linked.env')
    (root / 'gateway/.env').write_text('DEPLOY_MODE=server\nCLOUD_ENABLED=1\nCLOUD_INTERNAL_KEY=' + 'x' * 32 + '\nSECRET_KEY=test-only\n')
    if kind == 'manifest':
        (root / 'gateway/.env.jarvis-managed.json').write_text('{broken-json')
    env = {k: v for k, v in os.environ.items() if not k.startswith('CLOUD_') and k not in ('DEPLOY_MODE', 'SECRET_KEY')}
    env['PYTHONPATH'] = str(root) + os.pathsep + str(root / 'gateway')
    result = subprocess.run([sys.executable, '-c',
                             'import main; from cloud_proxy import configuration; '
                             'assert configuration()[1] is True; '
                             'assert configuration()[3] == "x" * 32'], cwd=root, env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
