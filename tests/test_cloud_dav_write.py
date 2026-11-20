"""Optional native file-manager writes use the transactional storage engine."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import threading
from urllib.parse import quote

from fastapi.testclient import TestClient
import pytest

from cloud.app import create_app
from cloud.storage import Settings


@pytest.fixture
def writable(tmp_path, monkeypatch):
    monkeypatch.setenv('DEPLOY_MODE', 'server')
    monkeypatch.setenv('CLOUD_ENABLED', '1')
    app = create_app(Settings(tmp_path, quota=1024**2, max_file=65536, chunk_size=4096, dav_write=True), 'x' * 32)
    client = TestClient(app)
    client.headers['X-Cloud-Internal-Key'] = 'x' * 32
    device = app.state.store.associate('Files')
    client.auth = (device['device_id'], device['token'])
    return app.state.store, client, device


def test_native_create_replace_history_stale_write_and_delete(writable):
    store, client, device = writable
    content = b'hello' * 3000
    path = '/cloud/dav/' + quote('été #100%?.txt', safe='')
    result = client.put(path, content=content)
    assert result.status_code == 201, result.text
    etag = result.headers['etag']
    assert client.get(path).content == content
    assert store.status(device['device_id'])['webdav'] == 'read_write'
    replacement = client.put(path, content=b'replacement', headers={'If-Match': etag})
    assert replacement.status_code == 204
    assert client.put(path, content=b'stale', headers={'If-Match': etag}).status_code == 412
    assert client.put(path, content=b'stale', headers={'If-None-Match': '*'}).status_code == 412
    assert client.get(path).content == b'replacement'
    latest = client.put(path, content=b'GVfs replacement', headers={'If-Match': replacement.headers['etag'].strip('"')})
    assert latest.status_code == 204
    with store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM versions').fetchone()[0] == 2
    assert client.delete(path, headers={'If-Match': latest.headers['etag']}).status_code == 204
    assert client.get(path).status_code == 404
    assert store.status(device['device_id'])['used_bytes'] == 0


def test_native_folders_move_and_recursive_delete(writable):
    store, client, _ = writable
    assert client.request('MKCOL', '/cloud/dav/dossier').status_code == 201
    assert client.request('MKCOL', '/cloud/dav/destination').status_code == 201
    assert client.put('/cloud/dav/dossier/fichier.txt', content=b'contents').status_code == 201
    response = client.request('MOVE', '/cloud/dav/dossier', headers={'Destination': '/cloud/dav/destination/' + quote('renommé')})
    assert response.status_code == 201, response.text
    assert client.get('/cloud/dav/destination/' + quote('renommé') + '/fichier.txt').content == b'contents'
    assert client.request('MOVE', '/cloud/dav/destination', headers={'Destination': '/cloud/dav/destination/' + quote('renommé') + '/self'}).status_code == 409
    assert client.delete('/cloud/dav/destination', headers={'Depth': '0'}).status_code == 409
    assert client.delete('/cloud/dav/destination').status_code == 204
    assert store.entries()['entries'] == []


def test_chunked_native_put_and_failed_length_reclaim_parts(writable):
    store, client, device = writable
    content = b'x' * 20000 + b'end'
    response = client.put('/cloud/dav/chunked', content=iter([content[:13000], content[13000:]]))
    assert response.status_code == 201, response.text
    assert client.get('/cloud/dav/chunked').content == content
    before = store.status(device['device_id'])['used_bytes']
    response = client.put('/cloud/dav/partial', content=b'1234', headers={'Content-Length': '20'})
    assert response.status_code == 422
    assert client.get('/cloud/dav/partial').status_code == 404
    assert store.status(device['device_id'])['used_bytes'] == before
    assert store.status(device['device_id'])['reserved_bytes'] == 0
    assert list(store.parts.iterdir()) == []


def test_dav_limits_and_authentication_before_allocating(writable):
    store, client, _ = writable
    assert client.put('/cloud/dav/too-big', content=b'x' * 65537).status_code == 413
    assert client.put('/cloud/dav/bad-token', content=b'abc', auth=('wrong', 'wrong')).status_code == 401
    assert client.request('MKCOL', '/cloud/dav/bad', content=b'unsupported').status_code == 415
    assert client.put('/cloud/dav/%2e%2e/escape', content=b'wrong').status_code == 400
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM uploads WHERE state='pending'").fetchone()[0] == 0


def test_concurrent_native_replacements_have_one_winner(writable, monkeypatch):
    store, client, device = writable
    first = client.put('/cloud/dav/concurrent', content=b'original')
    assert first.status_code == 201
    barrier = threading.Barrier(2)
    original_commit = store.commit
    def simultaneous(*args):
        barrier.wait(timeout=5)
        return original_commit(*args)
    monkeypatch.setattr(store, 'commit', simultaneous)
    def replace(content):
        return client.put('/cloud/dav/concurrent', content=content).status_code
    with ThreadPoolExecutor(2) as pool:
        statuses = list(pool.map(replace, [b'one', b'two']))
    assert sorted(statuses) == [204, 412]
    assert client.get('/cloud/dav/concurrent').content in (b'one', b'two')
    assert store.status(device['device_id'])['reserved_bytes'] == 0


def test_native_move_cannot_overwrite_another_file(writable):
    _, client, _ = writable
    assert client.put('/cloud/dav/a', content=b'a').status_code == 201
    assert client.put('/cloud/dav/b', content=b'b').status_code == 201
    response = client.request('MOVE', '/cloud/dav/a', headers={'Destination': '/cloud/dav/b', 'Overwrite': 'T'})
    assert response.status_code == 409
    assert client.get('/cloud/dav/a').content == b'a'
    assert client.get('/cloud/dav/b').content == b'b'


def test_writable_dav_through_real_gateway_and_same_origin_destination(writable, monkeypatch):
    from fastapi import FastAPI
    spec = importlib.util.spec_from_file_location('dav_gateway_check', Path(__file__).with_name('test_cloud_proxy.py'))
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    store, internal_client, device = writable
    monkeypatch.setenv('CLOUD_INTERNAL_KEY', 'x' * 32)
    monkeypatch.setenv('CLOUD_DAV_WRITE', '1')
    gateway = FastAPI()
    gateway.include_router(helper.PROXY.router(lambda: None))
    with helper.serve(internal_client.app) as address:
        monkeypatch.setenv('CLOUD_INTERNAL_URL', address)
        client = TestClient(gateway)
        client.auth = (device['device_id'], device['token'])
        content = b'z' * 20000
        result = client.put('/cloud/dav/source', content=iter([content[:10000], content[10000:]]))
        assert result.status_code == 201, result.text
        result = client.request('MOVE', '/cloud/dav/source', headers={'Destination': 'http://testserver/cloud/dav/' + quote('été #100%?')})
        assert result.status_code == 201, result.text
        assert client.get('/cloud/dav/' + quote('été #100%?')).content == content
        result = client.request('MOVE', '/cloud/dav/' + quote('été #100%?'), headers={'Destination': 'https://untrusted.invalid/cloud/dav/stolen'})
        assert result.status_code == 400
        assert store.resolve('été #100%?')['size'] == len(content)
