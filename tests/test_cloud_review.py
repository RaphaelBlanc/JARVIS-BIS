"""Regression cases from Théo's b8313b1 review, through real storage/gateway."""
import asyncio
import importlib.util
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from cloud.body_limit import BodyRejected, bounded_dav_stream
from cloud.app import create_app
from cloud.storage import Settings


def helper(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


H = helper('test_cloud_proxy')
W = helper('test_cloud_dav_write')
gateway, writable = H.gateway, W.writable


@pytest.mark.parametrize('streamed', [False, True])
def test_unauthenticated_delete_is_bounded_at_gateway(gateway, monkeypatch, streamed):
    client, _ = gateway
    monkeypatch.setenv('CLOUD_INTERNAL_URL', 'http://127.0.0.1:1')
    content = iter([b'x' * 4096, b'x' * 4096, b'x']) if streamed else b'x' * 8193
    response = client.request('DELETE', '/api/cloud/v1/entries/' + 'a' * 32, content=content)
    assert response.status_code == 413


@pytest.mark.parametrize('length', [None, '1', '8193'])
@pytest.mark.parametrize('dav_write', [False, True])
def test_delete_is_bounded_before_device_auth_at_storage(tmp_path, monkeypatch, length, dav_write):
    monkeypatch.setenv('DEPLOY_MODE', 'server')
    monkeypatch.setenv('CLOUD_ENABLED', '1')
    client = TestClient(create_app(Settings(tmp_path, dav_write=dav_write), 'x' * 32))
    client.headers['X-Cloud-Internal-Key'] = 'x' * 32
    headers = {} if length is None else {'Content-Length': length}
    response = client.request('DELETE', '/api/cloud/v1/entries/' + 'a' * 32,
                              content=iter([b'x' * 4096, b'x' * 4096, b'x']), headers=headers)
    assert response.status_code == 413


def test_gateway_never_passes_or_follows_dav_redirects(monkeypatch):
    from starlette.responses import RedirectResponse
    internal = FastAPI()
    @internal.get('/cloud/dav/name')
    def redirect():
        return RedirectResponse('https://untrusted.invalid/steal', status_code=307)
    monkeypatch.setenv('DEPLOY_MODE', 'server')
    monkeypatch.setenv('CLOUD_ENABLED', '1')
    monkeypatch.setenv('CLOUD_INTERNAL_KEY', 'x' * 32)
    app = FastAPI()
    app.include_router(H.PROXY.router(lambda: None))
    with H.serve(internal) as address:
        monkeypatch.setenv('CLOUD_INTERNAL_URL', address)
        response = TestClient(app).get('/cloud/dav/name')
    assert response.status_code == 502
    assert 'location' not in response.headers and not response.history


def public(writable, monkeypatch):
    _, internal, _ = writable
    monkeypatch.setenv('CLOUD_INTERNAL_KEY', 'x' * 32)
    app = FastAPI()
    app.include_router(H.PROXY.router(lambda: None))
    return app, internal.app


def test_gateway_rejects_bad_basic_without_reading_put_body(writable, monkeypatch):
    store, _, device = writable
    app, internal = public(writable, monkeypatch)
    monkeypatch.setenv('CLOUD_DAV_WRITE', '1')
    with H.serve(internal) as address:
        monkeypatch.setenv('CLOUD_INTERNAL_URL', address)
        reads, statuses = [], []
        async def receive():
            reads.append(True)
            raise AssertionError('Unauthenticated PUT body was consumed')
        async def send(message):
            if message['type'] == 'http.response.start':
                statuses.append(message['status'])
        scope = dict(type='http', method='PUT', path='/cloud/dav/unauthenticated', raw_path=b'/cloud/dav/unauthenticated',
                     root_path='', scheme='http', query_string=b'', http_version='1.1',
                     server=('localhost', 80), client=('127.0.0.1', 1234), headers=[])
        asyncio.run(app(scope, receive, send))
        assert statuses == [401] and not reads
    assert store.status(device['device_id'])['reserved_bytes'] == 0


@pytest.mark.parametrize('method', ['PUT', 'MKCOL', 'MOVE', 'DELETE'])
def test_gateway_flag_blocks_writes_when_storage_still_allows_them(writable, monkeypatch, method):
    store, _, device = writable
    app, internal = public(writable, monkeypatch)
    monkeypatch.setenv('CLOUD_DAV_WRITE', '0')
    with H.serve(internal) as address:
        monkeypatch.setenv('CLOUD_INTERNAL_URL', address)
        client = TestClient(app)
        response = client.request(method, '/cloud/dav/name', auth=(device['device_id'], device['token']))
        assert response.status_code == 403
        status = client.get('/api/cloud/v1/status', headers={'Authorization': 'Bearer ' + device['token']})
        assert status.json()['webdav'] == 'read_only'
        assert status.json()['features']['webdav_write'] is False
        assert client.get('/api/cloud/v1/capabilities').json()['features']['webdav_write'] is False
    assert store.entries()['entries'] == []


def test_slow_stream_has_total_deadline_even_when_every_chunk_is_on_time():
    async def receive():
        await asyncio.sleep(.01)
        return {'type': 'http.request', 'body': b'x', 'more_body': True}
    async def consume():
        async for _ in bounded_dav_stream({'headers': []}, receive, 1000, timeout=1, total_timeout=.04):
            pass
    with pytest.raises(BodyRejected) as caught:
        asyncio.run(consume())
    assert caught.value.status == 408


def test_empty_if_match_cannot_replace_and_move_requires_destination(writable):
    store, client, device = writable
    assert client.put('/cloud/dav/name', content=b'original').status_code == 201
    assert client.put('/cloud/dav/name', content=b'wrong', headers={'If-Match': ''}).status_code == 412
    assert client.get('/cloud/dav/name').content == b'original'
    entry = store.resolve('name')
    client.auth = None
    response = client.post('/api/cloud/v1/entries/' + entry['id'] + '/move',
        headers={'Authorization': 'Bearer ' + device['token']},
        json={'parent_revision': store.entry('root')['revision'], 'name': 'renamed'})
    assert response.status_code == 422
    with store.connection() as db:
        plan = db.execute('EXPLAIN QUERY PLAN SELECT * FROM versions WHERE file_id=?', (entry['id'],)).fetchall()
        assert any('versions_by_file' in row[3] for row in plan)


def test_chunked_put_reserves_received_chunks_instead_of_maximum(writable, monkeypatch):
    store, client, device = writable
    # max_file exceeds the whole quota: the old full-file reservation failed.
    store.settings = Settings(store.root, quota=24576, max_file=65536, chunk_size=4096, dav_write=True)
    reservations = []
    original = store.chunk
    def observe(*args):
        reservations.append(store.status(device['device_id'])['reserved_bytes'])
        return original(*args)
    monkeypatch.setattr(store, 'chunk', observe)
    response = client.put('/cloud/dav/chunked-small', content=iter([b'x' * 12000, b'x' * 8000]))
    assert response.status_code == 201, response.text
    assert reservations and max(reservations) <= 24576
    assert client.get('/cloud/dav/chunked-small').content == b'x' * 20000
    response = client.put('/cloud/dav/over-quota', content=iter([b'x' * 20000]))
    assert response.status_code == 507
    assert store.status(device['device_id'])['reserved_bytes'] == 0
    assert client.get('/cloud/dav/chunked-small').content == b'x' * 20000
