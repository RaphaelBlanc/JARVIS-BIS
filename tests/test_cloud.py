"""Cloud protocol and persistence tests; no gateway bots, production files or network."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import shutil
from pathlib import Path
import uuid

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
import pytest

from cloud.app import create_app
from cloud.storage import CloudError, Settings, Store

KEY = 'internal-test-key-' + 'x' * 32


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv('DEPLOY_MODE', 'server')
    monkeypatch.setenv('CLOUD_ENABLED', '1')
    app = create_app(Settings(tmp_path, quota=1024, max_file=1024, chunk_size=4), KEY)
    client = TestClient(app)
    client.headers['X-Cloud-Internal-Key'] = KEY
    device = app.state.store.associate('portable')
    client.headers['Authorization'] = 'Bearer ' + device['token']
    return app.state.store, client, device


def intent(store, data=b'hello', name='rapport.pdf', **extra):
    return dict(parent_id='root', parent_revision=store.entry('root')['revision'],
                name=name, size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                policy='create', expected_revision=None, **extra)


def prepared(store, device, data=b'hello', spec=None, idem=None):
    spec = spec or intent(store, data)
    result = store.create_upload(device, spec, idem or uuid.uuid4().hex)
    for index, offset in enumerate(range(0, len(data), store.settings.chunk_size)):
        chunk = data[offset:offset + store.settings.chunk_size]
        store.chunk(device, result['upload_id'], index, chunk, hashlib.sha256(chunk).hexdigest())
    return result['upload_id']


def test_server_and_internal_auth(service, monkeypatch):
    store, client, device = service
    assert client.get('/health').status_code == 200
    assert client.get('/health', headers={'X-Cloud-Internal-Key': 'wrong'}).status_code == 401
    monkeypatch.setenv('DEPLOY_MODE', 'local')
    assert client.get('/health').status_code == 403
    assert client.get('/api/cloud/v1/status').status_code == 403


def test_api_round_trip_and_ranges(service):
    store, client, device = service
    spec = intent(store, b'hello world', name='été 2026.txt')
    created = client.post('/api/cloud/v1/uploads', json=spec, headers={'Idempotency-Key': uuid.uuid4().hex})
    assert created.status_code == 200, created.text
    upload = created.json()['upload_id']
    for i, data in enumerate([b'hell', b'o wo', b'rld']):
        response = client.put(f'/api/cloud/v1/uploads/{upload}/chunks/{i}', content=data,
                              headers={'X-Content-SHA256': hashlib.sha256(data).hexdigest()})
        assert response.status_code == 200, response.text
    committed = client.post(f'/api/cloud/v1/uploads/{upload}/commit')
    assert committed.status_code == 200, committed.text
    entry = committed.json()['result']
    url = '/api/cloud/v1/files/' + entry['id'] + '/content'
    assert client.get(url).content == b'hello world'
    partial = client.get(url, headers={'Range': 'bytes=2-5'})
    assert partial.status_code == 206 and partial.content == b'llo '
    assert client.get(url, headers={'Range': 'bytes=-3'}).content == b'rld'
    assert client.get(url, headers={'Range': 'bytes=99-'}).status_code == 416
    assert client.get('/api/cloud/v1/entries').json()['entries'][0]['name'] == 'été 2026.txt'
    assert client.post(f'/api/cloud/v1/uploads/{upload}/commit').json() == committed.json()


def test_two_creations_atomic(service):
    store, _, first = service
    second = store.associate('tour')
    a = prepared(store, first['device_id'])
    b = prepared(store, second['device_id'])
    def publish(device, transfer):
        try:
            return store.commit(device, transfer)['state']
        except CloudError as exc:
            return exc.code
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(publish, first['device_id'], a), pool.submit(publish, second['device_id'], b)]
        results = [f.result() for f in futures]
    assert sorted(results) == ['committed', 'version_conflict']
    assert len(store.entries()['entries']) == 1


def test_two_replacements_preserve_old_version(service):
    store, _, device = service
    ident = device['device_id']
    original = store.commit(ident, prepared(store, ident))['result']
    spec = intent(store, b'world') | dict(policy='replace', expected_revision=original['revision'])
    a, b = prepared(store, ident, b'world', spec), prepared(store, ident, b'world', spec)
    store.commit(ident, a)
    with pytest.raises(CloudError, match='version_conflict'):
        store.commit(ident, b)
    with store.connection() as db:
        versions = db.execute('SELECT * FROM versions').fetchall()
    assert len(versions) == 1
    assert (store.blobs / versions[0]['blob']).read_bytes() == b'hello'


def test_keep_both_and_idempotent_result(service):
    store, _, device = service
    ident = device['device_id']
    store.commit(ident, prepared(store, ident))
    spec = intent(store) | dict(policy='keep_both')
    transfer = prepared(store, ident, spec=spec)
    result = store.commit(ident, transfer)
    assert result['result']['name'] == 'rapport (2).pdf'
    assert store.commit(ident, transfer) == result
    assert store.cancel(ident, transfer)['state'] == 'committed'


def test_restart_resume_incomplete_and_source_integrity(service):
    store, _, device = service
    ident = device['device_id']
    transfer = store.create_upload(ident, intent(store), uuid.uuid4().hex)['upload_id']
    store.chunk(ident, transfer, 0, b'hell', hashlib.sha256(b'hell').hexdigest())
    assert store.entries()['entries'] == []
    restarted = Store(store.settings)
    assert restarted.server_id == store.server_id
    assert len(restarted.upload_status(ident, transfer)['chunks']) == 1
    with pytest.raises(CloudError, match='incomplete_upload'):
        restarted.commit(ident, transfer)
    restarted.chunk(ident, transfer, 1, b'x', hashlib.sha256(b'x').hexdigest())
    with pytest.raises(CloudError, match='integrity_error'):
        restarted.commit(ident, transfer)
    assert restarted.entries()['entries'] == []


def test_revocation_and_cross_device_access(service):
    store, client, first = service
    second = store.associate('tour')
    transfer = prepared(store, first['device_id'])
    with pytest.raises(CloudError, match='not_found'):
        store.upload_status(second['device_id'], transfer)
    store.revoke(first['device_id'])
    assert client.get('/api/cloud/v1/status').status_code == 403
    with pytest.raises(CloudError, match='device_revoked'):
        store.commit(first['device_id'], transfer)


def test_idempotency_intent_cannot_change(service):
    store, _, device = service
    ident, key = device['device_id'], uuid.uuid4().hex
    result = store.create_upload(ident, intent(store), key)
    assert store.create_upload(ident, intent(store), key)['upload_id'] == result['upload_id']
    with pytest.raises(CloudError, match='idempotency_conflict'):
        store.create_upload(ident, intent(store, name='other'), key)


def test_reservations_cancel_and_expiry(service):
    store, _, device = service
    ident = device['device_id']
    spec = intent(store, b'x' * 700)
    transfer = store.create_upload(ident, spec, uuid.uuid4().hex)['upload_id']
    with pytest.raises(CloudError, match='quota_exceeded'):
        store.create_upload(ident, spec, uuid.uuid4().hex)
    store.cancel(ident, transfer)
    transfer = store.create_upload(ident, spec, uuid.uuid4().hex)['upload_id']
    with store.transaction() as db:
        db.execute('UPDATE uploads SET expires=0 WHERE id=?', (transfer,))
    assert store.upload_status(ident, transfer)['state'] == 'expired'
    with pytest.raises(CloudError, match='upload_expired'):
        store.commit(ident, transfer)
    assert store.create_upload(ident, spec, uuid.uuid4().hex)['state'] == 'pending'


@pytest.mark.parametrize('name', ['..', '.', '/etc/passwd', 'a\\b', 'a\x00b', 'a\nb'])
def test_invalid_names(service, name):
    store, _, device = service
    with pytest.raises(CloudError, match='invalid_name'):
        store.create_upload(device['device_id'], intent(store, name=name), uuid.uuid4().hex)


def test_directories_and_changed_destination(service):
    store, client, device = service
    response = client.post('/api/cloud/v1/directories', json=dict(parent_id='root',
                           parent_revision=store.entry('root')['revision'], name='Cours'))
    assert response.status_code == 200
    assert store.resolve('/Cours')['kind'] == 'directory'
    spec = intent(store) | dict(parent_revision='old')
    with pytest.raises(CloudError, match='destination_changed'):
        store.create_upload(device['device_id'], spec, uuid.uuid4().hex)


def test_dav_read_only_and_authentication(service):
    store, client, device = service
    entry = store.commit(device['device_id'], prepared(store, device['device_id']))['result']
    client.headers.pop('Authorization')
    auth = (device['device_id'], device['token'])
    assert client.get('/cloud/dav/rapport.pdf').status_code == 401
    assert client.get('/cloud/dav/rapport.pdf', auth=auth).content == b'hello'
    partial = client.get('/cloud/dav/rapport.pdf', auth=auth, headers={'Range': 'bytes=1-2'})
    assert partial.status_code == 206 and partial.content == b'el'
    listing = client.request('PROPFIND', '/cloud/dav/', auth=auth, headers={'Depth': '1'})
    assert listing.status_code == 207 and b'rapport.pdf' in listing.content
    assert b'/cloud/dav/' in listing.content  # GVfs must resolve hrefs under the actual mount path.
    for method in ['PUT', 'MOVE', 'COPY', 'DELETE', 'MKCOL', 'PROPPATCH']:
        assert client.request(method, '/cloud/dav/rapport.pdf', auth=auth, content=b'bad').status_code == 403
    assert store.entry(entry['id'])['revision'] == entry['revision']


def test_empty_file_and_cleanup(service):
    store, _, device = service
    result = store.commit(device['device_id'], prepared(store, device['device_id'], b''))
    assert result['result']['size'] == 0
    orphan = store.blobs / uuid.uuid4().hex
    orphan.write_bytes(b'orphan')
    store.maintenance()
    assert not orphan.exists()
    assert store.upload_status(device['device_id'], result['upload_id'])['state'] == 'committed'
    assert len(list(store.blobs.iterdir())) == 1


def test_restore_complete_snapshot_in_distinct_volume(service, tmp_path):
    store, _, device = service
    result = store.commit(device['device_id'], prepared(store, device['device_id']))
    snapshot = tmp_path / 'restored-volume'
    # The service is quiescent: copy DB, WAL if present, and every object together.
    shutil.copytree(store.root, snapshot, ignore=shutil.ignore_patterns('restored-volume'))
    restored = Store(Settings(snapshot, quota=1024, max_file=1024, chunk_size=4))
    assert restored.server_id == store.server_id
    assert restored.authenticate(device['token']) == device['device_id']
    assert restored.upload_status(device['device_id'], result['upload_id']) == result
    entry = restored.entry(result['result']['id'])
    assert (restored.blobs / entry['blob']).read_bytes() == b'hello'


def test_cancel_races_commit_without_deleting_published_file(service):
    store, _, device = service
    ident = device['device_id']
    transfer = prepared(store, ident)
    def commit():
        try:
            return store.commit(ident, transfer)
        except CloudError as exc:
            assert exc.code == 'upload_not_pending'
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(commit), pool.submit(store.cancel, ident, transfer)]
        for future in futures:
            future.result()
    final = store.upload_status(ident, transfer)
    assert final['state'] in ('committed', 'cancelled')
    assert bool(store.entries()['entries']) == (final['state'] == 'committed')


def test_bounded_body_and_administration(service):
    _, client, _ = service
    assert client.post('/api/cloud/v1/devices', json={'name': 'bad'}).status_code == 401
    assert client.post('/api/cloud/v1/uploads', content=b'x' * 9000).status_code == 413
    streamed = client.post('/api/cloud/v1/uploads', content=iter([b'x' * 5000, b'x' * 5000]))
    assert streamed.status_code in (400, 413)


def test_gateway_disabled_whitelist_and_admin(monkeypatch):
    import importlib.util
    path = Path(__file__).resolve().parents[1] / 'gateway/cloud_proxy.py'
    spec = importlib.util.spec_from_file_location('cloud_proxy_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def secret(x_secret_key: str = Header(default='')):
        if x_secret_key != 'admin-key':
            raise HTTPException(401)
    app = FastAPI()
    app.include_router(module.router(secret))
    client = TestClient(app)
    monkeypatch.setenv('DEPLOY_MODE', 'local')
    monkeypatch.setenv('CLOUD_ENABLED', '1')
    assert client.post('/api/cloud/v1/devices', json={'name': 'bad'}).status_code == 401
    assert client.get('/api/cloud/v1/status').status_code == 403
    cap = client.get('/api/cloud/v1/capabilities', headers={'X-Secret-Key': 'admin-key'}).json()
    assert cap['enabled'] is False and cap['reason'] == 'local_deployment'
    assert client.post('/api/cloud/v1/devices/evil').status_code == 404
